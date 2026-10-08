#!/usr/bin/env python3
"""Replay a labelled split through the approved model as a contract-shaped run
with labels.csv, for model quality calculations that need ground truth. Manifest says replay.
--drift marks a disclosed simulation (-sim run id) for M5-03. Scores locally, no cost.

    uv run --with 'xgboost==1.7.6' python monitoring/replay_labelled_batch.py \
        --execution-id <execution> --model-version <n> --split test [--drift monthlyrevenue=1.5]
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import sys
import tarfile
import time

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import settings as config  # noqa: E402
import metrics as mon  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--split", default="test", choices=("train", "validation", "test"))
    parser.add_argument("--threshold", type=float, default=None,
                        help="cutoff; defaults to the execution's evaluation.json threshold.value")
    parser.add_argument("--model-version", required=True,
                        help="the registered package version this execution produced")
    parser.add_argument("--drift", metavar="FEATURE=FACTOR",
                        help="multiply one feature before scoring; marks the run as a simulation")
    args = parser.parse_args()
    import xgboost

    s3, bucket = config.s3_client(), config.BUCKET_NAME

    models = config.dataset_path(config.MODELS_PREFIX, config.MODEL_VERSION)
    prefix = f"{models}/{args.execution_id}/"
    key = next(o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix=prefix)["Contents"]
               if o["Key"].endswith("model.tar.gz"))
    with tarfile.open(fileobj=io.BytesIO(s3.get_object(Bucket=bucket, Key=key)["Body"].read())) as tar:
        member = next(m for m in tar.getmembers() if m.isfile() and pathlib.Path(m.name).name == "xgboost-model")
        booster = xgboost.Booster()
        booster.load_model(bytearray(tar.extractfile(member).read()))

    threshold = args.threshold
    if threshold is None:
        evaluation = json.loads(s3.get_object(
            Bucket=bucket, Key=f"{models}/{args.execution_id}/evaluation/evaluation.json")["Body"].read())
        threshold = (evaluation.get("threshold") or {}).get("value")
        if threshold is None:
            raise SystemExit("evaluation.json has no threshold.value; pass --threshold")

    split = mon.read_split(args.execution_id, args.split, client=s3)
    names = mon.load_feature_names(client=s3, execution_id=args.execution_id)
    features = split[names].copy()

    simulation = None
    if args.drift:
        feature, factor = args.drift.split("=")
        features[feature] = features[feature] * float(factor)
        simulation = {"feature": feature, "factor": float(factor), "source_split": args.split}

    scores = booster.predict(xgboost.DMatrix(features.to_numpy()))
    flag = (scores >= threshold).astype(int)

    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ("-sim" if simulation else "")
    ids = [f"{args.split[0]}{i:05d}" for i in range(len(split))]
    out = config.dataset_path(config.INFERENCE_PREFIX, run_id)

    def put(name, body):
        s3.put_object(Bucket=bucket, Key=f"{out}/{name}", Body=body, ServerSideEncryption="AES256")

    put("scores.csv", pd.DataFrame({
        "customerid": ids, "churn_probability": scores, "outreach_flag": flag,
        "model_version": f"{config.MODEL_PACKAGE_GROUP}/{args.model_version}"}).to_csv(index=False).encode())
    put("features.csv", pd.concat([pd.Series(ids, name="customerid"),
                                   features.reset_index(drop=True)], axis=1).to_csv(index=False).encode())
    put("labels.csv", pd.DataFrame({"customerid": ids,
                                    "churn_label": split["churn_label"].to_numpy()}).to_csv(index=False).encode())
    put("manifest.json", json.dumps({
        "schema_version": 1, "run_id": run_id, "rows": len(split), "flagged": int(flag.sum()),
        "model_version": f"{config.MODEL_PACKAGE_GROUP}/{args.model_version}", "execution_id": args.execution_id,
        "threshold": {"value": threshold, "prevalence": 0.02},
        "simulation": simulation, "source": f"labelled replay of the {args.split} split", "replay": True,
    }, indent=2).encode())

    print(f"{run_id}: {len(split):,} rows, {int(flag.sum())} flagged"
          + (f", SIMULATION {simulation['feature']} x{simulation['factor']}" if simulation else ""))
    print(f"s3://{bucket}/{out}/")


if __name__ == "__main__":
    main()
