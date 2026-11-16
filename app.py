import csv
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
OECD = "https://sdmx.oecd.org/public/rest/data/OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT,2.0/"
BONDS = "https://tradingeconomics.com/bonds"
DEFAULT_INDUSTRY = "Software (System & Application)"
COUNTRY_NAMES = {
    "BOL": "Bolivia", "CZE": "Czech Republic", "GBR": "United Kingdom",
    "IRN": "Iran", "KOR": "South Korea", "LAO": "Laos", "RUS": "Russia",
    "TUR": "Turkey", "TWN": "Taiwan", "VNM": "Vietnam",
}
COUNTRIES = {
    COUNTRY_NAMES.get(c.alpha_3, c.name): c
    for c in pycountry.countries
    if get_territory_currencies(c.alpha_2)
}


def get(url, **kwargs):
    response = requests.get(url, timeout=(5, 10), **kwargs)
    response.raise_for_status()
    return response


def fetched_at():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def source_metadata(url, date, frequency=""):
    return {"url": url, "date": date, "frequency": frequency, "retrieved": fetched_at()}


def normalize_source(source):
    if isinstance(source, dict):
        return source if {"url", "date", "retrieved"} <= source.keys() else None
    if not isinstance(source, (list, tuple)):
        return None
    if len(source) == 3:
        url, date, retrieved = source
        frequency = ""
    elif len(source) == 4:
        url, date, frequency, retrieved = source
    else:
        return None
    return {"url": url, "date": date, "frequency": frequency, "retrieved": retrieved}


def source_date(text):
    match = re.search(
        r"(?:as of|Last Updated:?|Date of Analysis:)\s*"
        r"(?:[A-Za-z]+\s+(?:\d{1,2},?\s+)?20\d{2}|\d{1,2}/\d{1,2}/20\d{2})",
        text, re.I,
    )
    return match.group() if match else "Observation date not shown"


def parse_beta_grid(grid):
    for row_number, row in enumerate(grid.itertuples(index=False, name=None)):
        headers = [str(cell).strip().casefold() for cell in row]
        if "industry name" not in headers or "unlevered beta" not in headers:
            continue
        data = grid.iloc[row_number + 1:, [
            headers.index("industry name"), headers.index("unlevered beta")
        ]].copy()
        data.columns = ["Industry", "Unlevered beta"]
        data["Industry"] = data["Industry"].astype(str).str.strip().str.replace(
            r"\s+", " ", regex=True
        )
        data["Unlevered beta"] = pd.to_numeric(data["Unlevered beta"], errors="coerce")
        data = data.dropna().loc[lambda f: ~f["Industry"].str.match(r"(?i)^total market")]
        data = data.drop_duplicates("Industry").sort_values("Industry")
        if data.empty:
            break
        return dict(zip(data["Industry"], data["Unlevered beta"]))
    raise ValueError("Industry beta columns were not found.")


@st.cache_data(ttl=3600, show_spinner=False)
def load_industry_betas():
    try:
        response = get(BETA_XLS)
        for sheet in pd.read_excel(
            BytesIO(response.content), header=None, sheet_name=None, engine="xlrd"
        ).values():
            try:
                rates = parse_beta_grid(sheet)
            except ValueError:
                continue
            date = source_date(" ".join(sheet.head(10).fillna("").astype(str).stack()))
            return rates, source_metadata(BETA_XLS, date)
    except (requests.RequestException, ValueError):
        pass
    page = get(BETA_HTML).text
    for table in pd.read_html(StringIO(page), header=None):
        try:
            grid = pd.concat([pd.DataFrame([list(table.columns)]), table.set_axis(
                range(len(table.columns)), axis=1
            )], ignore_index=True)
            rates = parse_beta_grid(grid)
        except ValueError:
            continue
        date = source_date(BeautifulSoup(page, "html.parser").get_text(" ", strip=True))
        return rates, source_metadata(BETA_HTML, date)
    raise ValueError("Damodaran beta table was not found.")


@st.cache_data(ttl=3600, show_spinner=False)
def load_usd_bbb_spread():
    soup = BeautifulSoup(get(RATINGS).text, "html.parser")
    for table in soup.find_all("table"):
        if not re.search(r"large\s+non.financial", table.get_text(" ", strip=True), re.I):
            continue
        for row in table.find_all("tr"):
            cells = [re.sub(r"\s+", "", c.get_text(" ", strip=True))
                     for c in row.find_all(["td", "th"], recursive=False)]
            for rating, spread in zip(cells, cells[1:]):
                if rating.casefold() == "baa2/bbb" and re.fullmatch(r"\d+(\.\d+)?%", spread):
                    return float(spread[:-1]), source_metadata(
                        RATINGS, source_date(soup.get_text(" ", strip=True))
                    )
    raise ValueError("Large nonfinancial Baa2/BBB row was not found.")


