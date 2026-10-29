import streamlit as st
import snowflake.connector

conn = snowflake.connector.connect(warehouse="WH_BI", database="ANALYTICS", role="INSIGHTS_APP")
st.title("Customer insights")
sales = conn.cursor().execute("SELECT * FROM ANALYTICS.DAILY_SALES").fetch_pandas_all()
features = conn.cursor().execute("SELECT * FROM ANALYTICS.CUSTOMER_FEATURES").fetch_pandas_all()
