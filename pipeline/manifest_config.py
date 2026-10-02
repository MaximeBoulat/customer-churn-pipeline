"""Manifest settings passed into Processing jobs by pipeline/definition.tf."""
import os

import boto3

BUCKET_NAME = os.environ["BUCKET_NAME"]
LAB_REGION = os.environ["LAB_REGION"]
PIPELINE_PREFIX = "pipeline/"
CURATED_VERSION = os.environ["CURATED_VERSION"]
PREPROCESSING_CONTRACT = os.environ["PREPROCESSING_CONTRACT"]
FEATURE_VERSION = os.environ["FEATURE_VERSION"]
MODEL_VERSION = os.environ["MODEL_VERSION"]


def s3_client():
    return boto3.client("s3", region_name=LAB_REGION)
