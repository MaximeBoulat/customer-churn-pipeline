"""Evaluate the trained XGBoost model on the untouched test split."""
import json
from pathlib import Path
import tarfile

import numpy as np
import pandas as pd
from sklearn.metrics import (accuracy_score, average_precision_score, confusion_matrix,
                             f1_score, precision_score, recall_score, roc_auc_score)
import xgboost as xgb


def main():
    # Read only the native model member; do not extract arbitrary archive paths.
    with tarfile.open("/opt/ml/processing/input/model/model.tar.gz", "r:gz") as archive:
        members = [m for m in archive.getmembers() if Path(m.name).name == "xgboost-model" and m.isfile()]
        if len(members) != 1:
            raise ValueError("Expected one native xgboost-model in the training artifact")
        data = archive.extractfile(members[0]).read()
    model = xgb.Booster()
    model.load_model(bytearray(data))
    table = pd.read_csv("/opt/ml/processing/input/test/test.csv", header=None)
    labels = table.iloc[:, 0].to_numpy().astype(int)
    scores = model.predict(xgb.DMatrix(table.iloc[:, 1:].to_numpy()))
    if not np.isfinite(scores).all():
        raise ValueError("Model returned non-finite predictions")
    predictions = (scores >= 0.5).astype(int)
    values = {
        "auc": roc_auc_score(labels, scores),
        "average_precision": average_precision_score(labels, scores),
        "accuracy": accuracy_score(labels, predictions),
        "precision": precision_score(labels, predictions, zero_division=0),
        "recall": recall_score(labels, predictions, zero_division=0),
        "f1": f1_score(labels, predictions, zero_division=0),
    }
    tn, fp, fn, tp = confusion_matrix(labels, predictions, labels=[0, 1]).ravel()
    report = {
        "binary_classification_metrics": {name: {"value": float(value)} for name, value in values.items()},
        "classification_threshold": 0.5,
        "recalibration_factor": 1.0,
        "test_rows": len(labels),
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
    }
    out = Path("/opt/ml/processing/output")
    out.mkdir(parents=True, exist_ok=True)
    (out / "evaluation.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
