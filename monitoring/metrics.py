"""System 3: a frozen baseline, PSI against it, model quality and bias.
Reference: team PR #56 at 0740804. Calculations and metric payload match it.
Personal adaptations: imports, AWS session, split paths and S3 encryption."""

from __future__ import annotations

import io
import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import settings as config
from pipeline.preprocess import NON_FEATURES

PSI_BINS = 10

# Conventional PSI reading, and the thresholds the architecture doc quotes.
PSI_STABLE = 0.1
PSI_INVESTIGATE = config.MONITORING_PSI_INVESTIGATE

# Smooths empty bins so log(0) cannot go infinite; explicit so it is reproducible.
EPSILON = 1e-6

# Unseen categories land here and register as drift rather than vanishing.
UNSEEN = "__unseen__"


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
        if pd.api.types.is_numeric_dtype(column):
            edges = np.unique(np.quantile(column, np.linspace(0, 1, PSI_BINS + 1)))
            if len(edges) < 2:
                continue  # constant feature: nothing to measure drift against
            edges = edges.astype(float).tolist()
            counts = np.histogram(column, bins=_open_edges(edges))[0]
            numeric[name] = {"edges": edges, "shares": _shares(counts)}
        else:
            counts = column.astype(str).value_counts()
            categories = counts.index.tolist() + [UNSEEN]
            shares = _shares(np.append(counts.to_numpy(), 0))
            categorical[name] = {"categories": categories, "shares": shares}

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
        column = current[name].dropna().astype(str)
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


# --- the label join ---------------------------------------------------------
# Churn labels arrive late, so a batch is usually only partly labelled.

ID_COLUMN = "customerid"


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


# --- model quality ----------------------------------------------------------


def quality_report(
    labels: np.ndarray, probabilities: np.ndarray, threshold: float
) -> dict:
    """Quality on labelled records only. Refuses an empty set: precision 0.0
    from no labels reads as a broken model rather than a missing input."""
    labels = np.asarray(labels).astype(int)
    if labels.size == 0:
        raise ValueError("no labelled records; quality cannot be scored")

    predictions = (np.asarray(probabilities, dtype=float) > threshold).astype(int)
    tp = int(((predictions == 1) & (labels == 1)).sum())
    fp = int(((predictions == 1) & (labels == 0)).sum())
    fn = int(((predictions == 0) & (labels == 1)).sum())
    tn = int(((predictions == 0) & (labels == 0)).sum())

    precision = _safe(tp, tp + fp)
    recall = _safe(tp, tp + fn)

    def f_beta(beta: float) -> float:
        b2 = beta**2
        return _safe((1 + b2) * precision * recall, (b2 * precision) + recall)

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


# --- bias -------------------------------------------------------------------

FOUR_FIFTHS_FLOOR = 0.8
FOUR_FIFTHS_CEILING = 1.25

# A flag-rate ratio from a handful of positives is noise: 8 flagged gave DI 1.49
# where 21 gave 1.19. Below this many flagged, bias is not assessed.
MIN_FLAGGED_FOR_BIAS = config.MONITORING_MIN_FLAGGED_FOR_BIAS


def bias_report(
    labels: np.ndarray, probabilities: np.ndarray, facet: np.ndarray,
    advantaged, threshold: float,
) -> dict:
    """Segment-sliced fairness in place of Clarify. DI outside [0.8, 1.25] is
    the four-fifths rule, the one threshold here with a standard behind it."""
    labels = np.asarray(labels).astype(int)
    if labels.size == 0:
        raise ValueError("no labelled records; bias cannot be scored")

    predictions = (np.asarray(probabilities, dtype=float) > threshold).astype(int)
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
    breached = True if di != di else not (FOUR_FIFTHS_FLOOR <= di <= FOUR_FIFTHS_CEILING)
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


