#!/usr/bin/env python3
"""System 3, one run: read a scored run, measure it, publish to CloudWatch.
Quality and bias run only with --labels and are otherwise left out, not zeroed.

    python monitoring/run_monitoring.py --run-id <id> --baseline-from <execution-id>
    python monitoring/run_monitoring.py --run-id <id> [--labels s3-key] [--dry-run]
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import settings as config  # noqa: E402
import metrics as mon  # noqa: E402

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--baseline-from", metavar="EXECUTION_ID",
                        help="fit and save a baseline from this execution's training split first")
    parser.add_argument("--labels", metavar="S3_KEY",
                        help="labelled csv with customerid and churn_label; enables quality and bias")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    s3 = config.s3_client()

    if args.baseline_from:
        train = mon.read_split(args.baseline_from, "train", client=s3)
        baseline = mon.fit_baseline(
            train, curated_version=config.CURATED_VERSION,
            preprocessing_contract=config.PREPROCESSING_CONTRACT,
            model_version=config.MODEL_VERSION)
        print(f"baseline: {mon.save_baseline(baseline, client=s3)}  ({baseline.row_count:,} rows)")
    baseline = mon.load_baseline(client=s3)

    run = mon.read_scored_run(args.run_id, client=s3)
    manifest, scores, features = run["manifest"], run["scores"], run["features"]
    threshold = (manifest.get("threshold") or {}).get("value")
    if threshold is None:
        raise SystemExit("manifest has no threshold.value; the batch output contract requires it")
    print(f"run {args.run_id}: {len(scores):,} rows, model {manifest.get('model_version')}, "
          f"cutoff {threshold}")

    drift = mon.drift_report(baseline, features)
    flags = mon.flag_report(manifest, scores)
    print(f"  drift   max PSI {drift['max_psi']:.4f} on {drift['worst_feature']}, "
          f"{len(drift['features_investigate'])} to investigate")
    print(f"  flags   {flags['flagged']} of {flags['rows']} ({flags['flag_rate']:.2%})"
          + ("  NOBODY FLAGGED" if flags["nobody_flagged"] else ""))

    quality = bias = subgroups = join_summary = None
    coverage = None
    if args.labels:
        labels = pd.read_csv(io.BytesIO(
            s3.get_object(Bucket=config.BUCKET_NAME, Key=args.labels)["Body"].read()))
        join = mon.join_scored_to_labels(scores, labels)
        coverage = join.coverage
        frame = join.frame
        quality = mon.quality_report(frame["churn_label"], frame["churn_probability"], threshold)
        facet = mon.credit_rating_facet(features.set_index("customerid").loc[frame["customerid"]])
        bias = mon.bias_report(frame["churn_label"], frame["churn_probability"],
                               facet.to_numpy(), config.MONITORING_BIAS_ADVANTAGED, threshold)
        subgroups = mon.subgroup_report(
            frame.assign(credit_rating=facet.to_numpy()), frame["churn_label"],
            frame["churn_probability"], by="credit_rating", threshold=threshold)
        join_summary = join.summary()
        print(f"  join    {join.matched:,} matched, coverage {coverage:.2%}")
        print(f"  quality precision {quality['Precision']:.4f}  recall {quality['Recall']:.4f}  "
              f"F2 {quality['F2']:.4f}")
        if bias["assessed"]:
            print(f"  bias    DI {bias['DI']:.3f}" + ("  FOUR-FIFTHS BREACH" if bias["four_fifths_breach"] else ""))
        else:
            print(f"  bias    not assessed, {bias['reason']}")

    report = {
        "run_id": args.run_id, "manifest": manifest, "drift": drift, "flags": flags,
        "label_join": join_summary, "quality": quality, "bias": bias,
        "subgroups": subgroups.to_dict(orient="records") if subgroups is not None else None,
        "label_source": args.labels,
    }
    metrics = mon.cloudwatch_metrics(drift, flags, quality, bias, coverage)

    if args.dry_run:
        print(f"\ndry run: {len(metrics)} metrics not published")
        print(json.dumps([m["MetricName"] for m in metrics]))
        return

    print(f"\nreport: {mon.save_drift_report(report, args.run_id, client=s3)}")
    print(f"published {mon.publish_metrics(metrics)} metrics to {mon.NAMESPACE}")


if __name__ == "__main__":
    main()
