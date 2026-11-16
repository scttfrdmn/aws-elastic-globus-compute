"""Worker logs as one JSON object per line (stdlib only).

Each line has ``ts`` (UTC, ISO 8601), ``level``, ``logger`` and ``msg``, then the worker's context
(``deployment``, ``pool``, ``instance_id``, ``type``, ``az``, ``lifecycle``; set once at start with
:func:`set_context`), then whatever the call passed with ``extra=`` (per-task ``task_uuid``,
``attempt``, ``etid``, ``receive_count``, ``outcome``, phase times). awslogs ships the lines to
``/aegc/<d>``, where CloudWatch Logs Insights parses them, and ``aegc provenance`` finds a task's
``task_finished`` line.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import IO, Any

#: Attributes every LogRecord has; anything else on a record came from ``extra=``.
_STANDARD = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
}

CONTEXT_FIELDS = ("deployment", "pool", "instance_id", "type", "az", "lifecycle")

_context: dict[str, Any] = {}


def set_context(**fields: Any) -> None:
    """Fields every line carries from now on (empty values are left out)."""
    unknown = set(fields) - set(CONTEXT_FIELDS)
    if unknown:
        raise ValueError(f"unknown log context fields {sorted(unknown)}")
    for k, v in fields.items():
        if v in ("", None):
            _context.pop(k, None)
        else:
            _context[k] = v


def context() -> dict[str, Any]:
    return dict(_context)


def _ts(created: float) -> str:
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created))
        + f".{int(created % 1 * 1000):03d}Z"
    )


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": _ts(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_context,
        }
        for k, v in record.__dict__.items():
            if k not in _STANDARD and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack"] = self.formatStack(record.stack_info)
        return json.dumps(out, default=str, separators=(",", ":"))


def configure(stream: IO[str] | None = None, level: int = logging.INFO) -> logging.Handler:
    """Send every logger's records, as JSON lines, to ``stream`` (default stderr)."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)
    return handler