def subgroup_report(
    frame: pd.DataFrame, labels: np.ndarray, probabilities: np.ndarray,
    by: str, threshold: float, min_rows: int = 100,
) -> pd.DataFrame:
    """One row per segment. Undersized ones are kept with reported=False, since
    a check that is clean only because the group was too small is worse than none."""
    rows = []
    for value, index in frame.groupby(by, dropna=False).groups.items():
        mask = frame.index.isin(index)
        segment_labels = np.asarray(labels)[mask]
        both_classes = 0 < segment_labels.sum() < len(segment_labels)
        reported = bool(mask.sum() >= min_rows and both_classes)
        row = {"segment": value, "rows": int(mask.sum()), "reported": reported}
        if reported:
            row.update(quality_report(
                segment_labels, np.asarray(probabilities)[mask], threshold))
        rows.append(row)
    return pd.DataFrame(rows).sort_values("rows", ascending=False).reset_index(drop=True)


# --- persistence ------------------------------------------------------------
# Baseline keyed by feature version, since its ranges become alarm thresholds; reports per run.


def baseline_key(feature_version: str | None = None) -> str:
    prefix = config.dataset_path(
        config.MONITORING_PREFIX, feature_version or config.FEATURE_VERSION)
    return f"{prefix}/baseline.json"


def drift_report_key(run_id: str) -> str:
    prefix = config.dataset_path(config.MONITORING_PREFIX, run_id)
    return f"{prefix}/drift_report.json"


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


def _put(client, key: str, payload: dict) -> str:
    client.put_object(
        Bucket=config.BUCKET_NAME,
        Key=key,
        Body=json.dumps(_json_safe(payload), indent=2, allow_nan=False).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )
    return f"s3://{config.BUCKET_NAME}/{key}"


def save_baseline(baseline: Baseline, client=None, feature_version: str | None = None) -> str:
    """Persist the baseline and return its S3 URI."""
    client = client or config.s3_client()
    return _put(client, baseline_key(feature_version), baseline.to_dict())


def load_baseline(client=None, feature_version: str | None = None) -> Baseline:
    """Read the persisted baseline. Raises if it was never fitted."""
    client = client or config.s3_client()
    key = baseline_key(feature_version)
    body = client.get_object(Bucket=config.BUCKET_NAME, Key=key)["Body"].read()
    return Baseline.from_dict(json.loads(body))


def save_drift_report(report: dict, run_id: str, client=None) -> str:
    client = client or config.s3_client()
    return _put(client, drift_report_key(run_id), report)


# --- reading what the other systems wrote -----------------------------------
# Splits are headerless, features.csv is not; unnamed columns leave PSI comparing nothing.

MANIFEST = "manifest.json"


class IncompleteRun(Exception):
    """A run prefix with no manifest: System 2 writes it last, so the run did not finish."""


def load_feature_names(client=None, execution_id: str | None = None) -> list[str]:
    """Personal storage adapter: read feature order from this execution's output."""
    if not execution_id:
        raise ValueError("An execution ID is required to locate preprocessing output")
    client = client or config.s3_client()
    key = f"pipeline/{execution_id}/process/contract/preprocessing_parameters.json"
    body = client.get_object(Bucket=config.BUCKET_NAME, Key=key)["Body"].read()
    return json.loads(body)["feature_names"]


def read_split(execution_id: str, split: str = "train", client=None,
               feature_names: list[str] | None = None) -> pd.DataFrame:
    """Personal storage adapter: original split instead of the team's republished copy."""
    client = client or config.s3_client()
    names = feature_names or load_feature_names(client, execution_id)
    key = f"pipeline/{execution_id}/process/{split}/{split}.csv"
    body = client.get_object(Bucket=config.BUCKET_NAME, Key=key)["Body"].read()
    frame = pd.read_csv(io.BytesIO(body), header=None)
    if frame.shape[1] != len(names) + 1:
        raise ValueError(f"{key} has {frame.shape[1]} columns, expected 1 label + {len(names)} features")
    frame.columns = ["churn_label", *names]
    return frame


def read_scored_run(run_id: str, client=None) -> dict:
    """A completed scoring run. Raises IncompleteRun without a manifest, and
    refuses mismatched row counts because a short read looks like drift."""
    client = client or config.s3_client()
    prefix = config.dataset_path(config.INFERENCE_PREFIX, run_id)

    try:
        body = client.get_object(
            Bucket=config.BUCKET_NAME, Key=f"{prefix}/{MANIFEST}")["Body"].read()
    except Exception as exc:
        raise IncompleteRun(f"{prefix} has no {MANIFEST}") from exc
    manifest = json.loads(body)

    def frame(name: str) -> pd.DataFrame:
        raw = client.get_object(Bucket=config.BUCKET_NAME, Key=f"{prefix}/{name}")["Body"].read()
        return pd.read_csv(io.BytesIO(raw))

    scores, features = frame("scores.csv"), frame("features.csv")
    expected = manifest.get("rows")
    for name, got in (("scores.csv", len(scores)), ("features.csv", len(features))):
        if expected is not None and got != expected:
            raise ValueError(f"{name} has {got} rows, manifest says {expected}")

    return {"manifest": manifest, "scores": scores, "features": features}


