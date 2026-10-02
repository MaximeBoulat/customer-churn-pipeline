"""Start one AWS pipeline execution; local Python does not train the model."""
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config


def main():
    session = boto3.Session(profile_name=config.AWS_PROFILE, region_name=config.REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != config.LAB_ACCOUNT_ID:
        raise SystemExit(f"Expected lab account {config.LAB_ACCOUNT_ID}, got {account}")
    response = session.client("sagemaker").start_pipeline_execution(PipelineName=config.PIPELINE_NAME)
    print(response["PipelineExecutionArn"])
    print("Execution started in AWS. Follow it in SageMaker Studio → Pipelines.")


if __name__ == "__main__":
    main()
