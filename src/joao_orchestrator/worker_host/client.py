"""joao_worker_host.client — talk to a running `joao-worker-host` over its
Unix domain socket. Never falls back to an in-process dispatch: an
unreachable/absent worker host is a controlled BLOCK, exactly like an
unavailable `container`/`vm` `ExecutionBackend` (`bubble/execution_backend.py`
`PREFLIGHT_UNAVAILABLE`) — never a silent alternate path.
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any

from .server import default_socket_path

# A connection actively refused against a socket PATH THAT EXISTS is retried
# briefly — the listener may not have reached its first `accept()` yet under
# heavy host load (observed under a large concurrent test suite; the socket
# file existing does not guarantee the accept loop is already scheduled).
# Bounded and short: never masks a genuinely absent/dead worker-host (that
# path never reaches here — `path.exists()` fails fast, no retry).
_CONNECT_REFUSED_RETRIES = 5
_CONNECT_REFUSED_RETRY_DELAY_S = 0.05


class WorkerHostUnavailable(RuntimeError):
    pass


def _block(reason_code: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "decision": "block", "reason_code": reason_code, "reason": reason, **extra}


def send_request(payload: dict[str, Any], *, socket_path: Path | None = None, timeout: int = 1800) -> dict[str, Any]:
    """Send one JSON request, read one JSON response line, close the
    connection. Returns a controlled BLOCK dict (never raises) when the
    worker host is unreachable, closes early, or replies with something that
    is not valid JSON — a caller can trust every returned dict has at least
    `{"ok": bool, "decision": str, "reason_code": str}`."""
    path = Path(socket_path).expanduser() if socket_path else default_socket_path()
    if not path.exists():
        return _block("WORKER_HOST_UNAVAILABLE",
                      f"no worker-host socket at {path} — is `joao-worker-host` running?")
    chunks = None
    for attempt in range(_CONNECT_REFUSED_RETRIES + 1):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(str(path))
                sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                sock.shutdown(socket.SHUT_WR)
                chunks = []
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            break
        except ConnectionRefusedError as exc:
            if attempt < _CONNECT_REFUSED_RETRIES:
                time.sleep(_CONNECT_REFUSED_RETRY_DELAY_S)
                continue
            return _block("WORKER_HOST_UNREACHABLE", f"{type(exc).__name__}: {exc}")
        except (OSError, socket.timeout) as exc:
            return _block("WORKER_HOST_UNREACHABLE", f"{type(exc).__name__}: {exc}")

    raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    try:
        response = json.loads(raw) if raw else None
    except (json.JSONDecodeError, ValueError):
        response = None
    if not isinstance(response, dict):
        return _block("WORKER_HOST_MALFORMED_RESPONSE", "worker host did not return a JSON object")

    # Never trust a response bound to a DIFFERENT request/run/mission than
    # what we just sent — a stale, cross-wired or replayed response is a
    # controlled BLOCK, not a silently-accepted result for the wrong mission.
    for field in ("request_id", "run_id", "mission_id"):
        if field in payload and response.get(field) != payload[field]:
            return _block("WORKER_HOST_RESPONSE_MISMATCH",
                          f"response.{field} ({response.get(field)!r}) does not match the request "
                          f"({payload.get(field)!r}) — refusing a stale/cross-wired result",
                          expected=payload.get(field), received=response.get(field))
    return response


def health_check(*, socket_path: Path | None = None, timeout: int = 5) -> dict[str, Any]:
    """A cheap liveness probe distinct from a real worker dispatch — never
    invokes a builder/reviewer, never consumes a request_id from the
    duplicate-detection ledger; just proves the socket accepts a connection
    and answers with a well-formed response."""
    return send_request({"ping": True}, socket_path=socket_path, timeout=timeout)