@st.cache_data(ttl=3600, show_spinner=False)
def load_statutory_tax_rate(country_code):
    url = f"{OECD}{country_code}.A..ST..S13..."
    response = get(url, params={
        "lastNObservations": 1,
        "dimensionAtObservation": "AllDimensions",
        "format": "csvfilewithlabels",
    })
    data = pd.read_csv(StringIO(response.text))
    for column in ("REF_AREA", "OBS_VALUE", "TIME_PERIOD"):
        if column not in data:
            raise ValueError("OECD response lacked required columns.")
    data["OBS_VALUE"] = pd.to_numeric(data["OBS_VALUE"], errors="coerce")
    data = data.dropna(subset=["OBS_VALUE"]).sort_values("TIME_PERIOD")
    if data.empty:
        raise ValueError("No OECD tax observation.")
    latest = data.iloc[-1]
    return float(latest["OBS_VALUE"]), source_metadata(url, str(latest["TIME_PERIOD"]))


@st.cache_data(ttl=3600, show_spinner=False)
def load_te_bond_table():
    soup = BeautifulSoup(get(BONDS).text, "html.parser")
    results = {}
    for row in soup.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        name = cells[0].get_text(" ", strip=True)
        name = re.sub(r"\s+10Y(?:\s+Bond Yield)?$", "", name, flags=re.I).casefold()
        value = cells[1].get_text(strip=True).rstrip("%")
        if re.fullmatch(r"-?\d+(\.\d+)?", value):
            clock = row.find("time")
            date = clock.get("datetime", clock.get_text(strip=True)) if clock else ""
            if not date:
                for cell in reversed(cells[2:]):
                    candidate = cell.get_text(strip=True)
                    if re.fullmatch(r"[A-Za-z]{3}\s*/?\s*\d{1,2}|\d{1,2}/\d{1,2}", candidate):
                        date = candidate
                        break
            results[name] = (
                float(value),
                source_metadata(BONDS, date or "Observation date not shown"),
            )
    return results


@st.cache_data(ttl=3600, show_spinner=False)
def load_te_country_bond(name):
    slug = name.lower().replace(" ", "-")
    url = f"https://tradingeconomics.com/{slug}/government-bond-yield"
    soup = BeautifulSoup(get(url).text, "html.parser")
    text = soup.get_text(" ", strip=True)
    if not re.search(r"10.Year Government Bond Yield|10Y Bond Yield", text, re.I):
        raise ValueError("10-year series was not found.")
    summary = re.search(
        r"\bto\s+(-?\d+(?:\.\d+)?)\s*%\s+on\s+"
        r"([A-Za-z]+\s+\d{1,2},\s+20\d{2})", text, re.I,
    )
    quote = re.search(r"\bActual\s+(-?\d+(?:\.\d+)?)\s*%?\s+Daily Change", text, re.I)
    if quote:
        return float(quote[1]), source_metadata(
            url, summary[2] if summary else "Observation date not shown"
        )
    if summary:
        return float(summary[1]), source_metadata(url, summary[2])
    raise ValueError("Trading Economics quote was not found.")


def load(label, fetch, fallback):
    try:
        return fetch()
    except (requests.RequestException, ValueError, KeyError, ImportError) as exc:
        st.warning(f"{label} unavailable ({type(exc).__name__}).")
        return fallback


@st.cache_data(ttl=3600, show_spinner=False)
def load_fred_bond(code):
    legacy_codes = {
        "AUS": "AU", "CAN": "CA", "DEU": "DE", "FRA": "FR",
        "GBR": "GB", "ITA": "IT", "JPN": "JP",
    }
    if code == "USA":
        series = "DGS10"
    elif code in legacy_codes:
        series = f"IRLTLT01{legacy_codes[code]}M156N"
    else:
        series = f"{code}IRLTLT01STM"
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    response = get(url, params={"id": series})
    rows = csv.reader(StringIO(response.text))
    header = next(rows, [])
    if len(header) != 2 or header[1].strip() != series:
        raise ValueError("FRED series missing.")
    for date, value in reversed([row for row in rows if len(row) == 2]):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) and re.fullmatch(r"-?\d+(?:\.\d+)?", value):
            frequency = "daily" if code == "USA" else "monthly"
            observation = date if code == "USA" else date[:7]
            return float(value), source_metadata(
                f"https://fred.stlouisfed.org/series/{series}", observation, frequency
            )
    raise ValueError("FRED yield observation missing.")


