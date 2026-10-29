from airflow import DAG
from airflow.providers.amazon.aws.operators.emr import EmrAddStepsOperator

with DAG("customer_features", schedule="0 2 * * *") as dag:
    EmrAddStepsOperator(task_id="run_batch_features", job_flow_id="emr-batch", steps=[{"Name": "batch-features"}])
