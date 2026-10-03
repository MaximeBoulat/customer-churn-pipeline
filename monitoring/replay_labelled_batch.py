"""Create an explicitly labelled test replay for quality and bias monitoring."""
import argparse
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tarfile
import uuid

import boto3
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "batch-transform"))
import config
import run_batch_inference as batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract-uri", required=True)
    args = parser.parse_args()
    import xgboost as xgb
    session = boto3.Session(profile_name=config.AWS_PROFILE, region_name=config.REGION)
    if session.client("sts").get_caller_identity()["Account"] != config.LAB_ACCOUNT_ID:
        raise ValueError("AWS profile is not using the configured Learner Lab account")
    s3 = session.client("s3")
    contract, fitted, threshold = batch.load_approved(s3, session.client("sagemaker"), args.contract_uri)
    execution = contract["pipeline_execution_arn"].rsplit("/", 1)[-1]
    base = f"s3://{config.OUTPUT_BUCKET}/pipeline/{execution}/process"
    table = pd.read_csv(io.BytesIO(batch.read(s3, base + "/test/test.csv")), header=None)
    ids = pd.read_csv(io.BytesIO(batch.read(s3, base + "/identifiers/test.csv")), header=None).iloc[:,0]
    if len(ids) != len(table) or ids.isna().any() or ids.duplicated().any() or table.shape[1] != 1+len(fitted["feature_names"]):
        raise ValueError("Test labels, identifiers and feature columns must align")
    labels = table.iloc[:,0].astype(int)
    features = table.iloc[:,1:].copy(); features.columns = fitted["feature_names"]
    with tarfile.open(fileobj=io.BytesIO(batch.read(s3, contract["model_artifact"]["uri"])), mode="r:gz") as archive:
        members = [m for m in archive.getmembers() if m.isfile() and Path(m.name).name == "xgboost-model"]
        if len(members) != 1:
            raise ValueError("Expected one native XGBoost model")
        model_bytes = bytearray(archive.extractfile(members[0]).read())
    booster = xgb.Booster(); booster.load_model(model_bytes)
    probabilities = booster.predict(xgb.DMatrix(features.to_numpy()))
    if not np.isfinite(probabilities).all():
        raise ValueError("Non-finite replay predictions")
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-replay-" + uuid.uuid4().hex[:8]
    prefix = f"inference/cell2cell/{run_id}"
    group, version = contract["model_package_arn"].rsplit("/",2)[-2:]
    scores = pd.DataFrame({"customerid": ids, "churn_probability": probabilities,
        "outreach_flag": (probabilities >= threshold["value"]).astype(int),
        "model_version": f"{group}/{version}", "preprocessing_version": contract["preprocessing_version"],
        "scored_at": now.isoformat()})
    for name, data in [("scores.csv",scores), ("features.csv",pd.concat([ids.rename("customerid"),features],axis=1)),
                       ("labels.csv",pd.DataFrame({"customerid":ids,"churn_label":labels}))]:
        batch.put(s3,prefix,name,data.to_csv(index=False).encode(),"text/csv")
    manifest = {"schema_version":1,"run_id":run_id,"scored_at":now.isoformat(),"rows":len(scores),
        "flagged":int(scores.outreach_flag.sum()),"model_package_arn":contract["model_package_arn"],
        "model_version":f"{group}/{version}","execution_id":execution,"threshold":threshold,
        "approved_contract_uri":args.contract_uri,"preprocessing_version":contract["preprocessing_version"],
        "input_uri":base+"/test/test.csv","labels_uri":f"s3://{config.OUTPUT_BUCKET}/{prefix}/labels.csv",
        "replay":True,"source":"labelled replay of the test split"}
    batch.put(s3,prefix,"manifest.json",json.dumps(manifest,indent=2,allow_nan=False).encode(),"application/json")
    print(f"Replay run ID: {run_id}")
    print(f"s3://{config.OUTPUT_BUCKET}/{prefix}/")


if __name__ == "__main__":
    main()
