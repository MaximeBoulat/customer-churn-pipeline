"""Cell2Cell transforms. Fit statistics on training rows only."""
from __future__ import annotations

import hashlib
import numpy as np
import pandas as pd

LEAKAGE = ["retentioncalls", "retentionoffersaccepted", "madecalltoretentionteam"]
NON_FEATURES = ["customerid", "churn", "churn_label", "split", *LEAKAGE]
USAGE = ["monthlyrevenue", "monthlyminutes", "totalrecurringcharge",
         "directorassistedcalls", "overageminutes", "roamingcalls"]
SPLIT_SEED = 540
SPLIT_FRACTIONS = {"train": 0.70, "validation": 0.15, "test": 0.15}


def make_splits(frame):
    """Rank customer IDs deterministically within each class, as in the team code."""
    if frame.customerid.isna().any() or frame.customerid.duplicated().any():
        raise ValueError("Curated input must contain one row per customer")
    if not frame.churn_label.isin([0, 1]).all():
        raise ValueError("Expected labeled rows from split=train")
    assignment = {}
    edges = np.cumsum(list(SPLIT_FRACTIONS.values()))
    names = list(SPLIT_FRACTIONS)
    for _, group in frame.groupby("churn_label", sort=True):
        def rank(customer):
            digest = hashlib.blake2b(
                f"{SPLIT_SEED}:{int(customer)}".encode(), digest_size=8).digest()
            return int.from_bytes(digest, "big"), int(customer)
        ids = sorted(group.customerid, key=rank)
        for index, customer in enumerate(ids):
            position = (index + 0.5) / len(ids)
            assignment[customer] = names[np.searchsorted(edges, position, side="right")]
    splits = frame.customerid.map(assignment)
    for name in names:
        if frame.loc[splits == name, "churn_label"].nunique() != 2:
            raise ValueError(f"{name} needs both classes")
    return splits


def prepare(frame):
    out = frame.drop(columns=NON_FEATURES, errors="ignore").copy()
    out["age_unknown"] = ((out.agehh1 == 0) | (out.agehh2 == 0)).astype(int)
    out[["agehh1", "agehh2"]] = out[["agehh1", "agehh2"]].replace(0, np.nan)
    out["handsetprice_unknown"] = out.handsetprice.isna().astype(int)
    out["usage_record_missing"] = out[USAGE].isna().all(axis=1).astype(int)
    return out


def feature_name(column, level):
    safe = "".join(c if c.isalnum() else "_" for c in str(level)).strip("_").lower()
    return f"{column}__{safe or 'blank'}"


def fit(frame, version):
    prepared = prepare(frame)
    contract = {
        "preprocessing_version": version,
        "split_seed": SPLIT_SEED,
        "split_fractions": SPLIT_FRACTIONS,
        "numeric_medians": {}, "category_levels": {}, "service_area_encoding": {},
        "feature_names": [], "recalibration_factor": 1.0,
    }
    for name in prepared:
        values = prepared[name]
        if name == "servicearea":
            contract["service_area_encoding"] = values.fillna("unknown").astype(str).value_counts(normalize=True).to_dict()
        elif pd.api.types.is_numeric_dtype(values):
            median = values.replace([np.inf, -np.inf], np.nan).median()
            contract["numeric_medians"][name] = float(median) if pd.notna(median) else 0.0
        else:
            contract["category_levels"][name] = sorted(values.fillna("unknown").astype(str).unique().tolist())
    contract["feature_names"] = list(transform(frame, contract).columns)
    if len(set(contract["feature_names"])) != len(contract["feature_names"]):
        raise ValueError("Encoded feature names collide")
    return contract


def transform(frame, contract):
    prepared = prepare(frame)
    columns = {}
    for name, median in contract["numeric_medians"].items():
        columns[name] = pd.to_numeric(prepared[name], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(median)
    columns["servicearea"] = prepared.servicearea.fillna("unknown").astype(str).map(contract["service_area_encoding"]).fillna(0.0)
    for name, levels in contract["category_levels"].items():
        values = prepared[name].fillna("unknown").astype(str)
        for level in levels[1:]:
            output = feature_name(name, level)
            if output in columns:
                raise ValueError(f"Encoded feature name collision: {output}")
            columns[output] = (values == level).astype(int)
    result = pd.DataFrame(columns, index=frame.index)
    if contract["feature_names"]:
        result = result[contract["feature_names"]]
    return result.astype(float)
