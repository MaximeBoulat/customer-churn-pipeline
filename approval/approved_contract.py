"""Pin a pipeline candidate with its existing preprocessing contract.

No resource is selected by 'latest'. All AWS clients are injected for offline tests.
"""
from __future__ import annotations

import json
import math
from urllib.parse import urlparse

from botocore.exceptions import ClientError



def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def location(uri):
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError(f"Expected an S3 object URI: {uri}")
    if parsed.query or parsed.fragment:
        raise ValueError("Use an exact S3 object key without query/fragment")
    return {"Bucket": parsed.netloc, "Key": parsed.path.lstrip("/")}


def read(s3, uri):
    response = s3.get_object(**location(uri))
    return response["Body"].read(), response["ETag"]


def write_once(s3, uri, body):
    """Create only; allow identical retries, never overwrite different bytes."""
    try:
        s3.put_object(**location(uri), Body=body, IfNoneMatch="*", ServerSideEncryption="AES256")
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
            raise
        if read(s3, uri)[0] != body:
            raise ValueError(f"Pinned object already exists with different content: {uri}") from exc


def snapshot(s3, source, destination):
    body, _ = read(s3, source)
    write_once(s3, destination, body)
    return {"uri": destination, "source_uri": source}


def require_approved(package):
    if package.get("ModelPackageStatus") != "Completed" or package.get("ModelApprovalStatus") != "Approved":
        raise ValueError("The exact package must be Completed and Approved to finalize the approved contract")


def package_container(package):
    containers = package["InferenceSpecification"]["Containers"]
    if len(containers) != 1 or "ModelDataUrl" not in containers[0]:
        raise ValueError("This entry point supports single-container XGBoost packages")
    return containers[0]


def pin(sm, s3, *, execution_arn, note="", approve=False):
    """Publish a package-specific contract independently of run bookkeeping.

    Consumers receive the returned reference through deployment configuration.
    Registry approval remains authoritative; a contract object alone does not authorize use of a package.
    """
    steps = []
    token = None
    while True:
        result = sm.list_pipeline_execution_steps(PipelineExecutionArn=execution_arn, **({"NextToken": token} if token else {}))
        steps.extend(result["PipelineExecutionSteps"])
        token = result.get("NextToken")
        if not token:
            break
    by_name = {s["StepName"]: s for s in steps}
    reg = by_name.get("RegisterModel", {})
    package_arn = reg.get("Metadata", {}).get("RegisterModel", {}).get("Arn")
    if reg.get("StepStatus") != "Succeeded" or not package_arn:
        raise ValueError("Execution has no successfully registered model package")
    if package_arn.split(":")[3:5] != execution_arn.split(":")[3:5]:
        raise ValueError("Package and execution must be in the same account and region")
    package = sm.describe_model_package(ModelPackageName=package_arn)
    if package["ModelPackageStatus"] != "Completed" or package["ModelApprovalStatus"] not in {"Approved", "PendingManualApproval"}:
        raise ValueError("Only completed pending/approved packages can be finalized")
    if not approve:
        require_approved(package)
    training_step = by_name["Train"]
    training = sm.describe_training_job(TrainingJobName=training_step["Metadata"]["TrainingJob"]["Arn"].rsplit("/", 1)[-1])
    process = sm.describe_processing_job(ProcessingJobName=by_name["Preprocess"]["Metadata"]["ProcessingJob"]["Arn"].rsplit("/", 1)[-1])
    outputs = {x["OutputName"]: x["S3Output"]["S3Uri"].rstrip("/") for x in process["ProcessingOutputConfig"]["Outputs"]}
    container = package_container(package)
    if training["TrainingJobStatus"] != "Completed" or container["ModelDataUrl"] != training["ModelArtifacts"]["S3ModelArtifacts"]:
        raise ValueError("Package artifact does not match the execution's training job")
    preprocessing_uri = outputs["contract"] + "/preprocessing_parameters.json"
    pre_body, _ = read(s3, preprocessing_uri)
    pre = json.loads(pre_body)
    derived = {"age_unknown", "handsetprice_unknown", "usage_record_missing"}
    numeric = set(pre["numeric_medians"]) - derived
    categorical = set(pre["category_levels"]) | {"servicearea"}
    expected_schema = {**{k: "numeric" for k in numeric},
                       **{k: "categorical" for k in categorical}, "customerid": "identifier"}
    factor = pre["recalibration_factor"]
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError("Preprocessing contract requires a positive recalibration factor")
    bucket = location(container["ModelDataUrl"])["Bucket"]
    group, version = package_arn.rsplit("/", 2)[-2:]
    execution_id = execution_arn.rsplit("/", 1)[-1]
    base = f"s3://{bucket}/models/cell2cell/approvals/{group}/{execution_id}/package-{version}"
    image = container["Image"]
    model_ref = snapshot(s3, container["ModelDataUrl"], base + "/model.tar.gz")
    write_once(s3, base + "/preprocessing_parameters.json", pre_body)
    pre_ref = {"uri": base + "/preprocessing_parameters.json", "source_uri": preprocessing_uri}
    evaluation_uri = package["ModelMetrics"]["ModelQuality"]["Statistics"]["S3Uri"]
    eval_ref = snapshot(s3, evaluation_uri, base + "/evaluation.json")
    contract = {
        "format_version": 1, "model_package_arn": package_arn, "model_package_version": int(version),
        "pipeline_execution_arn": execution_arn,
        "model_version": int(version), "preprocessing_version": pre["preprocessing_version"],
        "input_schema": expected_schema, "recalibration_factor": factor,
        "model_artifact": model_ref, "preprocessing": pre_ref,
        "inference_image": image,
        "evaluation": eval_ref,
    }
    contract_uri = base + "/approved_contract.json"
    write_once(s3, contract_uri, encoded(contract))
    if approve and package["ModelApprovalStatus"] != "Approved":
        sm.update_model_package(ModelPackageArn=package_arn, ModelApprovalStatus="Approved", ApprovalDescription=note[:1024] or "Approved for the course project")
    require_approved(sm.describe_model_package(ModelPackageName=package_arn))
    return {"uri": contract_uri, "model_package_arn": package_arn}
