"""Workarounds for the places where substrate still differs from AWS.

Each one names the substrate issue that tracks it. When substrate fixes the issue, delete the
workaround and its uses, so the test exercises substrate directly.

Gaps without code here (substrate v0.131.0 fixed #1594-#1596, #1618, #1620, #1622, #1623):
- substrate#1524: no ECR lifecycle policy preview (a strict xfail in test_images.py).
- substrate#1522 (partial in v0.131.0): DescribeNetworkInterfaces answers, but instances have no
  primary ENI, so ``verify_destroyed``'s ENI check always sees none.
"""

from __future__ import annotations

import fnmatch
import re
from types import SimpleNamespace
from typing import Any


def instance_type_offerings(ec2: Any, offered: dict[str, list[str]]) -> list[dict[str, Any]]:
    """substrate#1621: DescribeInstanceTypeOfferings offers every type in every
    zone, so a zone without Graviton (us-east-1's use1-az3) can't be modelled. Subnets also lack
    AvailabilityZoneId.

    Answers it on this client from ``offered`` (zone name -> instance types), applying the
    ``instance-type`` filter's globs as EC2 does, for LocationType=availability-zone. Returns a
    list that collects each call's request body, so callers can check the caching.
    """
    calls: list[dict[str, Any]] = []

    def answer(params: dict[str, Any], **_: Any) -> tuple[Any, dict[str, Any]]:
        body = dict(params.get("body") or {})
        calls.append(body)
        patterns: list[str] = []
        for key, name in body.items():
            m = re.fullmatch(r"Filter\.(\d+)\.Name", key)
            if m and name == "instance-type":
                n = m.group(1)
                patterns += [v for k, v in body.items() if k.startswith(f"Filter.{n}.Value.")]
        found = [
            {"InstanceType": t, "LocationType": "availability-zone", "Location": zone}
            for zone, types in offered.items()
            for t in types
            if not patterns or any(fnmatch.fnmatchcase(t, p) for p in patterns)
        ]
        return SimpleNamespace(status_code=200, headers={}), {"InstanceTypeOfferings": found}

    ec2.meta.events.register("before-call.ec2.DescribeInstanceTypeOfferings", answer)
    return calls


def _stub(client: Any, service: str, op: str, respond: Any) -> None:
    """Answer ``op`` on this client with ``respond(api_params)`` instead of calling substrate."""
    last: dict[str, Any] = {}

    def capture(params: dict[str, Any], **_: Any) -> None:
        last.clear()
        last.update(params)

    def answer(**_: Any) -> tuple[Any, dict[str, Any]]:
        return SimpleNamespace(status_code=200, headers={}), respond(dict(last))

    client.meta.events.register(f"before-parameter-build.{service}.{op}", capture)
    client.meta.events.register(f"before-call.{service}.{op}", answer)


def instance_refreshes(autoscaling: Any) -> list[dict[str, Any]]:
    """substrate#1624: no StartInstanceRefresh. Records the requests on this client."""
    calls: list[dict[str, Any]] = []

    def start(p: dict[str, Any]) -> dict[str, Any]:
        calls.append(p)
        return {"InstanceRefreshId": "r-1"}

    _stub(autoscaling, "auto-scaling", "StartInstanceRefresh", start)
    return calls


#: Function settings substrate accepts but doesn't return (substrate#1659).
_FUNCTION_ECHOES = ("EphemeralStorage", "LoggingConfig")
#: Mapping settings substrate accepts but doesn't return (substrate#1659).
_MAPPING_ECHOES = ("FunctionResponseTypes", "ScalingConfig")


