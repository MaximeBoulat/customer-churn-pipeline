"""Measure one completed batch and publish its monitoring report and metrics."""
from __future__ import annotations
import argparse
import io
import json
import math
from pathlib import Path
import sys

import boto3
import numpy as np
import pandas as pd
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from pipeline.preprocess import feature_name
import metrics as mon
import settings


def read(s3, uri):
    from urllib.parse import urlparse
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Expected an S3 URI: {uri}")
    return s3.get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))["Body"].read()


def write(s3, key, payload, create=False):
    s3.put_object(Bucket=config.OUTPUT_BUCKET, Key=key,
        Body=json.dumps(mon._json_safe(payload), indent=2, allow_nan=False).encode(),
        ContentType="application/json", ServerSideEncryption="AES256",
        **({"IfNoneMatch": "*"} if create else {}))


def frame(s3, uri, **kwargs):
    return pd.read_csv(io.BytesIO(read(s3, uri)), **kwargs)


def model_baseline(s3, manifest, fitted):
    execution = manifest["execution_id"]
    key = f"monitoring/cell2cell/baselines/{execution}/baseline.json"
    uri = f"s3://{config.OUTPUT_BUCKET}/{key}"
    try:
        payload = json.loads(read(s3, uri))
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
            raise
        train_uri = f"s3://{config.OUTPUT_BUCKET}/pipeline/{execution}/process/train/train.csv"
        train = frame(s3, train_uri, header=None)
        if train.shape[1] != len(fitted["feature_names"]) + 1:
            raise ValueError("Training CSV does not match pinned feature order")
        train.columns = ["churn_label", *fitted["feature_names"]]
        baseline = mon.fit_baseline(train, curated_version=fitted.get("curated_version"),
            preprocessing_contract=manifest["preprocessing_version"], model_version=manifest["model_version"])
        payload = {"execution_id": execution, "model_package_arn": manifest["model_package_arn"],
                   "reference_uri": train_uri, "baseline": baseline.to_dict()}
        try:
            write(s3, key, payload, create=True)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                raise
            payload = json.loads(read(s3, uri))
    if payload["model_package_arn"] != manifest["model_package_arn"]:
        raise ValueError("Baseline belongs to a different model package")
    return mon.Baseline.from_dict(payload["baseline"]), uri


def credit_facet(features, fitted):
    levels = fitted["category_levels"]["creditrating"]
    out = pd.Series(levels[0], index=features.index)
    for level in levels[1:]:
        out.loc[features[feature_name("creditrating", level)] == 1] = level
    return out, levels[0]


