"""Builds and incrementally updates the pipeline execution manifest.

The single record mapping one `07_pipeline` execution to the exact artifact
versions it used and produced, written to
`pipeline/{execution-id}/execution_manifest.json` (see
Arch/SystemArchitecture.md, "Traceability"), alongside the inputs and outputs
that run already writes under the same prefix.

A pipeline execution is a chain of stages (catalog -> processing ->
feature_store -> training -> evaluation -> registration -> gate), each
finishing at a different time. `model_package_version` in particular does
not exist until `registration` runs, near the end of the chain. Building
the whole manifest in one call at the end means a run that fails partway
through leaves no record at all -- exactly the run you most want a partial
trace of. So the manifest is built incrementally instead: `create_manifest`
writes a skeleton at the start, and each stage calls `update_manifest` with
its own result as soon as it finishes.

`used` holds the version axes known up front, before any stage has run,
sourced from `src/config.py` unless a caller deliberately overrides one
(e.g. retraining against an older curated version to reproduce a past
result). `produced` holds what only exists once the run actually happens --
`model_package_version` is `None` until `registration` records it.

Logic is kept separate from S3 I/O (`_initial_manifest`, `_apply_update` are
pure) so it can be unit tested without a real bucket or a mocking library.
"""

from __future__ import annotations

import json
from typing import Any

import manifest_config as config

STAGES = (
    "catalog",
    "processing",
    "feature_store",
    "training",
    "evaluation",
    "registration",
    "gate",
)


def _manifest_key(execution_id: str) -> str:
    return f"{config.PIPELINE_PREFIX}{execution_id}/execution_manifest.json"


def _initial_manifest(
    execution_id: str,
    curated_version: str | None,
    feature_version: str | None,
    preprocessing_contract: str | None,
    model_version: str | None = None,
) -> dict:
    """Build the skeleton manifest written at pipeline start. Pure: no I/O."""
    return {
        "execution_id": execution_id,
        "used": {
            "curated_version": curated_version or config.CURATED_VERSION,
            "preprocessing_contract": preprocessing_contract or config.PREPROCESSING_CONTRACT,
            "feature_version": feature_version or config.FEATURE_VERSION,
            "model_version": model_version or config.MODEL_VERSION,
        },
        "produced": {
            "model_package_version": None,
        },
        "stages": {stage: "pending" for stage in STAGES},
    }


def _apply_update(
    manifest: dict,
    stage: str,
    status: str,
    produced_fields: dict[str, Any],
) -> dict:
    """Merge one stage's status and produced fields into a manifest. Pure:
    takes and returns plain dicts, no I/O, no mutation of the input."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")

    updated = json.loads(json.dumps(manifest))  # deep copy without a dependency
    updated["stages"][stage] = status
    updated["produced"].update(produced_fields)
    return updated


def create_manifest(
    execution_id: str,
    curated_version: str | None = None,
    feature_version: str | None = None,
    preprocessing_contract: str | None = None,
    model_version: str | None = None,
) -> dict:
    """Initialize a new execution manifest and write it to S3.

    Call once, at pipeline start. `execution_id` has no default: it is a
    Pipeline execution ARN, assigned by AWS only once the run starts. The
    four version axes default from config, but can be overridden for a run
    that deliberately mixes versions.
    """
    manifest = _initial_manifest(
        execution_id, curated_version, feature_version, preprocessing_contract, model_version
    )
    _write_manifest(execution_id, manifest)
    return manifest


def read_manifest(execution_id: str) -> dict:
    """Fetch and parse the current manifest for an execution."""
    client = config.s3_client()
    body = client.get_object(Bucket=config.BUCKET_NAME, Key=_manifest_key(execution_id))["Body"]
    return json.loads(body.read())


def update_manifest(
    execution_id: str,
    stage: str,
    status: str = "completed",
    **produced_fields: Any,
) -> dict:
    """Record one stage's completion and whatever it produced.

    Read-modify-write against the manifest `create_manifest` already wrote.
    `model_package_version` is supplied here by the `registration` stage,
    once it exists -- e.g. `update_manifest(execution_id, "registration",
    model_package_version=version)`. Safe without locking or conditional
    writes because stages run sequentially within one execution; there is no
    concurrent writer to race against.
    """
    manifest = read_manifest(execution_id)
    updated = _apply_update(manifest, stage, status, produced_fields)
    _write_manifest(execution_id, updated)
    return updated


def _write_manifest(execution_id: str, manifest: dict) -> None:
    client = config.s3_client()
    client.put_object(
        Bucket=config.BUCKET_NAME,
        Key=_manifest_key(execution_id),
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
