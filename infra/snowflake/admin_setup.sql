-- Snowflake objects and grants for QuantAI, applied by hand in Snowsight as ACCOUNTADMIN (Run all).
-- They were run in several batches while the setup was built; this file records the same statements in
-- one place so the setup can be reviewed and rebuilt. Values in angle brackets are filled locally and
-- never committed:
--   <rsa-public-key>   body of the dbt service user's public key (the private key stays outside the repo)
--   <aws-account-id>   the AWS account that owns the data lake
--   <data-bucket>      the data lake bucket
-- Order with Terraform: infra/terraform/snowflake.tf creates the IAM role first with a placeholder trust;
-- after this script, DESC INTEGRATION QUANTAI_S3_RAW gives the IAM user and external ID for the second apply.

USE ROLE ACCOUNTADMIN;

-- Compute and the cost cap. Resource monitors cap warehouse credits only.
CREATE WAREHOUSE IF NOT EXISTS QUANTAI_WH
  WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE;
CREATE RESOURCE MONITOR IF NOT EXISTS QUANTAI_RM
  WITH CREDIT_QUOTA = 20 FREQUENCY = MONTHLY START_TIMESTAMP = IMMEDIATELY
  TRIGGERS ON 80 PERCENT DO NOTIFY ON 100 PERCENT DO SUSPEND;
ALTER WAREHOUSE QUANTAI_WH SET RESOURCE_MONITOR = QUANTAI_RM;
-- Warehouses the trial account created on its own go under the same cap.
ALTER WAREHOUSE IF EXISTS COMPUTE_WH SET AUTO_SUSPEND = 60 RESOURCE_MONITOR = QUANTAI_RM;
ALTER WAREHOUSE IF EXISTS SNOWFLAKE_LEARNING_WH SET AUTO_SUSPEND = 60 RESOURCE_MONITOR = QUANTAI_RM;
ALTER WAREHOUSE IF EXISTS SYSTEM$STREAMLIT_NOTEBOOK_WH SET RESOURCE_MONITOR = QUANTAI_RM;

-- Database, role and key-pair service user for dbt. The role owns only the schemas it creates.
CREATE DATABASE IF NOT EXISTS QUANTAI;
CREATE ROLE IF NOT EXISTS QUANTAI_DBT;
GRANT USAGE ON WAREHOUSE QUANTAI_WH TO ROLE QUANTAI_DBT;
GRANT USAGE, CREATE SCHEMA ON DATABASE QUANTAI TO ROLE QUANTAI_DBT;
CREATE USER IF NOT EXISTS QUANTAI_DBT_USER
  TYPE = SERVICE DEFAULT_ROLE = QUANTAI_DBT DEFAULT_WAREHOUSE = QUANTAI_WH
  RSA_PUBLIC_KEY = '<rsa-public-key>';
GRANT ROLE QUANTAI_DBT TO USER QUANTAI_DBT_USER;
-- Lets the service role read the monitor's used credits.
GRANT MONITOR ON RESOURCE MONITOR QUANTAI_RM TO ROLE QUANTAI_DBT;

-- Read-only access to the lake's raw/ prefix through the Terraform role.
CREATE STORAGE INTEGRATION IF NOT EXISTS QUANTAI_S3_RAW
  TYPE = EXTERNAL_STAGE
  STORAGE_PROVIDER = 'S3'
  ENABLED = TRUE
  STORAGE_AWS_ROLE_ARN = 'arn:aws:iam::<aws-account-id>:role/quantai-snowflake-raw-reader'
  STORAGE_ALLOWED_LOCATIONS = ('s3://<data-bucket>/raw/')
  COMMENT = 'QuantAI: read-only access to the data lake raw/ prefix';
GRANT USAGE ON INTEGRATION QUANTAI_S3_RAW TO ROLE QUANTAI_DBT;

-- Trial defaults that every role inherits through PUBLIC. Compute pools and Cortex bill outside any
-- resource monitor, and the learning role reaches another warehouse, so the dbt role loses all of them.
REVOKE ALL PRIVILEGES ON WAREHOUSE SNOWFLAKE_LEARNING_WH FROM ROLE PUBLIC;
REVOKE ALL PRIVILEGES ON WAREHOUSE SYSTEM$STREAMLIT_NOTEBOOK_WH FROM ROLE PUBLIC;
REVOKE ROLE SNOWFLAKE_LEARNING_ROLE FROM ROLE PUBLIC;
REVOKE USAGE ON COMPUTE POOL SYSTEM_COMPUTE_POOL_CPU FROM ROLE PUBLIC;
REVOKE USAGE ON COMPUTE POOL SYSTEM_COMPUTE_POOL_GPU FROM ROLE PUBLIC;
REVOKE DATABASE ROLE SNOWFLAKE.CORTEX_USER FROM ROLE PUBLIC;
REVOKE USE AI FUNCTIONS ON ACCOUNT FROM ROLE PUBLIC;
REVOKE EXECUTE AGENT TASK ON ACCOUNT FROM ROLE PUBLIC;
