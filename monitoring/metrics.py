"""Monitoring calculations adapted from PR #56 (0740804)."""
from __future__ import annotations
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import numpy as np
import pandas as pd
from pipeline.preprocess import NON_FEATURES
import settings

PSI_BINS = 10
PSI_STABLE = 0.1
PSI_INVESTIGATE = settings.PSI_INVESTIGATE
EPSILON = 1e-6
UNSEEN = "__unseen__"
ID_COLUMN = "customerid"
FOUR_FIFTHS_FLOOR = 0.8
FOUR_FIFTHS_CEILING = 1.25
MIN_FLAGGED_FOR_BIAS = settings.MIN_FLAGGED

@dataclass
class Baseline:
    """A frozen reference distribution, and the versions it was measured on."""

    numeric: dict[str, dict] = field(default_factory=dict)
    categorical: dict[str, dict] = field(default_factory=dict)
    feature_names: list[str] = field(default_factory=list)
    row_count: int = 0
    curated_version: str | None = None
    preprocessing_contract: str | None = None
    model_version: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "Baseline":
        known = {f for f in cls().to_dict()}
        return cls(**{k: v for k, v in payload.items() if k in known})

def _open_edges(edges: list[float]) -> np.ndarray:
    """Reopen the outer bins. Edges are stored finite because Infinity is not
    valid JSON; values beyond the reference range land in the end bins."""
    opened = np.asarray(edges, dtype=float).copy()
    opened[0], opened[-1] = -math.inf, math.inf
    return opened

def _shares(counts: np.ndarray) -> list[float]:
    total = counts.sum()
    if total == 0:
        return [0.0] * len(counts)
    return (counts / total).tolist()

