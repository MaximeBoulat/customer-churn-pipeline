"""Record a DAG step's stage in the execution manifest.

The DAG's steps are the stages Arch/SystemArchitecture.md names, so each
records itself into pipeline/{execution-id}/execution_manifest.json.
"""

from __future__ import annotations

import sys


def record(execution_id: str | None, stage: str, **produced) -> None:
    """Best-effort: a manifest bug must not fail an otherwise good run."""
    if not execution_id:
        print("manifest: no execution id, skipped")
        return
    try:
        sys.path.insert(0, "/opt/ml/processing/input/code")
        import manifest as mf

        try:
            mf.read_manifest(execution_id)
        except Exception:
            mf.create_manifest(execution_id)
        mf.update_manifest(execution_id, stage, **produced)
        print(f"manifest: recorded {stage}")
    except Exception as exc:
        print(f"manifest: skipped {stage}: {type(exc).__name__}: {exc}")
