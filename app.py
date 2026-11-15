import streamlit as st

st.title("International Cost of Capital")

st.selectbox("Industry", ["Software (System & Application)"])
st.selectbox("Target country", ["United States"])
st.radio("Project corporate tax rate", ["U.S. tax rate", "Target country tax rate"])

st.number_input("Equity risk premium (%)", value=6.5, step=0.1)
st.number_input(
    "Target debt share (%)",
    min_value=0.0,
    max_value=100.0,
    value=30.0,
    step=1.0,
)
import streamlit as st

st.title("International Cost of Capital")
st.caption("Manual verification with the Colombia example from class")

st.subheader("Project assumptions")
beta_unlevered = st.number_input("Unlevered beta", value=0.80, step=0.01)
erp = st.number_input("Equity risk premium (%)", value=6.50, step=0.10) / 100
debt_share = st.number_input(
    "Target debt share (%)", min_value=0.0, max_value=99.99, value=30.0
) / 100
tax_rate = st.number_input(
    "Project corporate tax rate (%)", min_value=0.0, max_value=100.0, value=35.0
) / 100

st.subheader("Market inputs")
usd, cop = st.columns(2)

with usd:
    yield_usd = st.number_input("U.S. 10-year yield (%)", value=4.0) / 100
    spread_usd = st.number_input("USD BBB spread (%)", value=1.50) / 100

with cop:
    yield_cop = st.number_input("Colombia 10-year yield (%)", value=12.0) / 100

if min(yield_usd, yield_cop) <= -1:
    st.error("Enter yields greater than -100%.")
    st.stop()

equity_share = 1 - debt_share
debt_to_equity = debt_share / equity_share
beta_levered = beta_unlevered * (1 + (1 - tax_rate) * debt_to_equity)
conversion_factor = (1 + yield_cop) / (1 + yield_usd)

with cop:
    spread_cop = spread_usd * conversion_factor
    if st.checkbox("Override COP BBB spread"):
        spread_cop = st.number_input(
            "Manual COP BBB spread (%)", value=round(spread_cop * 100, 4),
            format="%.4f"
        ) / 100
    else:
        st.write(f"Calculated COP BBB spread: {spread_cop:.4%}")

equity_usd = yield_usd + beta_levered * erp
equity_cop = (1 + equity_usd) * conversion_factor - 1

debt_usd = yield_usd + spread_usd
debt_cop = yield_cop + spread_cop

wacc_usd = equity_share * equity_usd + debt_share * debt_usd * (1 - tax_rate)
wacc_cop = equity_share * equity_cop + debt_share * debt_cop * (1 - tax_rate)

st.subheader("Results")
st.table({
    "Metric": ["Cost of equity", "Pretax cost of debt", "WACC"],
    "USD": [f"{equity_usd:.4%}", f"{debt_usd:.4%}", f"{wacc_usd:.4%}"],
    "COP": [f"{equity_cop:.4%}", f"{debt_cop:.4%}", f"{wacc_cop:.4%}"],
})
