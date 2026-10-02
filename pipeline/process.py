"""SageMaker Processing entry point: curated Parquet to model inputs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import preprocess
from stage_manifest import record as record_stage


def read_curated(directory):
    # Athena CTAS files need not have a .parquet extension.
    files = sorted(p for p in Path(directory).rglob("*") if p.is_file())
    if not files:
        raise ValueError(f"No curated Parquet in {directory}")
    return pd.concat([pd.read_parquet(p) for p in files], ignore_index=True).sort_values("customerid").reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preprocessing-version", required=True)
    parser.add_argument("--curated-version", required=True)
    parser.add_argument("--feature-version", required=True)
    parser.add_argument("--execution-id", default=None)
    args = parser.parse_args()
    frame = read_curated("/opt/ml/processing/input/curated")
    splits = preprocess.make_splits(frame)
    contract = preprocess.fit(frame[splits == "train"], args.preprocessing_version)
    contract.update(curated_version=args.curated_version, feature_version=args.feature_version)
    out = Path("/opt/ml/processing/output")
    (out / "contract").mkdir(parents=True, exist_ok=True)
    (out / "identifiers").mkdir(exist_ok=True)
    for name in preprocess.SPLIT_FRACTIONS:
        part = frame[splits == name]
        features = preprocess.transform(part, contract)
        if not np.isfinite(features.to_numpy()).all():
            raise ValueError(f"Non-finite features in {name}")
        target = out / name
        target.mkdir(exist_ok=True)
        pd.concat([part.churn_label.astype(int), features], axis=1).to_csv(
            target / f"{name}.csv", header=False, index=False)
        part.customerid.astype("int64").to_csv(out / "identifiers" / f"{name}.csv", header=False, index=False)
        print(f"{name}: {len(part)} rows, {len(features.columns)} features", flush=True)
    (out / "contract" / "preprocessing_parameters.json").write_text(json.dumps(contract, indent=2, allow_nan=False) + "\n")
    record_stage(args.execution_id, "processing",
        preprocessing={"feature_count": len(contract["feature_names"]),
                       "split_seed": contract["split_seed"],
                       "split_fractions": contract["split_fractions"]},
        rows_curated=len(frame))


if __name__ == "__main__":
    main()