# --- facets -----------------------------------------------------------------


# The one-hot's dropped reference level, a fact of the preprocessing contract.
# Distinct from MONITORING_BIAS_ADVANTAGED, the policy choice, which happens to equal it.
CREDIT_RATING_REFERENCE = "1-highest"


def credit_rating_facet(features: pd.DataFrame) -> pd.Series:
    """Invert the creditrating one-hot; all zeros is the dropped reference level."""
    columns = [c for c in features.columns if c.startswith("creditrating__")]
    out = pd.Series(CREDIT_RATING_REFERENCE, index=features.index)
    for column in columns:
        out[features[column] == 1] = column.replace("creditrating__", "")
    return out


# --- what one run means, as metrics -----------------------------------------
# One bounded dimension. run_id is never one: every run would mint a new series.

NAMESPACE = config.MONITORING_NAMESPACE
MODEL_DIMENSION = [{"Name": "ModelName", "Value": config.MONITORING_MODEL_NAME}]


def flag_report(manifest: dict, scores: pd.DataFrame) -> dict:
    """Outreach volume. flagged 0 is raised as a signal, not swallowed into a
    rate of 0.0: the cutoff never flags no one on its own data, so the scores moved."""
    flagged = int(scores["outreach_flag"].sum()) if "outreach_flag" in scores else 0
    rows = int(len(scores))
    declared = manifest.get("flagged")
    return {
        "rows": rows,
        "flagged": flagged,
        "flag_rate": _safe(flagged, rows),
        "matches_manifest": declared is None or declared == flagged,
        "nobody_flagged": flagged == 0,
    }


def _metric(name: str, value: float, unit: str = "None") -> dict | None:
    if value is None or value != value:  # None or NaN publishes nothing
        return None
    return {"MetricName": name, "Dimensions": MODEL_DIMENSION,
            "Value": float(value), "Unit": unit}


def cloudwatch_metrics(
    drift: dict, flags: dict, quality: dict | None = None,
    bias: dict | None = None, coverage: float | None = None,
) -> list[dict]:
    """The metric payload for one run. Labelled metrics only when a join produced
    them; precision 0.0 from no labels would read as a broken model."""
    candidates = [
        _metric("JobSucceeded", 1, "Count"),
        _metric("RowsScored", flags["rows"], "Count"),
        _metric("Flagged", flags["flagged"], "Count"),
        _metric("FlagRate", flags["flag_rate"]),
        _metric("NobodyFlagged", int(flags["nobody_flagged"]), "Count"),
        _metric("MaxFeaturePSI", drift["max_psi"]),
        _metric("FeaturesInvestigate", len(drift["features_investigate"]), "Count"),
        _metric("FeaturesWarning", len(drift["features_warning"]), "Count"),
    ]
    if coverage is not None:
        candidates.append(_metric("LabelCoverage", coverage))
    if quality:
        for key in ("Precision", "Recall", "F1", "F2", "Accuracy"):
            candidates.append(_metric(key, quality[key]))
    if bias and bias.get("assessed"):
        candidates.append(_metric("DI_CreditRating", bias["DI"]))
        candidates.append(_metric("FourFifthsBreach", int(bias["four_fifths_breach"]), "Count"))
    if bias:
        candidates.append(_metric("BiasAssessed", int(bool(bias.get("assessed"))), "Count"))
    return [m for m in candidates if m is not None]


def publish_metrics(metrics: list[dict], client=None) -> int:
    """put_metric_data in the 20-item batches the API allows. Returns the count."""
    import boto3

    client = client or config.session().client("cloudwatch")
    for start in range(0, len(metrics), 20):
        client.put_metric_data(Namespace=NAMESPACE, MetricData=metrics[start:start + 20])
    return len(metrics)
