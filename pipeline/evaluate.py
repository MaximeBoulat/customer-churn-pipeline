"""Evaluate the trained XGBoost model on the untouched test split."""
import json
import argparse
from pathlib import Path
import tarfile

import numpy as np
import pandas as pd
import evaluation_metrics as ev
import evaluation_evidence as evidence
from record_stages import training
import xgboost as xgb
from stage_manifest import record as record_stage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--training-job", required=True)
    parser.add_argument("--model-data", required=True)
    parser.add_argument("--baseline-model-uri", required=True)
    parser.add_argument("--top-k", type=float, default=ev.DEFAULT_TOP_K)
    args = parser.parse_args()
    training(args.execution_id, args.training_job, args.model_data)
    # Read only the native model member; do not extract arbitrary archive paths.
    with tarfile.open("/opt/ml/processing/input/model/model.tar.gz", "r:gz") as archive:
        members = [m for m in archive.getmembers() if Path(m.name).name == "xgboost-model" and m.isfile()]
        if len(members) != 1:
            raise ValueError("Expected one native xgboost-model in the training artifact")
        data = archive.extractfile(members[0]).read()
    model = xgb.Booster()
    model.load_model(bytearray(data))
    def score(split):
        table = pd.read_csv(f"/opt/ml/processing/input/{split}/{split}.csv", header=None)
        labels = table.iloc[:, 0].to_numpy().astype(int)
        scores = model.predict(xgb.DMatrix(table.iloc[:, 1:].to_numpy()))
        if not np.isfinite(scores).all():
            raise ValueError("Model returned non-finite predictions")
        return labels, scores

    validation_labels, validation_scores = score("validation")
    labels, scores = score("test")
    report = ev.evaluation_report(labels, scores, validation_labels, validation_scores, args.top_k)
    logistic, summary = evidence.load_baseline(
        Path("/opt/ml/processing/input/baseline"), validation_labels, labels,
        args.top_k, args.baseline_model_uri)
    evidence.add_benchmarks(report, logistic, summary, labels)
    report = evidence.strict(report)
    out = Path("/opt/ml/processing/output")
    out.mkdir(parents=True, exist_ok=True)
    (out / "evaluation.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report, indent=2, allow_nan=False))
    record_stage(args.execution_id, "evaluation",
        logistic_regression=summary,
        evaluation={"threshold": report["threshold"]["value"],
                    "contact_nobody": report["threshold"]["contact_nobody"],
                    "outreach_policy": report["threshold"]["outreach_policy"],
                    "auc": report["binary_classification_metrics"]["auc"]["value"],
                    "pr_auc": report["binary_classification_metrics"]["pr_auc"]["value"],
                    "confusion_matrix": report["binary_classification_metrics"]["confusion_matrix"]})


if __name__ == "__main__":
    main()
