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
