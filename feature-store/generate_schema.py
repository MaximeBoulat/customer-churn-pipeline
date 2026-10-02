"""Regenerate the Terraform schema from actual curated data; no ingestion."""
import argparse
import json
from pathlib import Path

import preprocess
from process import read_curated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--curated-directory", required=True,
                        help="Local copy of curated split=train Parquet files")
    args = parser.parse_args()
    frame = read_curated(args.curated_directory)
    split = preprocess.make_splits(frame)
    fitted = preprocess.fit(frame[split == "train"], "schema-generation")
    fields = [
        {"FeatureName": "customerid", "FeatureType": "Integral"},
        {"FeatureName": "churn_label", "FeatureType": "Integral"},
        {"FeatureName": "split_name", "FeatureType": "String"},
        {"FeatureName": "event_time", "FeatureType": "Fractional"},
        {"FeatureName": "execution_id", "FeatureType": "String"},
    ]
    fields += [{"FeatureName": name, "FeatureType": "Fractional"} for name in fitted["feature_names"]]
    Path(__file__).with_name("schema.json").write_text(json.dumps(fields, indent=2) + "\n")


if __name__ == "__main__":
    main()
