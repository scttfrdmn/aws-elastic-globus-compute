"""A Lambda Runtime API client: ``aegc-worker lambda`` (#75).

Worker images are aarchsci/aarchbio environments (Python 3.14 and newer) or any arm64 base, so
rather than add ``awslambdaric`` (a compiled extension, without wheels for every Python), the
worker speaks the Runtime API itself: a few HTTP calls to ``AWS_LAMBDA_RUNTIME_API``. The image is
unchanged; a Lambda pool's function sets its command to ``aegc-worker lambda``.

https://docs.aws.amazon.com/lambda/latest/dg/runtimes-api.html
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("aegc.worker")

API = "2018-06-01"


@dataclass
class Context:
    """The parts of Lambda's context object the handler uses."""

    aws_request_id: str
    deadline_ms: int
    invoked_function_arn: str = ""
    function_name: str = ""
    clock: Callable[[], float] = time.time

    def get_remaining_time_in_millis(self) -> int:
        return max(0, self.deadline_ms - int(self.clock() * 1000))


Handler = Callable[[dict[str, Any], Context], Any]


class RuntimeApi:
    def __init__(self, endpoint: str, *, timeout_s: float = 30) -> None:
        self.base = f"http://{endpoint}/{API}/runtime"
        self.timeout_s = timeout_s

    def next(self) -> tuple[dict[str, Any], Context]:
        # No timeout: this blocks until the next event (the environment is frozen meanwhile).
        with urllib.request.urlopen(f"{self.base}/invocation/next") as r:
            body = r.read()
            h = r.headers
        trace = h.get("Lambda-Runtime-Trace-Id")
        if trace:
            os.environ["_X_AMZN_TRACE_ID"] = trace
        ctx = Context(
            aws_request_id=h.get("Lambda-Runtime-Aws-Request-Id", ""),
            deadline_ms=int(h.get("Lambda-Runtime-Deadline-Ms", "0") or 0),
            invoked_function_arn=h.get("Lambda-Runtime-Invoked-Function-Arn", ""),
            function_name=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
        )
        return (json.loads(body) if body else {}), ctx

    def respond(self, request_id: str, result: Any) -> None:
        self._post(f"invocation/{request_id}/response", result)

    def error(self, request_id: str, e: BaseException) -> None:
        self._post(f"invocation/{request_id}/error", _error(e), unhandled=True)

    def init_error(self, e: BaseException) -> None:
        self._post("init/error", _error(e), unhandled=True)

    def _post(self, path: str, body: Any, *, unhandled: bool = False) -> None:
        headers = {"Content-Type": "application/json"}
        if unhandled:
            headers["Lambda-Runtime-Function-Error-Type"] = "Unhandled"
        req = urllib.request.Request(
            f"{self.base}/{path}", data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            r.read()


def _error(e: BaseException) -> dict[str, Any]:
    return {
        "errorMessage": str(e),
        "errorType": type(e).__name__,
        "stackTrace": traceback.format_exception(type(e), e, e.__traceback__),
    }


def serve(
    handler: Handler,
    *,
    api: RuntimeApi | None = None,
    init: Callable[[], object] | None = None,
    max_invocations: int | None = None,
) -> int:
    """Run ``handler`` for each invocation until the environment goes (``max_invocations``
    is for tests). Returns how many invocations ran."""
    api = api or RuntimeApi(os.environ["AWS_LAMBDA_RUNTIME_API"])
    if init is not None:
        try:
            init()
        except Exception as e:
            log.exception("init failed")
            api.init_error(e)
            raise
    n = 0
    while max_invocations is None or n < max_invocations:
        event, ctx = api.next()
        n += 1
        try:
            result = handler(event, ctx)
        except Exception as e:
            log.exception("invocation %s failed", ctx.aws_request_id)
            api.error(ctx.aws_request_id, e)
            continue
        api.respond(ctx.aws_request_id, result)
    return n
