"""Task-role credentials for task processes (#32).

Task processes can't reach instance metadata (user data rejects it for their uid), so they don't
get the worker role. The agent assumes the deployment's task role, which holds only the grants,
and serves its credentials on 127.0.0.1 in the container-credentials format. Task processes get
``AWS_CONTAINER_CREDENTIALS_FULL_URI`` and ``AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE``, which
botocore, the AWS CLI and GDAL read and refresh by themselves.

Chained role sessions last at most an hour, so the agent renews well before expiry; a task that
runs for hours keeps getting fresh credentials.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

log = logging.getLogger("aegc.worker")

SESSION_S = 3600
#: Renew when less than this is left.
RENEW_BEFORE_S = 15 * 60
PATH = "/aegc/task-credentials"


class TaskCredentials:
    """Assume ``role_arn`` and serve the credentials to local task processes."""

    def __init__(
        self,
        *,
        sts: Any,
        role_arn: str,
        session_name: str,
        token_file: Path,
        clock: Any = time.time,
    ) -> None:
        self.sts = sts
        self.role_arn = role_arn
        self.session_name = session_name[:64]
        self.token_file = token_file
        self.clock = clock
        self._token = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._creds: dict[str, str] | None = None
        self._expires = 0.0
        self._server: ThreadingHTTPServer | None = None

    # --- credentials ---------------------------------------------------------------------------

    def current(self) -> dict[str, str]:
        """The cached credentials, renewed when they're close to expiring."""
        with self._lock:
            if self._creds is None or self._expires - self.clock() < RENEW_BEFORE_S:
                try:
                    self._renew()
                except Exception as e:
                    if self._creds is None or self._expires <= self.clock():
                        raise
                    log.warning("couldn't renew task credentials (old ones still valid): %s", e)
            assert self._creds is not None
            return dict(self._creds)

    def _renew(self) -> None:
        c = self.sts.assume_role(
            RoleArn=self.role_arn, RoleSessionName=self.session_name, DurationSeconds=SESSION_S
        )["Credentials"]
        exp = c["Expiration"]
        if isinstance(exp, str):
            exp = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        self._expires = exp.timestamp()
        self._creds = {
            "AccessKeyId": c["AccessKeyId"],
            "SecretAccessKey": c["SecretAccessKey"],
            "Token": c["SessionToken"],
            "Expiration": exp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    # --- serving -------------------------------------------------------------------------------

    def start(self) -> dict[str, str]:
        """Start serving; returns the environment task processes need."""
        self.current()  # fail at start, not in the first task, if the role can't be assumed
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_file.with_suffix(".tmp")
        tmp.write_text(self._token)
        os.chmod(tmp, 0o444)  # readable by task processes; only the agent (root) can change it
        os.replace(tmp, self.token_file)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        server.daemon_threads = True
        self._server = server
        threading.Thread(target=server.serve_forever, name="aegc-task-creds", daemon=True).start()
        port = server.server_address[1]
        log.info("serving task-role credentials on 127.0.0.1:%d", port)
        return {
            "AWS_CONTAINER_CREDENTIALS_FULL_URI": f"http://127.0.0.1:{port}{PATH}",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": str(self.token_file),
        }

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def authorized(self, header: str | None) -> bool:
        return header is not None and secrets.compare_digest(header.strip(), self._token)


def _handler(creds: TaskCredentials) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != PATH or not creds.authorized(self.headers.get("Authorization")):
                self._send(403, {"message": "forbidden"})
                return
            try:
                self._send(200, creds.current())
            except Exception as e:
                log.error("task credentials unavailable: %s", e)
                self._send(503, {"message": "task credentials unavailable"})

        def _send(self, code: int, body: dict[str, str]) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return  # one request per task credential refresh; not worth a log line

    return Handler
