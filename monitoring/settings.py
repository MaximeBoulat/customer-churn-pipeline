"""Monitoring settings from the same exported .env values Terraform reads."""
import os
import config

PSI_INVESTIGATE = float(os.environ["TF_VAR_monitoring_psi_threshold"])
MIN_FLAGGED = int(os.environ["TF_VAR_monitoring_min_flagged"])
NAMESPACE = f"{config.PROJECT_PREFIX}/ChurnMonitoring"
MODEL_NAME = config.PROJECT_PREFIX
