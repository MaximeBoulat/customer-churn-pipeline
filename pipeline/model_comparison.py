"""
Side-by-side comparison of the majority-class baseline, logistic regression and
XGBoost on one held-out test split (issue #57, finishing M4-01).

Pure functions over evaluation reports in the shape `evaluate.evaluation_report`
returns, so every model is measured by the same code and the same cost policy.
Runs inside Evaluate, which writes the rows into evaluation.json. No AWS and no model library here.
"""

from __future__ import annotations

import numpy as np

import evaluation_metrics as ev

UNDEFINED = None  # rendered as "undefined": the metric does not exist for that model


def majority_class_row(labels: np.ndarray, prevalence: float = ev.TRUE_PREVALENCE) -> dict:
    """Always predict no churn. Nothing is flagged, so precision has no denominator,
    and a constant score ranks nobody, so ROC-AUC and PR-AUC do not exist."""
    labels = np.asarray(labels).astype(int)
    positives = int(labels.sum())
    return {
        "model": "majority class (always no churn)",
        "threshold": UNDEFINED, "outreach_policy": "contact nobody",
        "tp": 0, "fp": 0, "fn": positives, "tn": int(len(labels) - positives),
        "precision": UNDEFINED, "recall": 0.0, "f1": 0.0, "f2": 0.0,
        "auc": UNDEFINED, "pr_auc": UNDEFINED,
        "precision_at_true_prevalence": UNDEFINED,
        "expected_cost": ev.expected_cost(0.0, 0.0, prevalence),
    }


def f_beta(matrix: dict, beta: float) -> float | None:
    """From the counts, so a model that flags nobody but misses churners scores 0,
    as the majority baseline does, instead of an undefined 0/0."""
    b2 = beta**2
    denominator = (1 + b2) * matrix["tp"] + b2 * matrix["fn"] + matrix["fp"]
    return (1 + b2) * matrix["tp"] / denominator if denominator else UNDEFINED


def report_row(model: str, report: dict, prevalence: float = ev.TRUE_PREVALENCE) -> dict:
    """One model's line, from its evaluation report. Cost is per customer at the
    true prevalence, from the test split's TPR and FPR at the validation cutoff."""
    metrics = report["binary_classification_metrics"]
    projected = report["test_at_threshold"]["projected"]
    matrix = metrics["confusion_matrix"]
    value = lambda name: _finite(metrics[name]["value"])  # noqa: E731
    return {
        "model": model,
        "threshold": report["threshold"]["value"],
        "outreach_policy": report["threshold"]["outreach_policy"],
        **matrix,
        "precision": value("precision"), "recall": value("recall"),
        "f1": f_beta(matrix, 1.0), "f2": f_beta(matrix, 2.0),
        "auc": value("auc"), "pr_auc": value("pr_auc"),
        "precision_at_true_prevalence": _finite(projected["precision"]),
        "expected_cost": ev.expected_cost(projected["tpr"], projected["fpr"], prevalence),
    }


def unavailable_row(model: str, reason: str | None) -> dict:
    """A model that could not be compared: every metric undefined, and why."""
    keys = ("threshold", "tp", "fp", "fn", "tn", "precision", "recall", "f1", "f2", "auc", "pr_auc",
            "precision_at_true_prevalence", "expected_cost")
    return {"model": f"{model} (not available)", "outreach_policy": UNDEFINED,
            **{key: UNDEFINED for key in keys}, "reason": reason or "not available"}


def compare(xgboost_report: dict, logistic_report: dict | None, test_labels: np.ndarray,
            unavailable: str | None = None) -> list[dict]:
    """The three rows, refusing reports that were not scored on the same test rows.
    Without a baseline report its row is kept, marked not available, with the reason."""
    test_labels = np.asarray(test_labels).astype(int)
    expected = (int(test_labels.sum()), int(len(test_labels)))
    reports = [("xgboost", xgboost_report)]
    if logistic_report is not None:
        reports.append(("logistic regression", logistic_report))
    for name, report in reports:
        m = report["binary_classification_metrics"]["confusion_matrix"]
        found = (m["tp"] + m["fn"], m["tp"] + m["fp"] + m["fn"] + m["tn"])
        if found != expected:
            raise ValueError(
                f"{name} was scored on {found[1]} rows with {found[0]} churners; "
                f"the test split has {expected[1]} with {expected[0]}")
    return [
        majority_class_row(test_labels),
        report_row("logistic regression", logistic_report) if logistic_report is not None
        else unavailable_row("logistic regression", unavailable),
        report_row("xgboost", xgboost_report),
    ]


COLUMNS = (
    ("model", "Model"), ("threshold", "Cutoff"), ("tp", "TP"), ("fp", "FP"), ("fn", "FN"), ("tn", "TN"),
    ("precision", "Precision"), ("recall", "Recall"), ("f1", "F1"), ("f2", "F2"),
    ("auc", "ROC-AUC"), ("pr_auc", "PR-AUC"), ("expected_cost", "Cost per customer"),
)


def to_markdown(rows: list[dict]) -> str:
    def cell(key: str, value) -> str:
        if value is UNDEFINED:
            return "undefined"
        if key == "expected_cost":
            return f"${value:.4f}"
        return f"{value:,}" if isinstance(value, int) else value if isinstance(value, str) else f"{value:.4f}"

    head = "| " + " | ".join(title for _, title in COLUMNS) + " |"
    rule = "|" + "|".join("---" for _ in COLUMNS) + "|"
    body = ["| " + " | ".join(cell(key, row[key]) for key, _ in COLUMNS) + " |" for row in rows]
    return "\n".join([head, rule, *body])


def _finite(value) -> float | None:
    """A NaN ratio (nothing flagged) becomes undefined instead of a number."""
    return float(value) if value is not None and value == value else UNDEFINED
