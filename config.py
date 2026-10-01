"""Read the environment explicitly loaded by the terminal from .env."""
import os

AWS_PROFILE = os.environ["AWS_PROFILE"]
REGION = os.environ["TF_VAR_region"]
LAB_ACCOUNT_ID = os.environ["TF_VAR_lab_account_id"]
PROJECT_PREFIX = os.environ["TF_VAR_project_prefix"]
RAW_S3_URI = os.environ["TF_VAR_raw_s3_uri"]
OUTPUT_BUCKET = os.environ["TF_VAR_output_bucket"]
CURATED_VERSION = os.environ["TF_VAR_curated_version"]

DATABASE = PROJECT_PREFIX.replace("-", "_")
WORKGROUP = PROJECT_PREFIX
CURATED_LOCATION = f"s3://{OUTPUT_BUCKET}/curated/cell2cell/{CURATED_VERSION}/"
