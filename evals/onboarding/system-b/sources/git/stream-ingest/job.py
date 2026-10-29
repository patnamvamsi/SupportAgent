from pyspark.sql import SparkSession

spark = SparkSession.builder.appName("stream-ingest").getOrCreate()
events = (
    spark.readStream.format("kafka")
    .option("kafka.bootstrap.servers", "b-1.analytics-msk.kafka.us-east-1.amazonaws.com:9098")
    .option("subscribe", "events.clickstream")
    .option("kafka.group.id", "stream-ingest")
    .load()
)
(events.writeStream.format("snowflake")
    .option("sfDatabase", "RAW").option("dbtable", "CLICKSTREAM").option("sfWarehouse", "WH_LOAD")
    .option("checkpointLocation", "s3://acme-analytics/checkpoints/stream-ingest")
    .start())
