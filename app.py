import re
from datetime import datetime, timezone
from io import BytesIO, StringIO

import pandas as pd
import pycountry
import requests
import streamlit as st
from babel.numbers import get_territory_currencies
from bs4 import BeautifulSoup

BETA_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
BETA_PAGE = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/Betas.html"
RATING_URL = "https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html"
TAX_URL = "https://sdmx.oecd.org/public/rest/data/OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT,2.0/"
BOND_URL = "https://tradingeconomics.com/bonds"
DEFAULT_INDUSTRY = "Software (System & Application)"

COUNTRY_NAMES = {
    "BOL": "Bolivia",
    "CZE": "Czech Republic",
    "IRN": "Iran",
    "KOR": "South Korea",
    "RUS": "Russia",
    "TUR": "Turkey",
    "TWN": "Taiwan",
    "VNM": "Vietnam",
}


def fetch(url, **kwargs):
    response = requests.get(url, timeout=(5, 10), **kwargs)
    response.raise_for_status()
    return response


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def source_date(text):
    match = re.search(
        r"(?:as of|updated:?)\s*"
        r"([A-Za-z]+\s+(?:\d{1,2},?\s+)?20\d{2}|\d{1,2}/\d{1,2}/20\d{2})",
        text, re.I,
    )
    return match.group(1) if match else "Observation date unavailable"


def parse_beta_rows(rows):
    for index, row in enumerate(rows):
        headers = [str(cell).strip().casefold() for cell in row]
        if "industry name" in headers and "unlevered beta" in headers:
            name_index = headers.index("industry name")
            beta_index = headers.index("unlevered beta")
            break
    else:
        raise ValueError("Unlevered beta column missing.")

    result = {}
    for row in rows[index + 1:]:
        if len(row) <= max(name_index, beta_index):
            continue
        name = " ".join(str(row[name_index]).split())
        beta = pd.to_numeric(row[beta_index], errors="coerce")
        if name and name != "nan" and not name.lower().startswith("total market"):
            if pd.notna(beta):
                result[name] = float(beta)
    if not result:
        raise ValueError("Industry beta values missing.")
    return result