def publish(cw, report):
    source = "replay" if report["replay"] else "live"
    dims = [{"Name": "ModelName", "Value": settings.MODEL_NAME}, {"Name": "Source", "Value": source}]
    flags, drift = report["flags"], report["drift"]
    values = {"JobSucceeded": 1, "RowsScored": flags["rows"], "Flagged": flags["flagged"],
        "FlagRate": flags["flag_rate"], "NobodyFlagged": int(flags["flagged"] == 0),
        "MaxFeaturePSI": drift["max_psi"], "FeaturesInvestigate": len(drift["features_investigate"]),
        "QualityAssessed": int(report["quality"] is not None),
        "BiasAssessed": int(bool(report["bias"] and report["bias"]["assessed"]))}
    if report["label_join"]:
        values["LabelCoverage"] = report["label_join"]["coverage"]
    if report["quality"]:
        for key in ["Precision", "Recall", "F1", "F2", "Accuracy"]:
            values[key] = report["quality"][key]
    if report["bias"] and report["bias"]["assessed"]:
        values.update(DI_CreditRating=report["bias"]["DI"], FourFifthsBreach=int(report["bias"]["four_fifths_breach"]))
    data = [{"MetricName": k, "Value": float(v), "Dimensions": dims} for k,v in values.items() if math.isfinite(v)]
    cw.put_metric_data(Namespace=settings.NAMESPACE, MetricData=data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--labels", help="Optional S3 URI of customerid,churn_label CSV")
    args = parser.parse_args()
    if "/" in args.run_id or not args.run_id:
        parser.error("Use the batch run ID, not an S3 path")
    session = boto3.Session(profile_name=config.AWS_PROFILE, region_name=config.REGION)
    if session.client("sts").get_caller_identity()["Account"] != config.LAB_ACCOUNT_ID:
        raise ValueError("AWS profile is not using the configured Learner Lab account")
    s3 = session.client("s3")
    root = f"s3://{config.OUTPUT_BUCKET}/inference/cell2cell/{args.run_id}"
    manifest = json.loads(read(s3, root + "/manifest.json"))
    scores, features = frame(s3, root + "/scores.csv"), frame(s3, root + "/features.csv")
    if manifest["run_id"] != args.run_id or not len(scores) or len(scores) != len(features) or len(scores) != manifest["rows"]:
        raise ValueError("Batch files disagree with the manifest's run or row count")
    for data in [scores, features]:
        if data.customerid.isna().any() or data.customerid.duplicated().any():
            raise ValueError("Customer IDs must be present and unique")
    if set(scores.customerid) != set(features.customerid):
        raise ValueError("Scores and features describe different customers")
    if not scores.outreach_flag.isin([0,1]).all() or int(scores.outreach_flag.sum()) != manifest["flagged"]:
        raise ValueError("Outreach flags disagree with batch manifest")
    contract = json.loads(read(s3, manifest["approved_contract_uri"]))
    if (contract["model_package_arn"] != manifest["model_package_arn"]
            or contract["pipeline_execution_arn"].rsplit("/",1)[-1] != manifest["execution_id"]):
        raise ValueError("Batch manifest and approved contract refer to different models")
    fitted = json.loads(read(s3, contract["preprocessing"]["uri"]))
    if list(features.columns) != ["customerid", *fitted["feature_names"]] or not np.isfinite(features.iloc[:,1:].to_numpy()).all():
        raise ValueError("Features must match the pinned order and contain finite values")
    baseline, baseline_uri = model_baseline(s3, manifest, fitted)
    report = {"run_id": args.run_id, "replay": bool(manifest.get("replay",False)),
        "source": manifest.get("source", "holdout batch"), "model_package_arn": manifest["model_package_arn"],
        "baseline_uri": baseline_uri, "threshold": manifest["threshold"],
        "drift": mon.drift_report(baseline, features),
        "flags": {"rows": len(scores), "flagged": int(scores.outreach_flag.sum()), "flag_rate": float(scores.outreach_flag.mean())},
        "label_source": args.labels or manifest.get("labels_uri"), "label_join": None, "quality": None, "bias": None}
    if report["label_source"]:
        labels = frame(s3, report["label_source"])
        if labels.customerid.isna().any() or not labels.churn_label.isin([0,1]).all():
            raise ValueError("Labels need non-null customer IDs and binary churn_label")
        joined = mon.join_scored_to_labels(scores, labels[["customerid","churn_label"]])
        report["label_join"] = joined.summary()
        if joined.matched:
            matched = joined.frame
            report["quality"] = mon.quality_report(matched.churn_label, matched.outreach_flag)
            facet, reference = credit_facet(features.set_index("customerid").loc[matched.customerid], fitted)
            report["bias"] = mon.bias_report(matched.churn_label, matched.outreach_flag, facet.to_numpy(), reference)
            report["bias"]["reference_credit_rating"] = reference
    key = f"monitoring/cell2cell/{args.run_id}/drift_report.json"
    write(s3, key, report)
    publish(session.client("cloudwatch"), report)
    print(f"s3://{config.OUTPUT_BUCKET}/{key}")
    print(f"Max PSI: {report['drift']['max_psi']:.4f}; flagged: {report['flags']['flag_rate']:.2%}")
    print(f"Quality assessed: {report['quality'] is not None}; source: {'replay' if report['replay'] else 'live'}")


if __name__ == "__main__":
    main()