@st.cache_data(ttl=900, show_spinner=False)
def try_te_bond_table():
    try:
        return load_te_bond_table()
    except (requests.RequestException, ValueError):
        return {}


@st.cache_data(ttl=900, show_spinner=False)
def load_government_yield(name, code):
    quote = try_te_bond_table().get(name.casefold())
    meta = normalize_source(quote[1]) if quote else None
    if meta and meta["date"] != "Observation date not shown":
        return quote, "Trading Economics"
    try:
        return load_te_country_bond(name), "Trading Economics"
    except (requests.RequestException, ValueError):
        if quote:
            return quote, "Trading Economics"
    try:
        return load_fred_bond(code), "FRED"
    except (requests.RequestException, ValueError):
        return (None, None), "Manual"


def rate_input(label, data, key, minimum=None, maximum=None):
    value, _ = data
    override = value is None or st.checkbox(f"Override {label}", key=f"override_{key}")
    if override:
        return st.number_input(
            label, value=value, min_value=minimum, max_value=maximum,
            step=0.01, format="%.4f", key=f"value_{key}"
        ), "Manual"
    if value is not None:
        st.write(f"{label}: **{value:.4f}%**" if not key.startswith("beta_") else f"{label}: **{value:.4f}**")
    return value, "Source" if value is not None else "Missing"


def calculate(beta, erp, share, tax, us_yield, local_yield, spread, local_override=None):
    levered = beta * (1 + (1 - tax) * share / (1 - share))
    factor = (1 + local_yield) / (1 + us_yield)
    local_spread = spread * factor if local_override is None else local_override
    eq_us = us_yield + levered * erp
    eq_local = (1 + eq_us) * factor - 1
    debt_us, debt_local = us_yield + spread, local_yield + local_spread
    after_us, after_local = debt_us * (1 - tax), debt_local * (1 - tax)
    return {
        "Cost of equity": (eq_us, eq_local),
        "Pretax cost of debt": (debt_us, debt_local),
        "After-tax cost of debt": (after_us, after_local),
        "WACC": ((1 - share) * eq_us + share * after_us,
                 (1 - share) * eq_local + share * after_local),
    }, local_spread, levered