def fit_baseline(
    reference: pd.DataFrame,
    *,
    exclude: set[str] | None = None,
    curated_version: str | None = None,
    preprocessing_contract: str | None = None,
    model_version: str | None = None,
) -> Baseline:
    """Freeze the reference distribution: bin edges, categories, and shares."""
    # Identifiers are not features: left in, every batch's new ids read as drift.
    skip = set(NON_FEATURES) | set(exclude or ())

    numeric, categorical = {}, {}
    for name in reference.columns:
        if name in skip:
            continue
        column = reference[name].dropna()
        if column.empty:
            continue
        if pd.api.types.is_numeric_dtype(column) and column.nunique() > PSI_BINS:
            edges = np.unique(np.quantile(column, np.linspace(0, 1, PSI_BINS + 1)))
            if len(edges) < 3:
                edges = np.linspace(column.min(), column.max(), PSI_BINS + 1)
            edges = edges.astype(float).tolist()
            counts = np.histogram(column, bins=_open_edges(edges))[0]
            numeric[name] = {"edges": edges, "shares": _shares(counts)}
        else:
            values = column.astype(float).astype(str) if pd.api.types.is_numeric_dtype(column) else column.astype(str)
            counts = values.value_counts()
            categories = counts.index.tolist() + [UNSEEN]
            shares = _shares(np.append(counts.to_numpy(), 0))
            categorical[name] = {"categories": categories, "shares": shares,
                                 "numeric_values": bool(pd.api.types.is_numeric_dtype(column))}

    return Baseline(
        numeric=numeric,
        categorical=categorical,
        feature_names=sorted([*numeric, *categorical]),
        row_count=int(len(reference)),
        curated_version=curated_version,
        preprocessing_contract=preprocessing_contract,
        model_version=model_version,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

def _psi(reference_shares: list[float], current_shares: list[float]) -> float:
    reference = np.clip(np.asarray(reference_shares, dtype=float), EPSILON, None)
    current = np.clip(np.asarray(current_shares, dtype=float), EPSILON, None)
    return float(np.sum((current - reference) * np.log(current / reference)))

def psi(baseline: Baseline, current: pd.DataFrame) -> dict[str, float]:
    """PSI per feature, against the frozen baseline rather than a fresh fit."""
    scores: dict[str, float] = {}

    for name, spec in baseline.numeric.items():
        if name not in current:
            continue
        column = current[name].dropna()
        if column.empty:
            continue
        counts = np.histogram(column, bins=_open_edges(spec["edges"]))[0]
        scores[name] = _psi(spec["shares"], _shares(counts))

    for name, spec in baseline.categorical.items():
        if name not in current:
            continue
        column = current[name].dropna()
        column = column.astype(float).astype(str) if spec.get("numeric_values") else column.astype(str)
        if column.empty:
            continue
        known = set(spec["categories"]) - {UNSEEN}
        mapped = column.where(column.isin(known), UNSEEN)
        counts = mapped.value_counts().reindex(spec["categories"], fill_value=0)
        scores[name] = _psi(spec["shares"], _shares(counts.to_numpy()))

    return scores

def drift_report(baseline: Baseline, current: pd.DataFrame) -> dict:
    """PSI per feature plus the headline numbers an alarm watches."""
    scores = psi(baseline, current)
    worst = max(scores, key=scores.get) if scores else None
    return {
        "psi": scores,
        "max_psi": scores[worst] if worst else 0.0,
        "worst_feature": worst,
        "features_investigate": sorted(k for k, v in scores.items() if v >= PSI_INVESTIGATE),
        "features_warning": sorted(
            k for k, v in scores.items() if PSI_STABLE <= v < PSI_INVESTIGATE),
        "rows_scored": int(len(current)),
        "baseline_rows": baseline.row_count,
        "baseline_created_at": baseline.created_at,
    }

def _safe(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else float("nan")

@dataclass
class LabelJoin:
    """Matched rows, and the accounting for everything that did not match."""

    frame: pd.DataFrame
    scored_rows: int = 0
    label_rows: int = 0
    matched: int = 0
    missing_labels: int = 0
    unmatched_labels: int = 0

    @property
    def coverage(self) -> float:
        return _safe(self.matched, self.scored_rows)

    def summary(self) -> dict:
        return {
            "scored_rows": self.scored_rows,
            "label_rows": self.label_rows,
            "matched": self.matched,
            "missing_labels": self.missing_labels,
            "unmatched_labels": self.unmatched_labels,
            "coverage": self.coverage,
        }

def join_scored_to_labels(
    scored: pd.DataFrame, labels: pd.DataFrame, id_column: str = ID_COLUMN
) -> LabelJoin:
    """One-to-one join on the identifier, or raise. A duplicated id would fan
    the join out and read as a metric change rather than a data fault."""
    for frame, side in ((scored, "scored"), (labels, "labels")):
        if id_column not in frame.columns:
            raise ValueError(f"{side} has no {id_column!r} column; the join needs it")
        duplicated = int(frame[id_column].duplicated().sum())
        if duplicated:
            raise ValueError(
                f"{side} has {duplicated} duplicate {id_column} values; "
                "the join must be one-to-one")

    merged = scored.merge(labels, on=id_column, how="inner", validate="one_to_one")
    return LabelJoin(
        frame=merged,
        scored_rows=len(scored),
        label_rows=len(labels),
        matched=len(merged),
        missing_labels=len(scored) - len(merged),
        unmatched_labels=len(labels) - len(merged),
    )

def quality_report(
    labels: np.ndarray, predictions: np.ndarray
) -> dict:
    """Quality on labelled records only. Refuses an empty set: precision 0.0
    from no labels reads as a broken model rather than a missing input."""
    labels = np.asarray(labels).astype(int)
    if labels.size == 0:
        raise ValueError("no labelled records; quality cannot be scored")

    predictions = np.asarray(predictions).astype(int)
    tp = int(((predictions == 1) & (labels == 1)).sum())
    fp = int(((predictions == 1) & (labels == 0)).sum())
    fn = int(((predictions == 0) & (labels == 1)).sum())
    tn = int(((predictions == 0) & (labels == 0)).sum())

    precision = _safe(tp, tp + fp)
    recall = _safe(tp, tp + fn)

    def f_beta(beta: float) -> float:
        b2 = beta**2
        denominator = (1 + b2) * tp + b2 * fn + fp
        return float((1 + b2) * tp / denominator) if denominator else 0.0

    return {
        "Accuracy": _safe(tp + tn, len(labels)),
        "Precision": precision,
        "Recall": recall,
        "F1": f_beta(1.0),
        # F2 weights recall above precision: a missed churner costs $500, an
        # unnecessary offer $100, so under-detection is the expensive error.
        "F2": f_beta(2.0),
        "PredictedPositiveRate": float(predictions.mean()),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "labelled_rows": int(labels.size),
    }

def bias_report(
    labels: np.ndarray, predictions: np.ndarray, facet: np.ndarray,
    advantaged,
) -> dict:
    """Compare credit groups using existing outreach flags; DI is a screening signal."""
    labels = np.asarray(labels).astype(int)
    if labels.size == 0:
        raise ValueError("no labelled records; bias cannot be scored")

    predictions = np.asarray(predictions).astype(int)
    flagged = int(predictions.sum())
    if flagged < MIN_FLAGGED_FOR_BIAS:
        return {"assessed": False, "flagged": flagged,
                "reason": f"{flagged} flagged; fewer than {MIN_FLAGGED_FOR_BIAS} "
                          "is too few for a flag-rate ratio to mean anything"}
    is_advantaged = np.asarray(facet) == advantaged

    def rates(mask):
        tp = int(((predictions == 1) & (labels == 1) & mask).sum())
        fp = int(((predictions == 1) & (labels == 0) & mask).sum())
        fn = int(((predictions == 0) & (labels == 1) & mask).sum())
        tn = int(((predictions == 0) & (labels == 0) & mask).sum())
        n = int(mask.sum())
        return {"n": n, "ppr": _safe(tp + fp, n), "tpr": _safe(tp, tp + fn),
                "acc": _safe(tp + tn, n)}

    a, d = rates(is_advantaged), rates(~is_advantaged)
    di = _safe(d["ppr"], a["ppr"])
    if not a["n"] or not d["n"] or not math.isfinite(di):
        return {"assessed": False, "flagged": flagged, "reason": "Both credit groups and a nonzero reference flag rate are needed"}
    breached = not (FOUR_FIFTHS_FLOOR <= di <= FOUR_FIFTHS_CEILING)
    return {
        "assessed": True,
        "flagged": flagged,
        "DPPL": a["ppr"] - d["ppr"],
        "DI": di,
        "RD": a["tpr"] - d["tpr"],
        "AD": a["acc"] - d["acc"],
        "n_advantaged": a["n"],
        "n_disadvantaged": d["n"],
        "four_fifths_breach": breached,
    }

def _json_safe(value):
    """NaN and infinity become null, so a strict reader still parses the file."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and (value != value or value in (math.inf, -math.inf)):
        return None
    if hasattr(value, "item"):  # numpy scalars
        return _json_safe(value.item())
    return value
