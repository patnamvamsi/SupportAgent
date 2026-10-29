from airflow import DAG
from airflow.providers.snowflake.operators.snowflake import SnowflakeOperator

with DAG("daily_sales", schedule="0 3 * * *") as dag:
    SnowflakeOperator(task_id="run_task", sql="EXECUTE TASK ANALYTICS.TASK_BUILD_DAILY_SALES")
