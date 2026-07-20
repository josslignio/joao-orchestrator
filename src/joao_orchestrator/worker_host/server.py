"""joao_worker_host.server — the standalone worker-host process.

Owns real instances of the four worker adapters (`GLMBuilder`, `GLMReviewer`,
`ClaudeCodeBuilder`, `ClaudeCLIReviewer`) and dispatches each validated
request to the correct one via the SAME `ExecutionBackend`/subprocess path
those adapters already use elsewhere in this codebase — argv-based, one
request, one response, no PTY, no keystroke injection, no screen-scraping
(Boss addendum, 2026-07-20).

This process is meant to run OUTSIDE any Claude Code / model session —
started by the user, a script, or (once installed) the
`com.joao.worker-host` LaunchAgent — and receive requests over a local,
owner-only Unix domain socket (0600, inside a 0700 directory: no other local
user or process may connect). No worker ever launches another worker: this
is the ONLY process in the whole system that invokes `claude`/`joao-glm`
subprocesses on JOAO's behalf.
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any

from ..bubble.runtime import ClaudeCLIReviewer, ClaudeCodeBuilder, GLMBuilder, GLMReviewer
from ..storage.atomic import append_line
from . import protocol

DEFAULT_STATE_DIR = Path("~/.local/state/joao/worker-host").expanduser()
DEFAULT_SOCKET_NAME = "worker-host.sock"


def default_socket_path(state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    return Path(state_dir) / DEFAULT_SOCKET_NAME


def _block(reason_code: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "decision": "block", "reason_code": reason_code, "reason": reason, **extra}


class WorkerHost:
    """Owns the worker adapters and the served-request ledger.

    The ledger is persisted to disk (never only in-memory) so a crash-restart
    of the LaunchAgent-managed process can never re-serve, or lose duplicate
    detection for, a `request_id` a prior process instance already consumed
    — "LaunchAgent restart preserves no active mission incorrectly"."""

    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR, *, builders: dict | None = None,
                reviewers: dict | None = None):
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.ledger_path = self.state_dir / "served-requests.jsonl"
        self._lock = threading.Lock()
        self._served: set[str] = set()
        if self.ledger_path.exists():
            for line in self.ledger_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._served.add(json.loads(line)["request_id"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
        # Registered workers — controller-owned identity comes from these
        # REAL adapter classes' own `provider`/`model`/`provider_family`
        # class attributes, never from a request field or a worker's own
        # output. A request only ever SELECTS one of these by name.
        # `builders`/`reviewers` overrides exist ONLY for hermetic testing
        # (inject a fake dispatch instead of a real subprocess) — production
        # code (`serve_forever`) always uses the real defaults below.
        self.builders = builders if builders is not None else {
            "zai-coding-plan": GLMBuilder(), "claude-cli": ClaudeCodeBuilder()}
        self.reviewers = reviewers if reviewers is not None else {
            "zai-coding-plan": GLMReviewer(), "claude-cli": ClaudeCLIReviewer()}

    def _mark_served(self, request_id: str) -> bool:
        """True if newly marked; False if `request_id` was already served —
        single-use, mirrors `secure_import`'s nonce discipline: marked BEFORE
        dispatch, so even a request that goes on to fail still burns its id."""
        with self._lock:
            if request_id in self._served:
                return False
            self._served.add(request_id)
            append_line(self.ledger_path, json.dumps({"request_id": request_id, "served_at": time.time()}))
            return True

    def handle(self, payload: Any) -> dict[str, Any]:
        # A dedicated liveness probe, never a builder/reviewer dispatch and
        # never subject to the request_id ledger (a health check is not a
        # mission and must be free to repeat).
        if isinstance(payload, dict) and payload.get("ping") is True:
            return {"ok": True, "pong": True, "served_at": time.time()}
        try:
            protocol.validate_request(payload)
        except protocol.ProtocolError as exc:
            return _block("WORKER_HOST_MALFORMED_REQUEST", str(exc))

        request_id = payload["request_id"]
        if not self._mark_served(request_id):
            return _block("WORKER_HOST_DUPLICATE_REQUEST",
                          f"request_id {request_id!r} was already served once — single-use, "
                          "duplicate/replay rejected", request_id=request_id)

        worker = payload["worker"]
        role = payload["role"]
        run_dir = Path(payload["run_dir"]) if payload.get("run_dir") else self.state_dir / "runs" / request_id
        run_dir.mkdir(parents=True, exist_ok=True)

        try:
            if role == "builder":
                result = self._dispatch_builder(worker, payload, run_dir)
            else:
                result = self._dispatch_reviewer(worker, payload, run_dir)
        except Exception as exc:  # a worker crash must fail-closed, never crash the host process
            result = _block("WORKER_HOST_DISPATCH_EXCEPTION", f"{type(exc).__name__}: {exc}")

        return {**result, "request_id": request_id, "run_id": payload["run_id"],
                "mission_id": payload["mission_id"], "worker": worker, "role": role,
                "served_at": time.time()}

    def _dispatch_builder(self, worker: str, payload: dict, run_dir: Path) -> dict[str, Any]:
        builder = self.builders.get(worker)
        if builder is None:
            return _block("WORKER_HOST_UNKNOWN_WORKER", f"no builder registered for worker {worker!r}")
        if hasattr(builder, "available") and not builder.available():
            return _block("WORKER_HOST_WORKER_UNAVAILABLE",
                          f"builder {worker!r} reports unavailable (capability probe failed) — "
                          "an unavailable worker is never dispatched")
        if hasattr(builder, "set_capabilities"):
            builder.set_capabilities({"network_capability": bool(payload.get("network_capability", False))})
        workspace = Path(payload["workspace"])
        allowed = list(payload.get("allowed_paths") or [])
        correction = bool(payload.get("correction", False))
        return builder.build(payload["mission"], workspace, run_dir, allowed, correction)

    def _dispatch_reviewer(self, worker: str, payload: dict, run_dir: Path) -> dict[str, Any]:
        reviewer = self.reviewers.get(worker)
        if reviewer is None:
            return _block("WORKER_HOST_UNKNOWN_WORKER", f"no reviewer registered for worker {worker!r}")
        if hasattr(reviewer, "available") and not reviewer.available():
            return _block("WORKER_HOST_WORKER_UNAVAILABLE",
                          f"reviewer {worker!r} reports unavailable (capability probe failed) — "
                          "an unavailable worker is never dispatched")
        stage = payload["stage"]
        candidate = None
        if payload.get("candidate_readonly_copy"):
            candidate = {"candidate_tree": payload.get("candidate_tree"),
                        "readonly_copy": payload.get("candidate_readonly_copy"),
                        "candidate_commit": payload.get("candidate_commit")}
        run = {"mission": payload["mission"], "workspace": payload["workspace"],
              "candidate_tree": payload.get("candidate_tree"), "candidate": candidate}
        return reviewer.review_stage(run, run_dir, stage, active_rules=payload.get("active_rules", ""))


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(getattr(self.server, "request_timeout", 1800))
        chunks = []
        try:
            while True:
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if chunk.endswith(b"\n"):
                    break
        except (socket.timeout, OSError):
            pass
        raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
        try:
            payload = json.loads(raw) if raw else None
        except (json.JSONDecodeError, ValueError):
            payload = None
        if payload is None:
            response = _block("WORKER_HOST_MALFORMED_REQUEST", "request body is empty or not valid JSON")
        else:
            response = self.server.host.handle(payload)  # type: ignore[attr-defined]
        try:
            self.request.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except OSError:
            pass


class WorkerHostServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: Path | None = None, state_dir: Path = DEFAULT_STATE_DIR,
                 request_timeout: int = 1800, *, builders: dict | None = None, reviewers: dict | None = None):
        state_dir = Path(state_dir).expanduser()
        socket_path = Path(socket_path).expanduser() if socket_path else default_socket_path(state_dir)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(socket_path.parent, 0o700)
        if socket_path.exists():
            socket_path.unlink()
        super().__init__(str(socket_path), _Handler)
        # Owner-only: no other local user or process on this Mac can connect
        # and trigger a builder/reviewer dispatch (Boss addendum, 2026-07-20).
        os.chmod(socket_path, 0o600)
        self.socket_path = socket_path
        self.request_timeout = request_timeout
        self.host = WorkerHost(state_dir, builders=builders, reviewers=reviewers)

    def health(self) -> dict[str, Any]:
        mode = self.socket_path.stat().st_mode & 0o777
        return {"ok": True, "socket": str(self.socket_path), "socket_mode": oct(mode), "pid": os.getpid()}

    def close(self) -> None:
        self.server_close()
        self.socket_path.unlink(missing_ok=True)


def serve_forever(socket_path: Path | None = None, state_dir: Path = DEFAULT_STATE_DIR) -> None:
    server = WorkerHostServer(socket_path=socket_path, state_dir=state_dir)
    try:
        server.serve_forever()
    finally:
        server.close()


if __name__ == "__main__":
    serve_forever()
