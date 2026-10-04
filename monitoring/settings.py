"""Map the personal repo's exported settings to PR #56's monitoring configuration."""
import os
import boto3
import config

BUCKET_NAME = config.OUTPUT_BUCKET
LAB_REGION = config.REGION
CURATED_VERSION = config.CURATED_VERSION
FEATURE_VERSION = os.environ["TF_VAR_feature_version"]
PREPROCESSING_CONTRACT = os.environ["TF_VAR_preprocessing_contract"]
MODEL_VERSION = os.environ["TF_VAR_model_version"]
MODEL_PACKAGE_GROUP = f"{config.PROJECT_PREFIX}-models"
MONITORING_PREFIX = "monitoring"
INFERENCE_PREFIX = "inference"
MODELS_PREFIX = "models"
MONITORING_PSI_INVESTIGATE = float(os.environ["TF_VAR_monitoring_psi_threshold"])
MONITORING_MIN_FLAGGED_FOR_BIAS = int(os.environ["TF_VAR_monitoring_min_flagged"])
MONITORING_BIAS_ADVANTAGED = os.environ["MONITORING_BIAS_ADVANTAGED"]
MONITORING_NAMESPACE = f"{config.PROJECT_PREFIX}/ChurnMonitoring"
MONITORING_MODEL_NAME = config.PROJECT_PREFIX


def dataset_path(prefix, version):
    return f"{prefix}/cell2cell/{version}"


def session():
    return boto3.Session(profile_name=config.AWS_PROFILE, region_name=LAB_REGION)


def s3_client():
    return session().client("s3")
