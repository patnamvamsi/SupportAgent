from pyspark.sql import SparkSession

spark = SparkSession.builder.appName("batch-features").getOrCreate()
clicks = spark.read.format("snowflake").option("dbtable", "RAW.CLICKSTREAM").load()
customers = spark.read.format("snowflake").option("dbtable", "RAW.CUSTOMERS").load()
features = clicks.join(customers, "customer_id")
features.write.format("snowflake").option("dbtable", "ANALYTICS.CUSTOMER_FEATURES").mode("overwrite").save()
