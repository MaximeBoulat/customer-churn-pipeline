"""Pipeline step: fit the logistic-regression baseline and score it.
Writes scores, not only a model, so the evaluation step never unpickles scikit-learn.

The baseline is evidence about the model, not a dependency of releasing it. If it
cannot be fitted, this step says why in baseline.json and still succeeds, so
evaluation and registration of the operational model go ahead.
"""

import argparse
import json
import pathlib

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

IN = pathlib.Path("/opt/ml/processing/input")
SCORES = pathlib.Path("/opt/ml/processing/output/scores")
MODEL = pathlib.Path("/opt/ml/processing/output/model")


def read(split: str):
    """Label in column 0, features after it, as docs/m4-04-pipeline-contract.md fixes.
    The file is named, not globbed: a stray csv in the folder must not become the split."""
    frame = pd.read_csv(IN / split / f"{split}.csv", header=None)
    features = frame.iloc[:, 1:]
    if not np.isfinite(features.to_numpy(dtype=float)).all():
        raise ValueError(f"{split} has missing or non-finite feature values; the contract promises none")
    return features, frame.iloc[:, 0].astype(int)


def fit_and_score(max_iter: int):
    # The scaler is fit here, on train only, and travels with the model, so
    # validation and test are transformed by train's statistics.
    features, labels = read("train")
    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=max_iter))
    model.fit(features, labels)
    iterations = int(model[-1].n_iter_[0])
    print(f"fit on {len(features):,} rows, {iterations} iterations")

    scored = {}
    for split in ("validation", "test"):
        split_features, split_labels = read(split)
        scored[split] = pd.DataFrame({"label": split_labels.to_numpy(),
                                      "score": model.predict_proba(split_features)[:, 1]})
        print(f"{split:11} {len(scored[split]):>6,} rows scored")
    summary = {"available": True, "scaler": "StandardScaler, fit on train",
               "max_iter": max_iter, "iterations": iterations, "converged": iterations < max_iter,
               "sklearn": sklearn.__version__, "train_rows": int(len(features))}
    return model, scored, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-iter", type=int, default=1000)
    args = parser.parse_args()
    SCORES.mkdir(parents=True, exist_ok=True)
    MODEL.mkdir(parents=True, exist_ok=True)

    try:
        model, scored, summary = fit_and_score(args.max_iter)
    except Exception as exc:  # reported, never fatal: see the module docstring
        summary = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
        print(f"baseline not available: {summary['reason']}")
    else:
        for split, frame in scored.items():
            frame.to_csv(SCORES / f"{split}.csv", header=False, index=False)
        joblib.dump(model, MODEL / "model.joblib")
    (SCORES / "baseline.json").write_text(json.dumps(dict(summary, model="logistic_regression"), indent=2))


if __name__ == "__main__":
    main()
