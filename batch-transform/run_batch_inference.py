"""Run System 2 using the approved model's pinned artifacts (reference: team PR #54)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlparse
import uuid

import boto3
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from pipeline import preprocess


def location(uri):
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError(f"Expected an S3 object URI: {uri}")
    if parsed.query or parsed.fragment:
        raise ValueError("Use an S3 URI without a query or fragment")
    return {"Bucket": parsed.netloc, "Key": parsed.path.lstrip("/")}


def read(s3, uri):
    return s3.get_object(**location(uri))["Body"].read()


def put(s3, prefix, name, body, content_type):
    s3.put_object(Bucket=config.OUTPUT_BUCKET, Key=f"{prefix}/{name}", Body=body,
                  ContentType=content_type, ServerSideEncryption="AES256", IfNoneMatch="*")


def load_approved(s3, sm, uri):
    contract = json.loads(read(s3, uri))
    if contract.get("format_version") != 1:
        raise ValueError("Unsupported approval contract format")
    arn = contract["model_package_arn"]
    expected = f"arn:aws:sagemaker:{config.REGION}:{config.LAB_ACCOUNT_ID}:model-package/{config.PROJECT_PREFIX}-models/"
    if not arn.startswith(expected):
        raise ValueError("Select a model from the configured personal model package group")
    package = sm.describe_model_package(ModelPackageName=arn)
    if package["ModelApprovalStatus"] != "Approved" or package["ModelPackageStatus"] != "Completed":
        raise ValueError("The pinned package must still be Completed and Approved")
    containers = package["InferenceSpecification"]["Containers"]
    if len(containers) != 1:
        raise ValueError("Expected a single-container model package")
    container = containers[0]
    if (container["ModelDataUrl"] != contract["model_artifact"]["source_uri"]
            or container["Image"] != contract["inference_image"]):
        raise ValueError("The pinned model no longer matches its registered package")
    fitted = json.loads(read(s3, contract["preprocessing"]["uri"]))
    if contract["recalibration_factor"] != 1.0 or fitted["recalibration_factor"] != 1.0:
        raise ValueError("Batch inference does not apply probability recalibration")
    evaluation = json.loads(read(s3, contract["evaluation"]["uri"]))
    threshold = evaluation.get("threshold", {})
    required = {"value", "prevalence", "contact_nobody", "outreach_policy", "top_k"}
    if not required.issubset(threshold) or not np.isfinite(threshold["value"]):
        raise ValueError("Pinned evaluation lacks a selected cutoff. Run the updated pipeline and approve/pin that new execution first.")
    return contract, fitted, threshold


def read_holdout(s3):
    uri = config.CURATED_LOCATION + "split=holdout/"
    loc = location(uri)
    frames = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=loc["Bucket"], Prefix=loc["Key"]):
        for obj in page.get("Contents", []):
            if obj["Size"] and not obj["Key"].endswith("/"):
                body = s3.get_object(Bucket=loc["Bucket"], Key=obj["Key"])["Body"].read()
                frames.append(pd.read_parquet(io.BytesIO(body)))
    if not frames:
        raise ValueError(f"No curated holdout data at {uri}")
    return pd.concat(frames, ignore_index=True).sort_values("customerid").reset_index(drop=True), uri


def prepare(frame, contract, fitted):
    missing = sorted(set(contract["input_schema"]) - set(frame.columns))
    if missing:
        raise ValueError(f"Holdout is missing required columns: {missing}")
    ids = frame.customerid.reset_index(drop=True)
    if ids.isna().any() or ids.duplicated().any():
        raise ValueError("Holdout customer IDs must be present and unique")
    # The same transform as training; no fit, new split or label is needed.
    features = preprocess.transform(frame, fitted).reset_index(drop=True)
    if list(features.columns) != fitted["feature_names"] or not np.isfinite(features.to_numpy()).all():
        raise ValueError("Prepared features must match the pinned order and contain finite values")
    return ids, features


def run_transform(sm, contract, role_arn, name, input_uri, output_uri):
    sm.create_model(ModelName=name, ExecutionRoleArn=role_arn,
                    PrimaryContainer={"Image": contract["inference_image"],
                                      "ModelDataUrl": contract["model_artifact"]["uri"]})
    started = False
    finished = False
    try:
        sm.create_transform_job(
            TransformJobName=name, ModelName=name, MaxPayloadInMB=6, BatchStrategy="MultiRecord",
            TransformInput={"DataSource": {"S3DataSource": {"S3DataType": "S3Prefix", "S3Uri": input_uri}},
                            "ContentType": "text/csv", "SplitType": "Line", "CompressionType": "None"},
            TransformOutput={"S3OutputPath": output_uri, "Accept": "text/csv", "AssembleWith": "Line"},
            TransformResources={"InstanceType": config.INSTANCE_TYPE, "InstanceCount": 1})
        started = True
        print(f"Batch Transform job: {name}", flush=True)
        deadline = time.monotonic() + 1800
        while True:
            job = sm.describe_transform_job(TransformJobName=name)
            status = job["TransformJobStatus"]
            if status in {"Completed", "Failed", "Stopped"}:
                finished = True
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Batch Transform job {name} exceeded 30 minutes")
            time.sleep(15)
        if status != "Completed":
            raise RuntimeError(f"Batch Transform {status}: {job.get('FailureReason', '')}")
    finally:
        if started and not finished:
            try:
                sm.stop_transform_job(TransformJobName=name)
            except Exception as exc:
                print(f"Could not stop {name}: {exc}", file=sys.stderr)
        try:
            sm.delete_model(ModelName=name)
        except Exception as exc:
            print(f"Could not delete temporary model {name}: {exc}", file=sys.stderr)
    return output_uri.rstrip("/") + "/input.csv.out"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract-uri", required=True, help="approved_contract.json URI printed by approval")
    args = parser.parse_args()
    session = boto3.Session(profile_name=config.AWS_PROFILE, region_name=config.REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != config.LAB_ACCOUNT_ID:
        raise ValueError("AWS profile is not using the configured Learner Lab account")
    s3, sm = session.client("s3"), session.client("sagemaker")
    contract, fitted, threshold = load_approved(s3, sm, args.contract_uri)
    holdout, input_uri = read_holdout(s3)
    ids, features = prepare(holdout, contract, fitted)
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    prefix = f"inference/cell2cell/{run_id}"
    staging = f"{prefix}/_transform"
    put(s3, staging, "input.csv", features.to_csv(header=False, index=False).encode(), "text/csv")
    role = f"arn:aws:iam::{config.LAB_ACCOUNT_ID}:role/{config.EXECUTION_ROLE_NAME}"
    name = f"{config.PROJECT_PREFIX}-batch-{run_id.lower()}"
    output_uri = run_transform(sm, contract, role, name,
        f"s3://{config.OUTPUT_BUCKET}/{staging}/input.csv", f"s3://{config.OUTPUT_BUCKET}/{staging}/")
    lines = [line for line in read(s3, output_uri).decode().splitlines() if line.strip()]
    probabilities = np.array([float(line) for line in lines])
    if (len(probabilities) != len(ids) or not np.isfinite(probabilities).all()
            or ((probabilities < 0) | (probabilities > 1)).any()):
        raise ValueError("Batch predictions must contain one probability in [0, 1] per customer")
    group, version = contract["model_package_arn"].rsplit("/", 2)[-2:]
    scores = pd.DataFrame({
        "customerid": ids, "churn_probability": probabilities,
        "outreach_flag": (probabilities >= threshold["value"]).astype(int),
        "model_version": f"{group}/{version}",
        "preprocessing_version": str(contract["preprocessing_version"]), "scored_at": now.isoformat()})
    put(s3, prefix, "scores.csv", scores.to_csv(index=False).encode(), "text/csv")
    prepared = pd.concat([ids.rename("customerid"), features], axis=1)
    put(s3, prefix, "features.csv", prepared.to_csv(index=False).encode(), "text/csv")
    manifest = {
        "schema_version": 1, "run_id": run_id, "scored_at": now.isoformat(),
        "rows": len(scores), "flagged": int(scores.outreach_flag.sum()),
        "model_package_arn": contract["model_package_arn"], "model_version": f"{group}/{version}",
        "execution_id": contract["pipeline_execution_arn"].rsplit("/", 1)[-1],
        "threshold": {k: threshold[k] for k in ("value", "prevalence", "contact_nobody", "outreach_policy", "top_k")},
        "approved_contract_uri": args.contract_uri, "preprocessing_version": str(contract["preprocessing_version"]),
        "input_uri": input_uri, "transform_job_name": name}
    # Written last: downstream consumers only accept runs with a manifest.
    put(s3, prefix, "manifest.json", json.dumps(manifest, indent=2, allow_nan=False).encode(), "application/json")
    print(f"Scored {len(scores)} customers; flagged {manifest['flagged']}.")
    print(f"s3://{config.OUTPUT_BUCKET}/{prefix}/")


if __name__ == "__main__":
    main()
