"""Cost-aware evaluation copied from the team repo src/evaluate.py.

Source Git blob: c38cb9cfd6f69ae20ea49c916d763937950e9ffc
"""
from __future__ import annotations

import numpy as np
import pandas as pd

COST_FALSE_NEGATIVE = 500.0


COST_FALSE_POSITIVE = 100.0


TRAIN_PREVALENCE = 0.288


TRUE_PREVALENCE = 0.02


DEFAULT_TOP_K = 0.05


def confusion(labels: np.ndarray, predictions: np.ndarray) -> dict:
    """Counts at one operating point."""
    labels = np.asarray(labels).astype(int)
    predictions = np.asarray(predictions).astype(int)
    return {
        "tp": int(((predictions == 1) & (labels == 1)).sum()),
        "fp": int(((predictions == 1) & (labels == 0)).sum()),
        "fn": int(((predictions == 0) & (labels == 1)).sum()),
        "tn": int(((predictions == 0) & (labels == 0)).sum()),
    }


def _safe(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else float("nan")


def rates(labels: np.ndarray, scores: np.ndarray, threshold: float) -> tuple[float, float]:
    """TPR and FPR at a threshold. The prevalence-invariant pair."""
    matrix = confusion(labels, np.asarray(scores) >= threshold)
    return (
        _safe(matrix["tp"], matrix["tp"] + matrix["fn"]),
        _safe(matrix["fp"], matrix["fp"] + matrix["tn"]),
    )


def threshold_metrics(
    labels: np.ndarray, scores: np.ndarray, threshold: float,
    prevalence: float | None = None,
) -> dict:
    """Metrics at one threshold.

    With `prevalence` given, precision and the F-scores are projected to that
    class mix instead of the sample's own. Recall is untouched either way,
    which is the point: it cannot move.
    """
    matrix = confusion(labels, np.asarray(scores) >= threshold)
    tpr, fpr = rates(labels, scores, threshold)

    if prevalence is None:
        precision = _safe(matrix["tp"], matrix["tp"] + matrix["fp"])
    else:
        precision = _safe(tpr * prevalence, tpr * prevalence + fpr * (1 - prevalence))

    def f_beta(beta: float) -> float:
        b2 = beta**2
        denominator = (b2 * precision) + tpr
        return _safe((1 + b2) * precision * tpr, denominator)

    return {
        "threshold": float(threshold),
        "prevalence": prevalence,
        "tpr": tpr,
        "fpr": fpr,
        "precision": precision,
        "recall": tpr,
        # F2 weights recall above precision, the right emphasis when a missed
        # churner costs 5x an unnecessary offer.
        "f1": f_beta(1.0),
        "f2": f_beta(2.0),
        **matrix,
    }


def roc_points(labels: np.ndarray, scores: np.ndarray) -> pd.DataFrame:
    """Every distinct operating point, ordered by descending score.

    One row per distinct score, so ties are resolved as a single point rather
    than an arbitrary order through them, which would otherwise bend the curve.
    """
    labels = np.asarray(labels).astype(int)
    scores = np.asarray(scores, dtype=float)
    order = np.argsort(-scores, kind="stable")
    labels, scores = labels[order], scores[order]

    positives, negatives = labels.sum(), len(labels) - labels.sum()
    if not positives or not negatives:
        raise ValueError("ROC needs both classes present")

    last = np.r_[np.diff(scores) != 0, True]  # last index of each score group
    tp = np.cumsum(labels)[last]
    fp = np.cumsum(1 - labels)[last]

    return pd.DataFrame({
        "threshold": scores[last],
        "tpr": tp / positives,
        "fpr": fp / negatives,
    })


_trapezoid = getattr(np, "trapezoid", None) or np.trapz


def roc_auc(labels: np.ndarray, scores: np.ndarray) -> float:
    """Area under the ROC curve. Prevalence-invariant, so it needs no projection."""
    points = roc_points(labels, scores)
    fpr = np.r_[0.0, points["fpr"].to_numpy(), 1.0]
    tpr = np.r_[0.0, points["tpr"].to_numpy(), 1.0]
    return float(_trapezoid(tpr, fpr))


def pr_auc(labels: np.ndarray, scores: np.ndarray, prevalence: float | None = None) -> float:
    """Average precision.

    With `prevalence`, precision at each operating point is projected to that
    class mix, giving the PR-AUC the model would show on that population. The
    ranking is unchanged, so only precision moves.

    Average precision rather than trapezoid: interpolating a PR curve
    overstates the area, because precision is not monotonic in recall.
    """
    labels_array = np.asarray(labels).astype(int)
    points = roc_points(labels_array, scores)
    tpr = points["tpr"].to_numpy()
    fpr = points["fpr"].to_numpy()

    pi = labels_array.mean() if prevalence is None else prevalence
    precision = np.divide(
        tpr * pi, tpr * pi + fpr * (1 - pi),
        out=np.zeros_like(tpr), where=(tpr * pi + fpr * (1 - pi)) > 0,
    )
    return float(np.sum(np.diff(np.r_[0.0, tpr]) * precision))


def expected_cost(
    tpr: float, fpr: float, prevalence: float,
    cost_fn: float = COST_FALSE_NEGATIVE, cost_fp: float = COST_FALSE_POSITIVE,
    cost_tp: float | None = None,
) -> float:
    """Expected cost per customer at a given class mix.

    Every contacted customer costs an offer, so a true positive pays `cost_tp`
    (default: the same as `cost_fp`) as well as a false positive. Needs no
    calibrated probability: TPR and FPR carry the model's behaviour,
    prevalence carries the population.
    """
    cost_tp = cost_fp if cost_tp is None else cost_tp
    return float(
        prevalence * (1 - tpr) * cost_fn
        + prevalence * tpr * cost_tp
        + (1 - prevalence) * fpr * cost_fp
    )


def cost_curve(
    labels: np.ndarray, scores: np.ndarray, prevalence: float = TRUE_PREVALENCE,
    cost_fn: float = COST_FALSE_NEGATIVE, cost_fp: float = COST_FALSE_POSITIVE,
) -> pd.DataFrame:
    """Cost at every operating point, including contacting nobody.

    The whole curve, not just its minimum: the shape shows whether the choice
    is a sharp optimum or a broad plateau, which decides how much the cost
    assumptions actually matter. The first row is the no-contact point
    (threshold inf, TPR = FPR = 0), which roc_points omits and which can be the
    cheapest choice at a 2% base rate.
    """
    points = pd.concat(
        [pd.DataFrame({"threshold": [np.inf], "tpr": [0.0], "fpr": [0.0]}), roc_points(labels, scores)],
        ignore_index=True,
    )
    points["prevalence"] = prevalence
    points["expected_cost"] = [
        expected_cost(t, f, prevalence, cost_fn, cost_fp)
        for t, f in zip(points["tpr"], points["fpr"])
    ]
    points["precision"] = [
        _safe(t * prevalence, t * prevalence + f * (1 - prevalence))
        for t, f in zip(points["tpr"], points["fpr"])
    ]
    return points


def choose_threshold(
    labels: np.ndarray, scores: np.ndarray, prevalence: float = TRUE_PREVALENCE,
    cost_fn: float = COST_FALSE_NEGATIVE, cost_fp: float = COST_FALSE_POSITIVE,
) -> dict:
    """The cost-minimising cutoff, JSON-safe, for the M5-01 threshold block.

    Customers are flagged when score >= value. `value` is always finite: when
    contacting nobody is cheapest it is the float just above the highest score,
    so nothing is flagged, and `contact_nobody` says so. Ties go to the cheaper
    action, which is contacting fewer people.

    Raises on input that carries no ranking information, because a cutoff
    chosen from it would look like a result and mean nothing.
    """
    labels = np.asarray(labels).astype(int)
    scores = np.asarray(scores, dtype=float)
    if labels.min() == labels.max():
        raise ValueError("threshold selection needs both classes in the validation labels")
    if scores.min() == scores.max():
        raise ValueError("threshold selection needs validation scores that are not all identical")

    curve = cost_curve(labels, scores, prevalence, cost_fn, cost_fp)
    best = curve.loc[curve["expected_cost"].idxmin()]
    contact_nobody = bool(np.isinf(best["threshold"]))
    value = float(np.nextafter(scores.max(), np.inf)) if contact_nobody else float(best["threshold"])
    return {
        "value": value,
        "selected_on": "validation",
        "validation_rows": int(len(labels)),
        "prevalence": prevalence,
        "cost_fn": cost_fn,
        "cost_contact": cost_fp,
        "expected_cost": float(best["expected_cost"]),
        "no_contact_cost": float(curve["expected_cost"].iloc[0]),
        "contact_nobody": contact_nobody,
    }


def operating_threshold(
    labels: np.ndarray, scores: np.ndarray, top_k: float = DEFAULT_TOP_K,
    prevalence: float = TRUE_PREVALENCE,
    cost_fn: float = COST_FALSE_NEGATIVE, cost_fp: float = COST_FALSE_POSITIVE,
) -> dict:
    """The cutoff the system operates at: cost-minimising, with a stated fallback.

    When `choose_threshold` finds that contacting nobody is cheapest, the system
    still needs customers to work with, so the cutoff falls back to the score at
    the top `top_k` share of validation and `outreach_policy` says so. The honest
    finding is kept: `contact_nobody` stays true, `no_contact_cost` is reported
    beside an `expected_cost` that is now higher than it, so nobody reads the
    fallback as the cost-optimal choice.
    """
    chosen = choose_threshold(labels, scores, prevalence, cost_fn, cost_fp)
    chosen["outreach_policy"] = "cost_minimising"
    chosen["top_k"] = None
    if not chosen["contact_nobody"]:
        return chosen
    if not 0 < top_k <= 1:
        raise ValueError(f"top_k must be in (0, 1], got {top_k}")

    scores = np.asarray(scores, dtype=float)
    value = float(np.quantile(scores, 1 - top_k))
    at = threshold_metrics(labels, scores, value, prevalence)
    chosen.update(
        value=value,
        expected_cost=expected_cost(at["tpr"], at["fpr"], prevalence, cost_fn, cost_fp),
        outreach_policy="top_k",
        top_k=top_k,
    )
    return chosen


def evaluation_report(
    labels: np.ndarray, scores: np.ndarray,
    validation_labels: np.ndarray, validation_scores: np.ndarray,
    top_k: float = DEFAULT_TOP_K,
) -> dict:
    """The evaluation.json body: cutoff chosen on validation, test scored once.

    Kept here, pure, so the pipeline step is a thin wrapper and the M5-01
    threshold contract is testable without a container or a model.
    """
    chosen = operating_threshold(validation_labels, validation_scores, top_k)
    value = chosen["value"]
    sample = threshold_metrics(labels, scores, value)
    projected = threshold_metrics(labels, scores, value, TRUE_PREVALENCE)
    return {
        "binary_classification_metrics": {
            "auc": {"value": roc_auc(labels, scores), "standard_deviation": "NaN"},
            "pr_auc": {"value": pr_auc(labels, scores), "standard_deviation": "NaN"},
            "precision": {"value": sample["precision"], "standard_deviation": "NaN"},
            "recall": {"value": sample["recall"], "standard_deviation": "NaN"},
            "f1": {"value": sample["f1"], "standard_deviation": "NaN"},
            "f2": {"value": sample["f2"], "standard_deviation": "NaN"},
            "accuracy": {"value": (sample["tp"] + sample["tn"]) / len(labels),
                         "standard_deviation": "NaN"},
            "confusion_matrix": {k: sample[k] for k in ("tp", "fp", "fn", "tn")},
        },
        "prevalence_projected": {
            "true_prevalence": TRUE_PREVALENCE,
            "sample_prevalence": float(np.asarray(labels).mean()),
            "precision_at_true_prevalence": projected["precision"],
            "f2_at_true_prevalence": projected["f2"],
            "pr_auc_at_true_prevalence": pr_auc(labels, scores, TRUE_PREVALENCE),
            "note": "TPR and FPR are prevalence-invariant; precision is not.",
        },
        "threshold": chosen,
        "test_at_threshold": {"sample": sample, "projected": projected},
    }
