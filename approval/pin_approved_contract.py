"""Approve and pin the model registered by one reviewed pipeline execution."""
import argparse
import json
from pathlib import Path
import sys

import boto3
from approved_contract import pin

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-arn", required=True)
    parser.add_argument("--approve", action="store_true",
                        help="Approve the reviewed pending package; omit if already Approved")
    parser.add_argument("--note", default="", help="Optional approval note")
    args = parser.parse_args()
    prefix = (f"arn:aws:sagemaker:{config.REGION}:{config.LAB_ACCOUNT_ID}:"
              f"pipeline/{config.PIPELINE_NAME}/execution/")
    if not args.execution_arn.startswith(prefix) or not args.execution_arn[len(prefix):]:
        parser.error("Use an execution ARN from the configured personal pipeline, account and region")
    session = boto3.Session(profile_name=config.AWS_PROFILE, region_name=config.REGION)
    result = pin(session.client("sagemaker"), session.client("s3"),
                 execution_arn=args.execution_arn, approve=args.approve, note=args.note)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
