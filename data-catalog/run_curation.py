"""Create curated Parquet, then the training view. Run after terraform apply."""
import sys
import time
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config


def main():
    session = boto3.Session(profile_name=config.AWS_PROFILE, region_name=config.REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != config.LAB_ACCOUNT_ID:
        raise SystemExit(f"Expected lab account {config.LAB_ACCOUNT_ID}, got {account}")
    athena = session.client("athena")
    print(f"Database: {config.DATABASE}\nParquet: {config.CURATED_LOCATION}")
    for name in ("01_create_curated.sql", "02_create_view.sql"):
        sql = (Path(__file__).parent / "sql" / name).read_text()
        sql = sql.replace("${database}", config.DATABASE)
        sql = sql.replace("${curated_location}", config.CURATED_LOCATION)
        query_id = athena.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": config.DATABASE},
            WorkGroup=config.WORKGROUP,
        )["QueryExecutionId"]
        print(f"{name}: {query_id}", flush=True)
        while True:
            status = athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]["Status"]
            if status["State"] == "SUCCEEDED":
                print("Succeeded", flush=True)
                break
            if status["State"] in ("FAILED", "CANCELLED"):
                raise SystemExit(status.get("StateChangeReason", status["State"]))
            time.sleep(2)


if __name__ == "__main__":
    main()
