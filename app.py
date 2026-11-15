import re
from datetime import datetime, timezone
from io import BytesIO, StringIO

import pandas as pd
import pycountry
import requests
import streamlit as st
from babel.numbers import get_territory_currencies
from bs4 import BeautifulSoup

BETA_XLS = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
BETA_HTML = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/Betas.html"
RATINGS = "https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html"
OECD = (
    "https://sdmx.oecd.org/public/rest/data/"
    "OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT,2.0"
)
BONDS = "https://tradingeconomics.com/bonds"
NO_DATE = "Observation date unavailable"
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
EMPTY_QUOTE = (None, None)


def fetch(url, **kwargs):
    response = requests.get(url, timeout=(5, 10), **kwargs)
    response.raise_for_status()
    return response


def metadata(url, date=NO_DATE):
    retrieved = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return url, date, retrieved


def source_date(text):
    match = re.search(
        r"(?:data as of|as of|updated:?)\s*"
        r"([A-Za-z]+\s+(?:\d{1,2},?\s+)?20\d{2}|\d{1,2}/\d{1,2}/20\d{2})",
        text,
        re.I,
    )
    return match.group(1) if match else NO_DATE


def html_rows(page):
    for row in page.find_all("tr"):
        yield [
            cell.get_text(" ", strip=True)
            for cell in row.find_all(["td", "th"], recursive=False)
        ]


def parse_betas(rows):
    for index, row in enumerate(rows):
        headers = [str(cell).strip().casefold() for cell in row]
        if "industry name" in headers and "unlevered beta" in headers:
            name_col = headers.index("industry name")
            beta_col = headers.index("unlevered beta")
            break
    else:
        raise ValueError("Unlevered beta column missing")

    betas = {}
    for row in rows[index + 1:]:
        if len(row) <= max(name_col, beta_col):
            continue
        name = " ".join(str(row[name_col]).split())
        beta = pd.to_numeric(row[beta_col], errors="coerce")
        if (
            name
            and name.casefold() != "nan"
            and not name.casefold().startswith("total market")
            and pd.notna(beta)
        ):
            betas[name] = float(beta)
    if not betas:
        raise ValueError("Industry beta values missing")
    return betas


@st.cache_data(ttl=3600, show_spinner=False)
def industry_betas():
    try:
        sheets = pd.read_excel(
            BytesIO(fetch(BETA_XLS).content),
            sheet_name=None,
            header=None,
            engine="xlrd",
        )
        for sheet in sheets.values():
            rows = sheet.values.tolist()
            try:
                betas = parse_betas(rows)
            except ValueError:
                continue
            heading = " ".join(str(cell) for row in rows[:10] for cell in row)
            return betas, metadata(BETA_XLS, source_date(heading))
    except (requests.RequestException, ValueError, ImportError):
        pass

    page = BeautifulSoup(fetch(BETA_HTML).text, "html.parser")
    return parse_betas(list(html_rows(page))), metadata(
        BETA_HTML, source_date(page.get_text(" ", strip=True))
    )


@st.cache_data(ttl=3600, show_spinner=False)
def bbb_spread():
    page = BeautifulSoup(fetch(RATINGS).text, "html.parser")
    for table in page.find_all("table"):
        if not re.search(r"large\s+non.financial", table.get_text(" ", strip=True), re.I):
            continue
        for row in html_rows(table):
            cells = [re.sub(r"\s+", "", cell) for cell in row]
            for rating, spread in zip(cells, cells[1:]):
                if rating.casefold() == "baa2/bbb" and re.fullmatch(
                    r"\d+(?:\.\d+)?%", spread
                ):
                    return float(spread[:-1]), metadata(
                        RATINGS, source_date(page.get_text(" ", strip=True))
                    )
    raise ValueError("Large nonfinancial Baa2/BBB row missing")


@st.cache_data(ttl=3600, show_spinner=False)
def corporate_tax(country_code):
    url = f"{OECD}/{country_code}.A..ST..S13..."
    response = fetch(
        url,
        params={
            "lastNObservations": 1,
            "dimensionAtObservation": "AllDimensions",
            "format": "csvfilewithlabels",
        },
    )
    data = pd.read_csv(StringIO(response.text))
    data["OBS_VALUE"] = pd.to_numeric(data["OBS_VALUE"], errors="coerce")
    data = data.dropna(subset=["OBS_VALUE"]).sort_values("TIME_PERIOD")
    if data.empty:
        raise ValueError("No OECD tax observation")
    latest = data.iloc[-1]
    return float(latest["OBS_VALUE"]), metadata(url, str(latest["TIME_PERIOD"]))


