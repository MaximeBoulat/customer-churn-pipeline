"""Ingest all three processed splits into an offline Feature Group."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

import boto3
from botocore.config import Config
import pandas as pd


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--feature-group", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--execution-id", required=True)
    args = parser.parse_args()
    root = Path("/opt/ml/processing/input")
    contract = json.loads((root / "contract" / "preprocessing_parameters.json").read_text())
    features = contract["feature_names"]
    sm = boto3.client("sagemaker", region_name=args.region)
    deadline = time.monotonic() + 600
    while True:
        group = sm.describe_feature_group(FeatureGroupName=args.feature_group)
        if group["FeatureGroupStatus"] == "Created":
            break
        if group["FeatureGroupStatus"] != "Creating" or time.monotonic() > deadline:
            raise RuntimeError(f"Feature Group not ready: {group}")
        time.sleep(10)
    actual = {f["FeatureName"]: f["FeatureType"] for f in group["FeatureDefinitions"]}
    expected = {name: "Fractional" for name in features}
    expected.update(customerid="Integral", churn_label="Integral", split_name="String", event_time="Fractional", execution_id="String")
    if actual != expected:
        raise ValueError("Feature Group schema differs from preprocessing. Regenerate schema, bump FEATURE_VERSION and apply Terraform before retrying.")

    runtime = boto3.client("sagemaker-featurestore-runtime", region_name=args.region,
        config=Config(retries={"mode": "adaptive", "max_attempts": 10}, max_pool_connections=8))
    event_time = str(time.time())

    def put(record):
        # The service's schema cache can lag a newly created group briefly.
        for attempt in range(5):
            try:
                runtime.put_record(FeatureGroupName=args.feature_group, Record=record, TargetStores=["OfflineStore"])
                return
            except runtime.exceptions.ValidationError:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)

    counts = {}
    for split in ("train", "validation", "test"):
        table = pd.read_csv(root / split / f"{split}.csv", header=None)
        ids = pd.read_csv(root / "identifiers" / f"{split}.csv", header=None).iloc[:, 0]
        if len(ids) != len(table) or table.shape[1] != len(features) + 1:
            raise ValueError(f"Feature/identifier shape mismatch for {split}")

        def records():
            for customer, values in zip(ids, table.itertuples(index=False, name=None)):
                record = {"customerid": str(int(customer)), "churn_label": str(int(values[0])),
                    "split_name": split, "event_time": event_time, "execution_id": args.execution_id}
                record.update({name: str(float(value)) for name, value in zip(features, values[1:])})
                yield [{"FeatureName": name, "ValueAsString": value} for name, value in record.items()]

        with ThreadPoolExecutor(max_workers=8) as pool:
            for _ in pool.map(put, records()):
                pass  # Exceptions propagate; training must not follow partial ingestion.
        counts[split] = len(table)
        print(f"{split}: {len(table)} records accepted", flush=True)
    out = Path("/opt/ml/processing/output")
    out.mkdir(parents=True, exist_ok=True)
    (out / "ingestion.json").write_text(json.dumps({
        "feature_group": args.feature_group, "execution_id": args.execution_id,
        "records_accepted": counts, "event_time": event_time,
        "offline_location": group["OfflineStoreConfig"]["S3StorageConfig"].get("ResolvedOutputS3Uri"),
        "note": "PutRecord accepted these records. Offline S3 delivery is asynchronous."
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
