"""Benchmark comparison for an evaluation report."""
import json

import numpy as np
import pandas as pd

import evaluation_metrics as ev
import model_comparison as mc


def load_baseline(directory, validation_labels, test_labels, top_k, model_uri):
    """Read scores on the same ordered splits, or report why comparison is unavailable."""
    summary = {}
    try:
        summary = json.loads((directory / "baseline.json").read_text())
        if not summary.get("available"):
            return None, summary
        scores = {}
        for split, expected in (("validation", validation_labels), ("test", test_labels)):
            table = pd.read_csv(directory / f"{split}.csv", header=None)
            if table.shape != (len(expected), 2):
                raise ValueError(f"baseline {split} shape differs from the evaluation split")
            if not np.array_equal(table.iloc[:, 0].to_numpy(), expected):
                raise ValueError(f"baseline {split} labels differ from the evaluation split")
            values = table.iloc[:, 1].to_numpy(dtype=float)
            if not np.isfinite(values).all() or ((values < 0) | (values > 1)).any():
                raise ValueError(f"baseline {split} scores must be finite probabilities")
            scores[split] = values
        report = ev.evaluation_report(test_labels, scores["test"], validation_labels,
                                      scores["validation"], top_k)
        return report, dict(summary, available=True, model_data=model_uri)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, dict(summary, available=False, reason=f"{type(exc).__name__}: {exc}")


def add_benchmarks(report, logistic, summary, labels):
    rows = mc.compare(report, logistic, labels, unavailable=summary.get("reason"))
    report["comparison"] = rows
    report["comparison_complete"] = logistic is not None
    # Constant scores have no ranking: retain the team's no-skill reference values
    # separately from the undefined ranking entries in the comparison table.
    baselines = {"majority_class": {
        "auc": 0.5, "pr_auc": float(np.asarray(labels).mean()), "f2": 0.0,
        "threshold": None, "note": "ROC-AUC and PR-AUC are no-skill reference values"}}
    if logistic is not None:
        row = rows[1]
        baselines["logistic_regression"] = dict(summary, **{
            key: row[key] for key in ("auc", "pr_auc", "f2", "threshold")})
    report["beats_baselines"] = {
        name: {metric: (rows[2][metric] or 0.0) > (baseline[metric] or 0.0)
               for metric in ("f2", "pr_auc")}
        for name, baseline in baselines.items()}
    baselines.setdefault("logistic_regression", summary)
    report["baselines"] = baselines
    return report


def strict(value):
    """Undefined metrics are null, never nonstandard JSON NaN or infinity."""
    if isinstance(value, dict):
        return {key: strict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [strict(item) for item in value]
    return None if isinstance(value, float) and not np.isfinite(value) else value