def lambda_echoes(lam: Any) -> dict[str, list[dict[str, Any]]]:
    """substrate#1659: Lambda drops settings it was given. Functions come back without
    ImageConfigResponse, LoggingConfig and EphemeralStorage; event source mappings without
    FunctionResponseTypes, ScalingConfig and EventSourceMappingArn.

    substrate#1660: a mapping's tags (Tags on CreateEventSourceMapping) aren't kept, and ListTags
    on a mapping's ARN is ResourceNotFound ("Function not found"). Also without code here:
    GetFunctionConfiguration is UnknownOperationException (deploy uses GetFunction), GetFunction
    returns no Tags (deploy uses ListTags), functions have no LastUpdateStatus, and the tagging
    API doesn't list mappings.

    Records the requests on this client and fills those fields into its responses, as AWS
    returns them; answers ListTags for mapping ARNs from the tags they were created with.
    Returns the requests by operation, so tests can check what was sent.
    """
    sent: dict[str, list[dict[str, Any]]] = {}
    last: dict[str, dict[str, Any]] = {}
    functions: dict[str, dict[str, Any]] = {}
    mappings: dict[str, dict[str, Any]] = {}

    def capture(op: str) -> Any:
        def hook(params: dict[str, Any], **_: Any) -> None:
            last[op] = dict(params)
            sent.setdefault(op, []).append(dict(params))

        return hook

    def fill_function(cfg: dict[str, Any]) -> None:
        name = str(cfg.get("FunctionName", ""))
        seen = functions.get(name, {})
        for k in _FUNCTION_ECHOES:
            if k in seen and k not in cfg:
                cfg[k] = seen[k]
        if "ImageConfig" in seen and "ImageConfigResponse" not in cfg:
            cfg["ImageConfigResponse"] = {"ImageConfig": seen["ImageConfig"]}

    def fill_mapping(m: dict[str, Any]) -> None:
        uuid = str(m.get("UUID", ""))
        seen = mappings.get(uuid, {})
        for k in _MAPPING_ECHOES:
            if k in seen and k not in m:
                m[k] = seen[k]
        if uuid and "EventSourceMappingArn" not in m:
            region, account = str(m.get("FunctionArn", "")).split(":")[3:5]
            m["EventSourceMappingArn"] = (
                f"arn:aws:lambda:{region}:{account}:event-source-mapping:{uuid}"
            )

    def on_function(op: str) -> Any:
        def hook(parsed: dict[str, Any], **_: Any) -> None:
            req = last.get(op, {})
            name = str(req.get("FunctionName", ""))
            functions.setdefault(name, {}).update(
                {k: v for k, v in req.items() if k in (*_FUNCTION_ECHOES, "ImageConfig")}
            )
            fill_function(parsed)

        return hook

    def on_mapping(op: str) -> Any:
        def hook(parsed: dict[str, Any], **_: Any) -> None:
            req = last.get(op, {})
            uuid = str(parsed.get("UUID") or req.get("UUID", ""))
            mappings.setdefault(uuid, {}).update(
                {k: v for k, v in req.items() if k in (*_MAPPING_ECHOES, "Tags")}
            )
            fill_mapping(parsed)

        return hook

    def get_function(parsed: dict[str, Any], **_: Any) -> None:
        if isinstance(parsed.get("Configuration"), dict):
            fill_function(parsed["Configuration"])

    def list_mappings(parsed: dict[str, Any], **_: Any) -> None:
        for m in parsed.get("EventSourceMappings", []):
            fill_mapping(m)

    def mapping_tags(**_: Any) -> Any:
        resource = str(last.get("ListTags", {}).get("Resource", ""))
        if ":event-source-mapping:" not in resource:
            return None  # a function: substrate answers
        uuid = resource.rsplit(":", 1)[-1]
        if uuid not in mappings:
            err = {"Type": "User", "Message": f"not found: {uuid}"}
            return SimpleNamespace(status_code=404, headers={}), {
                "Error": {"Code": "ResourceNotFoundException", **err}
            }
        return SimpleNamespace(status_code=200, headers={}), {
            "Tags": dict(mappings[uuid].get("Tags") or {})
        }

    ev = lam.meta.events
    for op in ("CreateFunction", "UpdateFunctionConfiguration", "UpdateFunctionCode",
               "CreateEventSourceMapping", "UpdateEventSourceMapping", "DeleteEventSourceMapping",
               "DeleteFunction", "ListTags"):  # fmt: skip
        ev.register(f"before-parameter-build.lambda.{op}", capture(op))
    for op in ("CreateFunction", "UpdateFunctionConfiguration"):
        ev.register(f"after-call.lambda.{op}", on_function(op))
    for op in ("CreateEventSourceMapping", "UpdateEventSourceMapping"):
        ev.register(f"after-call.lambda.{op}", on_mapping(op))
    ev.register("after-call.lambda.GetFunction", get_function)
    ev.register("after-call.lambda.GetEventSourceMapping", lambda parsed, **_: fill_mapping(parsed))
    ev.register("after-call.lambda.ListEventSourceMappings", list_mappings)
    ev.register("before-call.lambda.ListTags", mapping_tags)
    return sent


