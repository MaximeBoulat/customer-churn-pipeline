"""Evaluate the trained XGBoost model on the untouched test split."""
import json
import argparse
from pathlib import Path
import tarfile

import numpy as np
import pandas as pd
import evaluation_metrics as ev
import xgboost as xgb
from stage_manifest import record as record_stage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execution-id", default=None)
    parser.add_argument("--top-k", type=float, default=ev.DEFAULT_TOP_K)
    args = parser.parse_args()
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
    out = Path("/opt/ml/processing/output")
    out.mkdir(parents=True, exist_ok=True)
    (out / "evaluation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    record_stage(args.execution_id, "evaluation",
        evaluation={"threshold": report["threshold"]["value"],
                    "contact_nobody": report["threshold"]["contact_nobody"],
                    "outreach_policy": report["threshold"]["outreach_policy"],
                    "auc": report["binary_classification_metrics"]["auc"]["value"],
                    "pr_auc": report["binary_classification_metrics"]["pr_auc"]["value"],
                    "confusion_matrix": report["binary_classification_metrics"]["confusion_matrix"]})


if __name__ == "__main__":
    main()