@st.cache_data(ttl=3600, show_spinner=False)
def bond_table():
    page = BeautifulSoup(fetch(BONDS).text, "html.parser")
    quotes = {}
    for row in html_rows(page):
        if len(row) < 2:
            continue
        name = re.sub(r"\s+10Y(?:\s+Bond Yield)?$", "", row[0], flags=re.I)
        rate = row[1].rstrip("%").strip()
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", rate):
            continue
        date = next(
            (
                cell for cell in reversed(row[2:])
                if re.fullmatch(r"[A-Za-z]{3}\s*/?\s*\d{1,2}|\d{1,2}/\d{1,2}", cell)
            ),
            NO_DATE,
        )
        quotes[name.casefold()] = float(rate), metadata(BONDS, date)
    return quotes


@st.cache_data(ttl=3600, show_spinner=False)
def bond_country_page(country):
    slug = country.lower().replace(" ", "-")
    url = f"https://tradingeconomics.com/{slug}/government-bond-yield"
    page = BeautifulSoup(fetch(url).text, "html.parser")
    text = page.get_text(" ", strip=True)
    if not re.search(r"10.Year Government Bond Yield|10Y Bond Yield", text, re.I):
        raise ValueError("10-year bond series missing")

    summary = re.search(
        r"\bto\s+(-?\d+(?:\.\d+)?)\s*%\s+on\s+"
        r"([A-Za-z]+\s+\d{1,2},\s+20\d{2})",
        text,
        re.I,
    )
    actual = re.search(
        r"\bActual\s+(-?\d+(?:\.\d+)?)\s*%?\s+Daily Change", text, re.I
    )
    if actual:
        return float(actual[1]), metadata(url, summary[2] if summary else NO_DATE)
    if summary:
        return float(summary[1]), metadata(url, summary[2])
    raise ValueError("10-year quote missing")


def load(label, loader, fallback):
    try:
        return loader()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        st.warning(f"{label} unavailable (HTTP {status}).")
    except (requests.RequestException, ValueError, KeyError, ImportError) as exc:
        st.warning(f"{label} unavailable ({type(exc).__name__}: {exc}).")
    return fallback


def country_yield(country, quotes):
    quote = quotes.get(country.casefold())
    if quote and quote[1][1] != NO_DATE:
        return quote
    return load(
        f"{country} 10-year yield",
        lambda: bond_country_page(country),
        quote or EMPTY_QUOTE,
    )


def market_input(label, quote, key, minimum=None, maximum=None):
    value, source = quote
    if value is None:
        st.caption(f"{label} requires manual entry.")
        manual = True
    else:
        manual = st.checkbox(f"Override {label}", key=f"override_{key}")

    if manual:
        value = st.number_input(
            label,
            value=value,
            min_value=minimum,
            max_value=maximum,
            step=0.01,
            format="%.4f",
            key=f"input_{key}",
        )
    else:
        suffix = "%" if "(%)" in label else ""
        st.write(f"**{label}** · {value:.4f}{suffix}")
    return value, (label, "Manual" if manual else "Source", source)


def calculate(beta, erp, share, tax, us_yield, local_yield, usd_spread, override):
    equity_share = 1 - share
    levered_beta = beta * (1 + (1 - tax) * share / equity_share)
    factor = (1 + local_yield) / (1 + us_yield)
    local_spread = usd_spread * factor if override is None else override

    us_equity = us_yield + levered_beta * erp
    local_equity = (1 + us_equity) * factor - 1
    us_debt = us_yield + usd_spread
    local_debt = local_yield + local_spread
    us_after_tax = us_debt * (1 - tax)
    local_after_tax = local_debt * (1 - tax)

    results = {
        "Cost of equity": (us_equity, local_equity),
        "Pretax cost of debt": (us_debt, local_debt),
        "After-tax cost of debt": (us_after_tax, local_after_tax),
        "WACC": (
            equity_share * us_equity + share * us_after_tax,
            equity_share * local_equity + share * local_after_tax,
        ),
    }
    return results, local_spread, levered_beta


def show_sources(entries):
    with st.expander("Data sources and assumptions"):
        for label, mode, source in entries:
            if source:
                url, date, retrieved = source
                st.markdown(
                    f"**{label}** · {mode} · [{date}]({url}) · Retrieved {retrieved}"
                )
            else:
                st.write(f"{label} · {mode} · No source observation loaded")
        st.write(
            "The 10-year government yields proxy for risk-free rates. Both currencies "
            "use the same project tax rate, debt share and equity risk premium. "
            "USD cost of equity uses CAPM. The local cost of equity and automatic "
            "local credit spread use the ratio of gross government yields. "
            "WACC uses the after-tax cost of debt."
        )