def sender_ids(sqs: Any, who: Any) -> Any:
    """substrate#1676: SQS's ``SenderId`` is always the account id; AWS gives the role's unique
    id and session (``AROA...:<session>``) for an assumed role, which the engine checks (#80).

    On this client, each ``SendMessage`` carries ``who()`` (the id the test says sent it) in a
    message attribute, and ``ReceiveMessage`` reports it as ``SenderId``."""
    attr = "aegc-test-sender"

    def tag(params: dict[str, Any], **_: Any) -> None:
        sender = who()
        if sender:
            params.setdefault("MessageAttributes", {})[attr] = {
                "DataType": "String", "StringValue": f"{sender}:test"
            }  # fmt: skip

    def ask(params: dict[str, Any], **_: Any) -> None:
        names = list(params.get("MessageAttributeNames") or [])
        if "All" not in names and attr not in names:
            params["MessageAttributeNames"] = [*names, attr]

    def report(parsed: Any, **_: Any) -> None:
        for m in parsed.get("Messages", []) or []:
            a = (m.get("MessageAttributes") or {}).pop(attr, None)
            if a is not None:
                m.setdefault("Attributes", {})["SenderId"] = a["StringValue"]

    sqs.meta.events.register("provide-client-params.sqs.SendMessage", tag)
    sqs.meta.events.register("provide-client-params.sqs.ReceiveMessage", ask)
    sqs.meta.events.register("after-call.sqs.ReceiveMessage", report)
    return sqs


def rule_states(events: Any) -> list[dict[str, Any]]:
    """substrate#TBD-eventbridge-enable-disable-rule: EnableRule and DisableRule are
    UnknownOperationException ("The action DisableRule is not recognized"); PutRule, DescribeRule,
    ListRules (with State), PutTargets and tags work.

    On this client, both are answered by re-putting the rule with its own pattern and the new
    State (PutRule updates a rule in place, as AWS does). Returns the requests, in order."""
    calls: list[dict[str, Any]] = []

    def toggle(state: str) -> Any:
        def respond(p: dict[str, Any]) -> dict[str, Any]:
            calls.append({**p, "State": state})
            rule = events.describe_rule(Name=p["Name"])
            kw = {k: rule[k] for k in ("EventPattern", "Description") if rule.get(k)}
            events.put_rule(Name=p["Name"], State=state, **kw)
            return {}

        return respond

    _stub(events, "eventbridge", "EnableRule", toggle("ENABLED"))
    _stub(events, "eventbridge", "DisableRule", toggle("DISABLED"))
    return calls


def function_concurrency(lam: Any) -> dict[str, int]:
    """substrate#TBD-lambda-function-concurrency: PutFunctionConcurrency and
    GetFunctionConcurrency are UnknownOperationException. Also without code here:
    GetFunctionEventInvokeConfig is UnknownOperationException (PutFunctionEventInvokeConfig
    answers, without echoing DestinationConfig).

    Records reserved concurrency by function on this client and answers both."""
    reserved: dict[str, int] = {}

    def put(p: dict[str, Any]) -> dict[str, Any]:
        reserved[str(p["FunctionName"])] = int(p["ReservedConcurrentExecutions"])
        return {"ReservedConcurrentExecutions": p["ReservedConcurrentExecutions"]}

    def get(p: dict[str, Any]) -> dict[str, Any]:
        n = reserved.get(str(p["FunctionName"]))
        return {} if n is None else {"ReservedConcurrentExecutions": n}

    _stub(lam, "lambda", "PutFunctionConcurrency", put)
    _stub(lam, "lambda", "GetFunctionConcurrency", get)
    return reserved