def main():
    st.set_page_config(page_title="International Cost of Capital", layout="wide")
    st.title("International Cost of Capital")
    if st.button("Refresh source data"):
        for fetch in (
            load_industry_betas, load_usd_bbb_spread, load_statutory_tax_rate,
            load_te_bond_table, load_te_country_bond, try_te_bond_table,
            load_fred_bond, load_government_yield,
        ):
            fetch.clear()

    industries, beta_meta = load("Industry betas", load_industry_betas, ({}, None))
    options = sorted(set(industries) | {DEFAULT_INDUSTRY})
    left, right = st.columns(2)
    with left:
        industry = st.selectbox(
            "Industry", options, index=options.index(DEFAULT_INDUSTRY)
        )
        country_names = sorted(COUNTRIES)
        country = st.selectbox(
            "Target country", country_names, index=country_names.index("United States")
        )
        territory = COUNTRIES[country]
        currencies = get_territory_currencies(territory.alpha_2)
        currency = st.selectbox("Local currency", currencies) if len(currencies) > 1 else currencies[0]
        erp = st.number_input("Equity risk premium (%)", value=6.5, step=0.1) / 100
        share = st.number_input(
            "Target debt share (%)", min_value=0.0, max_value=99.99, value=30.0, step=0.1
        ) / 100

    with right:
        tax_choice = st.radio("Project tax rate", ["U.S. rate", "Target country rate"])
        tax_code = "USA" if tax_choice == "U.S. rate" else territory.alpha_3
        tax_data = load("OECD tax rate", lambda: load_statutory_tax_rate(tax_code), (None, None))
        tax, tax_mode = rate_input(
            "Project corporate tax rate (%)", tax_data, f"tax_{tax_code}", 0.0, 100.0
        )
        beta, beta_mode = rate_input(
            "Unlevered beta", (industries.get(industry), beta_meta), f"beta_{industry}"
        )
        spread_data = load("USD BBB spread", load_usd_bbb_spread, (None, None))
        spread, spread_mode = rate_input("USD BBB spread (%)", spread_data, "spread")

    st.subheader("10-year government yields")
    us_market, us_source = load_government_yield("United States", "USA")
    if country == "United States":
        local_market, local_source = us_market, us_source
    else:
        local_market, local_source = load_government_yield(country, territory.alpha_3)

    if "FRED" in (us_source, local_source):
        st.info("FRED fallback is in use. Check observation dates and frequency below.")
    if "Manual" in (us_source, local_source):
        st.warning("A 10-year government yield is unavailable. Enter it manually.")

    left, right = st.columns(2)
    with left:
        us, us_mode = rate_input("U.S. yield (%)", us_market, "us_yield", -99.99)
    with right:
        if country == "United States":
            local, local_mode = us, us_mode
            st.caption("Both USD columns use the same U.S. yield.")
        else:
            local, local_mode = rate_input(
                "Target country yield (%)", local_market,
                f"local_yield_{territory.alpha_3}", -99.99
            )
        local_override = None
        use_override = st.checkbox("Override local BBB spread", key="local_spread_switch")
        if use_override:
            suggested = spread * (100 + local) / (100 + us) if None not in (spread, us, local) else None
            local_override = st.number_input(
                "Manual local BBB spread (%)", value=suggested, step=0.01, format="%.4f",
                key=f"spread_{territory.alpha_3}"
            )

    with st.expander("Data sources and assumptions"):
        for label, mode, meta in (
            ("Industry betas", beta_mode, beta_meta),
            ("Project tax rate", tax_mode, tax_data[1]),
            ("USD Baa2/BBB spread", spread_mode, spread_data[1]),
            ("U.S. government yield", us_mode, us_market[1]),
            ("Target government yield", local_mode, local_market[1]),
        ):
            source = normalize_source(meta)
            if source:
                date = source["date"]
                frequency = source.get("frequency")
                if frequency:
                    date = f"{date} · {frequency}"
                st.markdown(
                    f"**{label}** · {mode} · [{date}]({source['url']})"
                    f" · Retrieved {source['retrieved']}"
                )
            else:
                st.write(f"{label} · {mode}")
        st.write("The 10-year government yields proxy for risk-free rates. Both currencies "
                 "use the same project tax rate, target debt share and equity risk premium. "
                 "USD equity cost uses CAPM. Local equity cost converts the gross USD equity "
                 "rate using (1 + local yield) / (1 + U.S. yield). This factor also converts "
                 "the USD credit spread unless a local spread is entered. Debt cost equals "
                 "government yield plus credit spread. WACC uses debt cost after tax.")

    if None in (beta, tax, spread, us, local) or (use_override and local_override is None):
        st.info("Enter the missing inputs to view results and download the comparison.")
        return

    assumptions = (
        erp, share, tax / 100, us / 100, local / 100, spread / 100,
        None if local_override is None else local_override / 100,
    )
    results, active_spread, levered = calculate(beta, *assumptions)
    st.subheader("Results")
    st.write(f"Levered beta: **{levered:.4f}** · "
             f"{'Manual' if use_override else 'Calculated'} local BBB spread: **{active_spread:.4%}**")
    usd_label, local_label = "USD (U.S. basis)", f"{currency} ({country} basis)"
    st.table(pd.DataFrame(
        [(metric, f"{values[0]:.4%}", f"{values[1]:.4%}") for metric, values in results.items()],
        columns=["Metric", usd_label, local_label]
    ).set_index("Metric"))

    st.subheader("All-industry comparison")
    industry_betas = {**industries, industry: beta}
    comparison = []
    for name, value in sorted(industry_betas.items()):
        metrics, _, _ = calculate(value, *assumptions)
        row = {"Industry": name, "Unlevered beta": value}
        for metric, (usd_value, local_value) in metrics.items():
            row[f"{metric} | {usd_label} (%)"] = round(usd_value * 100, 6)
            row[f"{metric} | {local_label} (%)"] = round(local_value * 100, 6)
        comparison.append(row)

    frame = pd.DataFrame(comparison)
    st.dataframe(frame, hide_index=True)
    st.download_button(
        "Download results (CSV)", frame.to_csv(index=False),
        "cost_of_capital_by_industry.csv", "text/csv"
    )


if __name__ == "__main__":
    main()