def main():
    st.set_page_config(page_title="International Cost of Capital", layout="wide")
    st.title("International Cost of Capital")
    if st.button("Refresh source data"):
        for source in (
            industry_betas, bbb_spread, corporate_tax, bond_table, bond_country_page
        ):
            source.clear()

    betas, beta_source = load("Damodaran industry betas", industry_betas, ({}, None))
    industries = sorted(set(betas) | {DEFAULT_INDUSTRY})
    countries = {
        COUNTRY_NAMES.get(item.alpha_3, item.name): item
        for item in pycountry.countries
        if get_territory_currencies(item.alpha_2)
    }
    names = sorted(countries)

    project, market = st.columns(2)
    with project:
        st.subheader("Project assumptions")
        industry = st.selectbox(
            "Industry", industries, index=industries.index(DEFAULT_INDUSTRY)
        )
        country = st.selectbox("Target country", names, index=names.index("United States"))
        territory = countries[country]
        currencies = get_territory_currencies(territory.alpha_2)
        currency = (
            st.selectbox("Local currency", currencies)
            if len(currencies) > 1 else currencies[0]
        )

        erp = st.number_input("Equity risk premium (%)", value=6.5, step=0.1) / 100
        share = st.number_input(
            "Target debt share (%)", min_value=0.0, max_value=99.99, value=30.0
        ) / 100
        tax_basis = st.radio("Project tax rate", ["U.S. rate", "Target country rate"])
        tax_code = "USA" if tax_basis == "U.S. rate" else territory.alpha_3
        tax_quote = load(
            f"OECD tax rate ({tax_code})", lambda: corporate_tax(tax_code), EMPTY_QUOTE
        )
        tax, tax_info = market_input(
            "Project corporate tax rate (%)", tax_quote, f"tax_{tax_code}", 0.0, 100.0
        )
        beta, beta_info = market_input(
            "Unlevered beta", (betas.get(industry), beta_source), f"beta_{industry}"
        )

    with market:
        st.subheader("Market inputs")
        spread_quote = load("Damodaran BBB spread", bbb_spread, EMPTY_QUOTE)
        quotes = load("Trading Economics bond table", bond_table, {})
        usd_quote = country_yield("United States", quotes)

        spread, spread_info = market_input("USD BBB spread (%)", spread_quote, "spread")
        usd, usd_info = market_input(
            "U.S. 10-year yield (%)", usd_quote, "usd_yield", -99.99
        )
        if country == "United States":
            local = usd
            local_info = ("Target country yield", usd_info[1], usd_info[2])
            st.caption("Both columns use the U.S. yield when the target is the United States.")
        else:
            local_quote = country_yield(country, quotes)
            local, local_info = market_input(
                "Target country 10-year yield (%)",
                local_quote,
                f"yield_{territory.alpha_3}",
                -99.99,
            )

        use_override = st.checkbox("Override local BBB spread")
        override = None
        if use_override:
            suggested = None
            if None not in (spread, usd, local):
                suggested = spread * (100 + local) / (100 + usd)
            override = st.number_input(
                "Manual local BBB spread (%)",
                value=suggested,
                step=0.01,
                format="%.4f",
                key=f"local_spread_{territory.alpha_3}",
            )

    show_sources((beta_info, tax_info, spread_info, usd_info, local_info))

    if None in (beta, tax, spread, usd, local) or (use_override and override is None):
        st.info("Enter the missing inputs to see results.")
        return

    assumptions = (
        erp, share, tax / 100, usd / 100, local / 100, spread / 100,
        None if override is None else override / 100,
    )
    results, active_spread, levered_beta = calculate(beta, *assumptions)

    st.subheader("Results")
    st.write(f"Levered beta · **{levered_beta:.4f}**")
    spread_label = "Manual" if use_override else "Calculated"
    st.write(f"{spread_label} local BBB spread · **{active_spread:.4%}**")

    usd_label = "USD (U.S. basis)"
    local_label = f"{currency} ({country} basis)"
    table = pd.DataFrame(
        [(metric, f"{us:.4%}", f"{local:.4%}") for metric, (us, local) in results.items()],
        columns=["Metric", usd_label, local_label],
    )
    st.table(table.set_index("Metric"))

    st.subheader("All-industry comparison")
    if not betas:
        st.info("Industry comparison requires the Damodaran beta data.")
        return

    comparison = []
    for name, industry_beta in sorted({**betas, industry: beta}.items()):
        metrics, _, _ = calculate(industry_beta, *assumptions)
        row = {"Industry": name, "Unlevered beta": industry_beta}
        for metric, (us_rate, local_rate) in metrics.items():
            row[f"{metric} | {usd_label} (%)"] = round(us_rate * 100, 6)
            row[f"{metric} | {local_label} (%)"] = round(local_rate * 100, 6)
        comparison.append(row)

    frame = pd.DataFrame(comparison)
    st.dataframe(frame, hide_index=True)
    st.download_button(
        "Download results (CSV)",
        frame.to_csv(index=False),
        "cost_of_capital_by_industry.csv",
        "text/csv",
    )


if __name__ == "__main__":
    main()
