"""AWS HealthOmics runs as steps of a Globus flow (#84).

A flow starts a run with :func:`start_run` on a Lambda (or EC2) pool, then loops: a Flows
``Wait`` state, :func:`run_status`, and a ``Choice`` on ``done``, until the run finishes; later
steps read its outputs from ``output_uri``. The deployment needs an omics grant for the
workflow and the run's service role (``aegc deploy --grant omics=WORKFLOW_ID,role=ROLE_ARN``):
it may start runs of that workflow only tagged with the deployment, and read or cancel only runs
so tagged.

Both functions import nothing from aegc at module level and return JSON, so Globus Compute can
call them by name from a flow (register them once with :func:`register`).
"""

from __future__ import annotations

from typing import Any

#: GetRun statuses after which a run won't change.
DONE = frozenset({"COMPLETED", "FAILED", "CANCELLED", "DELETED"})


def start_run(
    workflow_id: str,
    role_arn: str,
    output_uri: str,
    parameters: dict[str, Any] | None = None,
    name: str = "",
    storage_type: str = "DYNAMIC",
    deployment: str = "",
) -> dict[str, Any]:
    """Start a HealthOmics run; returns ``{"run_id", "arn", "status", "output_uri"}``.

    ``workflow_id`` is a private workflow's id or ``ready2run:<id>``. The run is tagged with the
    deployment (``AEGC_DEPLOYMENT`` in the task's environment, or ``deployment``), which the
    grant requires.
    """
    import os
    import uuid

    import boto3

    d = deployment or os.environ.get("AEGC_DEPLOYMENT", "")
    if not d:
        raise ValueError("no deployment to tag the run with: pass deployment=")
    ready2run = workflow_id.startswith("ready2run:")
    kw: dict[str, Any] = {
        "workflowId": workflow_id.removeprefix("ready2run:"),
        "workflowType": "READY2RUN" if ready2run else "PRIVATE",
        "roleArn": role_arn,
        "outputUri": output_uri,
        "parameters": parameters or {},
        "name": (name or f"aegc-{d}")[:128],
        "requestId": str(uuid.uuid4()),
        "tags": {"aegc:deployment": d},
    }
    if not ready2run:
        kw["storageType"] = storage_type
    r = boto3.client("omics").start_run(**kw)
    return {
        "run_id": r["id"],
        "arn": r.get("arn", ""),
        "status": r.get("status", "PENDING"),
        "output_uri": output_uri,
    }


def run_status(run_id: str) -> dict[str, Any]:
    """``{"run_id", "status", "done", "failed", "message", "output_uri"}`` for a flow's
    ``Choice`` state: loop while ``done`` is false."""
    import boto3

    r = boto3.client("omics").get_run(id=run_id)
    status = str(r.get("status", ""))
    return {
        "run_id": run_id,
        "status": status,
        "done": status in DONE,
        "failed": status in DONE and status != "COMPLETED",
        "message": str(r.get("statusMessage") or r.get("failureReason") or ""),
        "output_uri": str(r.get("runOutputUri") or r.get("outputUri") or ""),
    }


def register(client: Any = None) -> dict[str, str]:
    """Register both functions for Globus Flows (as source text); returns their ids."""
    from globus_compute_sdk import Client
    from globus_compute_sdk.serialize import PureSourceTextInspect

    gcc = client or Client(code_serialization_strategy=PureSourceTextInspect())
    return {
        "start_function": gcc.register_function(start_run),
        "status_function": gcc.register_function(run_status),
    }