@st.cache_data(ttl=3600, show_spinner=False)
def industry_betas():
    try:
        workbook = pd.read_excel(
            BytesIO(fetch(BETA_URL).content), header=None, sheet_name=None,
            engine="xlrd",
        )
        for sheet in workbook.values():
            rows = sheet.values.tolist()
            try:
                values = parse_beta_rows(rows)
            except ValueError:
                continue
            heading = " ".join(str(cell) for row in rows[:10] for cell in row)
            return values, (BETA_URL, source_date(heading), timestamp())
    except (requests.RequestException, ValueError):
        pass

    soup = BeautifulSoup(fetch(BETA_PAGE).text, "html.parser")
    rows = [
        [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        for row in soup.find_all("tr")
    ]
    return parse_beta_rows(rows), (
        BETA_PAGE, source_date(soup.get_text(" ", strip=True)), timestamp()
    )


@st.cache_data(ttl=3600, show_spinner=False)
def bbb_spread():
    soup = BeautifulSoup(fetch(RATING_URL).text, "html.parser")
    for table in soup.find_all("table"):
        if not re.search(r"large\s+non.financial", table.get_text(" ", strip=True), re.I):
            continue
        for row in table.find_all("tr"):
            cells = [
                re.sub(r"\s+", "", cell.get_text(" ", strip=True))
                for cell in row.find_all(["td", "th"], recursive=False)
            ]
            for rating, value in zip(cells, cells[1:]):
                if rating.casefold() == "baa2/bbb" and value.endswith("%"):
                    return float(value[:-1]), (
                        RATING_URL, source_date(soup.get_text(" ", strip=True)), timestamp()
                    )
    raise ValueError("Large nonfinancial Baa2/BBB row missing.")


@st.cache_data(ttl=3600, show_spinner=False)
def corporate_tax(code):
    url = f"{TAX_URL}{code}.A..ST..S13..."
    response = fetch(
        url,
        params={"lastNObservations": 1, "format": "csvfile-with-labels"},
        headers={"Accept": "text/csv"},
    )
    data = pd.read_csv(StringIO(response.text))
    data["OBS_VALUE"] = pd.to_numeric(data["OBS_VALUE"], errors="coerce")
    data = data.dropna(subset=["OBS_VALUE"]).sort_values("TIME_PERIOD")
    if data.empty:
        raise ValueError("No OECD tax observation.")
    latest = data.iloc[-1]
    return float(latest["OBS_VALUE"]), (url, str(latest["TIME_PERIOD"]), timestamp())


@st.cache_data(ttl=3600, show_spinner=False)
def bond_table():
    soup = BeautifulSoup(fetch(BOND_URL).text, "html.parser")
    quotes = {}
    for row in soup.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        name = cells[0].get_text(" ", strip=True)
        name = re.sub(r"\s+10Y(?:\s+Bond Yield)?$", "", name, flags=re.I)
        number = cells[1].get_text(strip=True).rstrip("%")
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", number):
            continue
        date = "Observation date unavailable"
        for cell in reversed(cells[2:]):
            candidate = cell.get_text(" ", strip=True)
            if re.fullmatch(r"[A-Za-z]{3}\s*/?\s*\d{1,2}|\d{1,2}/\d{1,2}", candidate):
                date = candidate
                break
        quotes[name.casefold()] = float(number), (BOND_URL, date, timestamp())
    return quotes


@st.cache_data(ttl=3600, show_spinner=False)
def country_yield(country):
    try:
        quote = bond_table().get(country.casefold())
        if quote and quote[1][1] != "Observation date unavailable":
            return quote
    except (requests.RequestException, ValueError):
        quote = None

    slug = country.lower().replace(" ", "-")
    url = f"https://tradingeconomics.com/{slug}/government-bond-yield"
    try:
        soup = BeautifulSoup(fetch(url).text, "html.parser")
    except requests.RequestException:
        if quote:
            return quote
        raise

    text = soup.get_text(" ", strip=True)
    if re.search(r"10.Year Government Bond Yield|10Y Bond Yield", text, re.I):
        summary = re.search(
            r"\bto\s+(-?\d+(?:\.\d+)?)\s*%\s+on\s+"
            r"([A-Za-z]+\s+\d{1,2},\s+20\d{2})", text, re.I,
        )
        actual = re.search(
            r"\bActual\s+(-?\d+(?:\.\d+)?)\s*%?\s+Daily Change", text, re.I,
        )
        if actual or summary:
            value = float(actual[1] if actual else summary[1])
            date = summary[2] if summary else source_date(text)
            return value, (url, date, timestamp())

    if quote:
        return quote
    raise ValueError("10-year yield missing.")


def load(label, loader, fallback):
    try:
        return loader()
    except (requests.RequestException, ValueError, KeyError, ImportError) as exc:
        st.warning(f"{label} unavailable ({type(exc).__name__}).")
        return fallback


def market_input(label, quote, key, decimals=4, minimum=None, maximum=None):
    rate, metadata = quote
    manual = st.checkbox(f"Override {label}", value=rate is None, key=f"override_{key}")
    if manual:
        rate = st.number_input(
            label, value=rate, step=0.01, format=f"%.{decimals}f",
            min_value=minimum, max_value=maximum, key=f"input_{key}",
        )
    elif rate is not None:
        st.write(f"**{label}** · {rate:.{decimals}f}")
    return rate, (label, "Manual" if manual else "Source", metadata)


def calculate(beta, erp, debt_share, tax, usd_yield, local_yield, usd_spread, override):
    equity_share = 1 - debt_share
    levered_beta = beta * (1 + (1 - tax) * debt_share / equity_share)
    factor = (1 + local_yield) / (1 + usd_yield)
    local_spread = usd_spread * factor if override is None else override

    usd_equity = usd_yield + levered_beta * erp
    local_equity = (1 + usd_equity) * factor - 1
    usd_debt = usd_yield + usd_spread
    local_debt = local_yield + local_spread
    usd_after_tax = usd_debt * (1 - tax)
    local_after_tax = local_debt * (1 - tax)

    return {
        "Cost of equity": (usd_equity, local_equity),
        "Pretax cost of debt": (usd_debt, local_debt),
        "After-tax cost of debt": (usd_after_tax, local_after_tax),
        "WACC": (
            equity_share * usd_equity + debt_share * usd_after_tax,
            equity_share * local_equity + debt_share * local_after_tax,
        ),
    }, local_spread, levered_beta


def main():
    st.set_page_config(page_title="International Cost of Capital", layout="wide")
    st.title("International Cost of Capital")
    if st.button("Refresh source data"):
        st.cache_data.clear()

    betas, beta_source = load("Industry betas", industry_betas, ({}, None))
    industries = sorted(set(betas) | {DEFAULT_INDUSTRY})
    countries = {
        COUNTRY_NAMES.get(country.alpha_3, country.name): country
        for country in pycountry.countries
        if get_territory_currencies(country.alpha_2)
    }
    names = sorted(countries)

    project, market = st.columns(2)
    with project:
        st.subheader("Project assumptions")
        industry = st.selectbox("Industry", industries, index=industries.index(DEFAULT_INDUSTRY))
        country = st.selectbox("Target country", names, index=names.index("United States"))
        territory = countries[country]
        currencies = get_territory_currencies(territory.alpha_2)
        currency = st.selectbox("Local currency", currencies) if len(currencies) > 1 else currencies[0]
        erp = st.number_input("Equity risk premium (%)", value=6.5, step=0.1) / 100
        share = st.number_input(
            "Target debt share (%)", min_value=0.0, max_value=99.99, value=30.0
        ) / 100
        tax_basis = st.radio("Project tax rate", ["U.S. rate", "Target country rate"])
        tax_code = "USA" if tax_basis == "U.S. rate" else territory.alpha_3
        tax_quote = load("OECD tax rate", lambda: corporate_tax(tax_code), (None, None))
        tax, tax_info = market_input(
            "Project corporate tax rate (%)", tax_quote, f"tax_{tax_code}",
            minimum=0.0, maximum=100.0,
        )
        beta, beta_info = market_input(
            "Unlevered beta", (betas.get(industry), beta_source), f"beta_{industry}"
        )

    with market:
        st.subheader("Market inputs")
        spread_quote = load("Damodaran BBB spread", bbb_spread, (None, None))
        usd_quote = load("U.S. 10-year yield", lambda: country_yield("United States"), (None, None))
        local_quote = usd_quote
        if country != "United States":
            local_quote = load(
                f"{country} 10-year yield", lambda: country_yield(country), (None, None)
            )

        spread, spread_info = market_input("USD BBB spread (%)", spread_quote, "spread")
        usd, usd_info = market_input("U.S. 10-year yield (%)", usd_quote, "usd", minimum=-99.99)
        local, local_info = market_input(
            "Target country 10-year yield (%)", local_quote,
            f"yield_{territory.alpha_3}", minimum=-99.99,
        )
        use_override = st.checkbox("Override local BBB spread")
        override = None
        if use_override:
            suggested = spread * (100 + local) / (100 + usd) if None not in (spread, local, usd) else None
            override = st.number_input(
                "Manual local BBB spread (%)", value=suggested, step=0.01,
                format="%.4f", key=f"local_spread_{territory.alpha_3}",
            )

    with st.expander("Data sources and assumptions"):
        for label, status, source in (beta_info, tax_info, spread_info, usd_info, local_info):
            if source is None:
                st.write(f"{label} · {status} · No source observation loaded")
            else:
                url, date, retrieved = source
                st.markdown(f"**{label}** · {status} · [{date}]({url}) · Retrieved {retrieved}")
        st.write(
            "Government yields proxy for risk-free rates. Both currencies use the same "
            "project tax rate, debt share and equity risk premium. The local cost of equity "
            "and automatic local credit spread use the ratio of gross government yields. "
            "WACC weights cost of equity and after-tax cost of debt."
        )

    if None in (beta, tax, spread, usd, local) or (use_override and override is None):
        st.info("Enter the missing market inputs to see results.")
        return

    assumptions = (
        erp, share, tax / 100, usd / 100, local / 100, spread / 100,
        None if override is None else override / 100,
    )
    results, local_spread, levered_beta = calculate(beta, *assumptions)
    st.subheader("Results")
    st.write(f"Levered beta · **{levered_beta:.4f}**")
    spread_label = "Manual" if use_override else "Calculated"
    st.write(f"{spread_label} local BBB spread · **{local_spread:.4%}**")

    usd_label, local_label = "USD (U.S. basis)", f"{currency} ({country} basis)"
    result_table = pd.DataFrame(
        [(metric, f"{a:.4%}", f"{b:.4%}") for metric, (a, b) in results.items()],
        columns=["Metric", usd_label, local_label],
    )
    st.table(result_table.set_index("Metric"))

    st.subheader("All-industry comparison")
    comparison = []
    for name, industry_beta in sorted({**betas, industry: beta}.items()):
        metrics, _, _ = calculate(industry_beta, *assumptions)
        row = {"Industry": name, "Unlevered beta": industry_beta}
        for metric, (a, b) in metrics.items():
            row[f"{metric} | {usd_label} (%)"] = round(a * 100, 6)
            row[f"{metric} | {local_label} (%)"] = round(b * 100, 6)
        comparison.append(row)

    frame = pd.DataFrame(comparison)
    st.dataframe(frame, hide_index=True)
    st.download_button(
        "Download results (CSV)", frame.to_csv(index=False),
        "cost_of_capital_by_industry.csv", "text/csv",
    )


if __name__ == "__main__":
    main()
