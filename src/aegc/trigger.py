"""The S3-event trigger's Lambda handler (#83): start a Globus flow for each new object.

EventBridge delivers S3 ``Object Created`` events for the trigger's bucket and key filter. The
handler renders the trigger's input template (``{bucket}``, ``{key}``, ``{size}``, ``{etag}`` in
string values), and starts the flow with the user's stored Globus login: a globus-sdk ``UserApp``
whose token storage is seeded from the trigger's secret. Globus Auth refresh tokens don't rotate
(they stay valid while used and expire after six months unused), so the storage is never
written back; a refresh that returns a different refresh token is logged (run
``aegc trigger login`` again).

Packaged as a zip with globus-sdk only (boto3 is in the runtime): this module must import
nothing else of aegc. Failures raise, so Lambda's async retries and the dead-letter queue apply.
Tokens are never logged.

Environment: ``AEGC_DEPLOYMENT``, ``AEGC_TRIGGER``, ``AEGC_TRIGGER_SECRET`` (the secret's
name), ``AEGC_FLOW_ID``, ``AEGC_TRIGGER_INPUT`` (the template, JSON).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

#: Placeholders a template's string values may use.
PLACEHOLDERS = ("bucket", "key", "size", "etag")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
#: Flows' run label limit.
LABEL_MAX = 64
TAG = "aegc-trigger"


class TemplateError(ValueError):
    """An input template that isn't a JSON object, or uses an unknown placeholder."""


def check_template(template: Any) -> dict[str, Any]:
    """The template (a JSON object, or its text), checked: only known placeholders."""
    if isinstance(template, str):
        try:
            template = json.loads(template)
        except json.JSONDecodeError as e:
            raise TemplateError(f"the input template isn't JSON: {e}") from None
    if not isinstance(template, dict):
        raise TemplateError("the input template must be a JSON object (the flow's input)")

    def walk(v: Any) -> None:
        if isinstance(v, str):
            for name in _PLACEHOLDER.findall(v):
                if name not in PLACEHOLDERS:
                    raise TemplateError(
                        f"unknown placeholder {{{name}}} in the input template; use "
                        + ", ".join(f"{{{p}}}" for p in PLACEHOLDERS)
                    )
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(template)
    return template


def render(template: Any, values: dict[str, str]) -> Any:
    """The template with each ``{name}`` in its string values replaced (keys are left alone).
    A value that is exactly ``"{size}"`` becomes the number."""
    if isinstance(template, str):
        if template == "{size}" and values.get("size", "").isdigit():
            return int(values["size"])
        return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)
    if isinstance(template, dict):
        return {k: render(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, values) for v in template]
    return template


def event_values(event: dict[str, Any]) -> dict[str, str]:
    """``bucket``, ``key``, ``size``, ``etag`` of an EventBridge S3 ``Object Created`` event
    (the key as the event carries it)."""
    if event.get("source") != "aws.s3" or event.get("detail-type") != "Object Created":
        raise ValueError(
            f"not an S3 Object Created event: {event.get('source')}/{event.get('detail-type')}"
        )
    detail = event.get("detail") or {}
    obj = detail.get("object") or {}
    return {
        "bucket": str((detail.get("bucket") or {}).get("name", "")),
        "key": str(obj.get("key", "")),
        "size": str(obj.get("size", "")),
        "etag": str(obj.get("etag", "")),
    }


def label(key: str) -> str:
    """The run's label: ``s3:<key>``, the key's end kept if it's too long."""
    text = f"s3:{key}"
    return text if len(text) <= LABEL_MAX else "s3:…" + key[-(LABEL_MAX - 4) :]


def _log(**fields: Any) -> None:
    print(json.dumps({"time": time.time(), **fields}, sort_keys=True), file=sys.stdout, flush=True)


# --- the stored login ---------------------------------------------------------------------------


def _secret_value(name: str) -> str:  # replaced in tests
    import boto3

    return str(boto3.client("secretsmanager").get_secret_value(SecretId=name)["SecretString"])


def _storage(login: dict[str, Any]) -> Any:
    """In-memory token storage seeded from the secret; a refresh is kept in memory only."""
    from globus_sdk.token_storage import MemoryTokenStorage, TokenStorageData

    class SecretBackedStorage(MemoryTokenStorage):
        def store_token_data_by_resource_server(self, data: Any) -> None:
            for rs, td in data.items():
                old = self.get_token_data(rs)
                if old is not None and td.refresh_token and td.refresh_token != old.refresh_token:
                    note = "not written back; run `aegc trigger login` again if runs fail"
                    _log(event="refresh_token_changed", resource_server=rs, note=note)
            super().store_token_data_by_resource_server(data)

    storage = SecretBackedStorage()
    storage.store_token_data_by_resource_server(
        {rs: TokenStorageData.from_dict(td) for rs, td in dict(login["tokens"]).items()}
    )
    return storage


class LoginExpired(RuntimeError):
    """The stored login no longer meets the flow's scope (expired, revoked or changed)."""


def _no_interactive_login(app: Any, error: Exception) -> None:
    # The default handler would start a login flow, prompting on stdin: there's no one here.
    raise LoginExpired(
        f"the stored Globus login is no longer valid ({type(error).__name__}); run "
        "`aegc trigger login` again"
    )


def _flow_client(flow_id: str, login: dict[str, Any]) -> Any:  # replaced in tests
    from globus_sdk import GlobusAppConfig, SpecificFlowClient, UserApp

    app = UserApp(
        "aegc-trigger",
        client_id=str(login["client_id"]),
        config=GlobusAppConfig(
            token_storage=_storage(login),
            request_refresh_tokens=True,
            token_validation_error_handler=_no_interactive_login,
        ),
    )
    return SpecificFlowClient(flow_id, app=app)


_LOGIN: dict[str, Any] | None = None  # cached per execution environment


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    global _LOGIN
    deployment = os.environ.get("AEGC_DEPLOYMENT", "")
    trigger = os.environ.get("AEGC_TRIGGER", "")
    flow_id = os.environ["AEGC_FLOW_ID"]
    values = event_values(event)
    body = render(check_template(os.environ.get("AEGC_TRIGGER_INPUT") or "{}"), values)
    try:
        if _LOGIN is None:
            _LOGIN = dict(json.loads(_secret_value(os.environ["AEGC_TRIGGER_SECRET"])))
        run = _flow_client(flow_id, _LOGIN).run_flow(
            body,
            label=label(values["key"]),
            tags=[TAG, f"aegc:{deployment}", f"trigger:{trigger}"],
        )
        run_id = str(run["run_id"])
    except Exception as e:
        # The error's type and message only: never the request (tokens are in its headers).
        _log(event="run_failed", deployment=deployment, trigger=trigger, flow_id=flow_id,
             bucket=values["bucket"], key=values["key"], error=type(e).__name__,
             message=str(e)[:500])  # fmt: skip
        raise
    _log(event="run_started", deployment=deployment, trigger=trigger, flow_id=flow_id,
         bucket=values["bucket"], key=values["key"], run_id=run_id)  # fmt: skip
    return {"run_id": run_id}
