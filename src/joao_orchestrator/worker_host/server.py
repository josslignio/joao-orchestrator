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
from . import hmac_auth, protocol

DEFAULT_STATE_DIR = Path("~/.local/state/joao/worker-host").expanduser()
DEFAULT_SOCKET_NAME = "worker-host.sock"


def default_socket_path(state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    return Path(state_dir) / DEFAULT_SOCKET_NAME


def _block(reason_code: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "decision": "block", "reason_code": reason_code, "reason": reason, **extra}


class WorkerHost:
    """Owns the worker adapter FACTORIES and the served-request ledger.

    Concurrency fix (Boss directive, 2026-07-21): `ThreadingUnixStreamServer`
    serves concurrent requests on daemon threads, and `set_capabilities()`/
    `build()`/`review_stage()` are not atomic — a shared, mutable adapter
    INSTANCE reused across requests would let one in-flight request's
    capabilities (or a builder's own `self._capabilities` state) leak into a
    concurrent, unrelated request. `builders`/`reviewers` therefore hold
    zero-arg FACTORY callables (a bare adapter class works directly — e.g.
    `GLMBuilder` itself — since it takes no required args), never instances;
    every dispatch below constructs a genuinely fresh instance, used by
    exactly one request, then discarded. No shared mutable adapter object is
    ever reused across requests.

    The served-request ledger is persisted to disk (never only in-memory) so
    a crash-restart of the LaunchAgent-managed process can never re-serve, or
    lose duplicate detection for, a `request_id` a prior process instance
    already consumed — "LaunchAgent restart preserves no active mission
    incorrectly"."""

    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR, *, builders: dict | None = None,
                reviewers: dict | None = None, allowed_workspace_roots: list | None = None,
                allowed_run_dir_roots: list | None = None):
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.ledger_path = self.state_dir / "served-requests.jsonl"
        self._lock = threading.Lock()
        self._served: set[str] = set()
        # HMAC envelope (Boss directive, 2026-07-21): defense-in-depth against
        # an accidental/misconfigured same-machine client — see
        # `hmac_auth`'s module docstring for the honest trust-boundary
        # declaration (WORKER_HOST_TRUST_BOUNDARY=same_macOS_user). The
        # secret is generated once, idempotently, outside any LaunchAgent
        # plist, owner-only, never logged.
        hmac_auth.ensure_secret(self.state_dir)
        self.secret = hmac_auth.load_secret(self.state_dir)
        self.replay_ledger = hmac_auth.ReplayLedger(self.state_dir / "hmac-nonces.jsonl")
        # Server-side restrictions independent of the HMAC layer (Boss
        # directive, 2026-07-21): when set (production wiring always sets
        # these — see `serve_forever`), `workspace` must resolve inside one
        # of `allowed_workspace_roots` and `run_dir` inside one of
        # `allowed_run_dir_roots`. None (the default, used by unit tests that
        # exercise unrelated behavior with arbitrary tmp_path fixtures) skips
        # the check entirely — never a silent narrowing of existing tests.
        self.allowed_workspace_roots = ([Path(p).expanduser().resolve() for p in allowed_workspace_roots]
                                       if allowed_workspace_roots else None)
        self.allowed_run_dir_roots = ([Path(p).expanduser().resolve() for p in allowed_run_dir_roots]
                                     if allowed_run_dir_roots else None)
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
        # Each value is a zero-arg FACTORY (a class, or a closure capturing
        # fixed constructor args — e.g. a test's fake executable path) —
        # NEVER a shared instance. `builders`/`reviewers` overrides exist for
        # hermetic testing (inject a fake dispatch factory instead of a real
        # subprocess) — production code (`serve_forever`) always uses the
        # real defaults below.
        self.builder_factories = builders if builders is not None else {
            "zai-coding-plan": GLMBuilder, "claude-cli": ClaudeCodeBuilder}
        self.reviewer_factories = reviewers if reviewers is not None else {
            "zai-coding-plan": GLMReviewer, "claude-cli": ClaudeCLIReviewer}

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
        # never subject to the request_id ledger or the HMAC envelope (a
        # health check is not a mission, carries no build authority, and
        # must be free to repeat).
        if isinstance(payload, dict) and payload.get("ping") is True:
            return {"ok": True, "pong": True, "served_at": time.time(),
                   "trust_boundary": hmac_auth.WORKER_HOST_TRUST_BOUNDARY,
                   "strong_same_uid_process_isolation_claimed": hmac_auth.STRONG_SAME_UID_PROCESS_ISOLATION_CLAIMED}
        if not isinstance(payload, dict):
            return _block("WORKER_HOST_MALFORMED_REQUEST",
                          f"request must be a JSON object, got {type(payload).__name__}")

        # Best-effort echo of request_id/run_id/mission_id, even on an EARLY
        # block below — the client independently refuses (BLOCK) any
        # response whose these fields don't match what it sent
        # (`client.send_request`'s stale/cross-wired-response check). Without
        # echoing them here too, every early block (bad signature, malformed
        # body, an out-of-bounds workspace) would surface to the caller as a
        # confusing WORKER_HOST_RESPONSE_MISMATCH instead of the real reason.
        echo = {k: payload.get(k) for k in ("request_id", "run_id", "mission_id")
               if isinstance(payload.get(k), str)}

        # HMAC envelope verification happens BEFORE worker selection (Boss
        # directive, 2026-07-21): a malformed/unsigned/tampered/stale/
        # replayed request is blocked here, never reaching protocol
        # validation or dispatch.
        try:
            hmac_auth.verify_envelope(payload, secret=self.secret, replay_ledger=self.replay_ledger)
        except hmac_auth.EnvelopeError as exc:
            return {**_block(exc.reason_code, exc.reason), **echo}

        try:
            protocol.validate_request(payload)
        except protocol.ProtocolError as exc:
            return {**_block("WORKER_HOST_MALFORMED_REQUEST", str(exc)), **echo}

        request_id = payload["request_id"]
        if not self._mark_served(request_id):
            return _block("WORKER_HOST_DUPLICATE_REQUEST",
                          f"request_id {request_id!r} was already served once — single-use, "
                          "duplicate/replay rejected", request_id=request_id)

        worker = payload["worker"]
        role = payload["role"]
        run_dir = Path(payload["run_dir"]) if payload.get("run_dir") else self.state_dir / "runs" / request_id

        # Server-side restrictions independent of the HMAC layer (Boss
        # directive, 2026-07-21) — only enforced when the roots are
        # configured (production wiring always configures them; see
        # `serve_forever`).
        if self.allowed_workspace_roots is not None:
            workspace_resolved = Path(payload["workspace"]).expanduser().resolve()
            if not any(workspace_resolved == root or root in workspace_resolved.parents
                      for root in self.allowed_workspace_roots):
                return {**_block("WORKER_HOST_WORKSPACE_OUTSIDE_ALLOWED_ROOTS",
                                 f"workspace {payload['workspace']!r} is not inside any configured "
                                 "project root — an arbitrary home-directory workspace is rejected"), **echo}
        if self.allowed_run_dir_roots is not None:
            run_dir_resolved = run_dir.expanduser().resolve()
            if not any(run_dir_resolved == root or root in run_dir_resolved.parents
                      for root in self.allowed_run_dir_roots):
                return {**_block("WORKER_HOST_RUN_DIR_OUTSIDE_ALLOWED_ROOTS",
                                 f"run_dir {str(run_dir)!r} is not inside the configured JOAO state root"), **echo}
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
        factory = self.builder_factories.get(worker)
        if factory is None:
            return _block("WORKER_HOST_UNKNOWN_WORKER", f"no builder registered for worker {worker!r}")
        builder = factory()  # fresh, request-scoped instance — never shared across requests
        if hasattr(builder, "available") and not builder.available():
            # Controlled BLOCK before any subprocess is launched — a builder
            # explicitly named in the request (e.g. `claude-cli`, disabled by
            # standing policy) never reaches `builder.build()`. Surfaces the
            # adapter's own `unavailable_reason` when it declares one (e.g.
            # `ClaudeCodeBuilder.CLAUDE_BUILDER_UNAVAILABLE_REASON`) rather
            # than a bare "capability probe failed".
            reason = getattr(builder, "unavailable_reason", None)
            return _block("WORKER_HOST_WORKER_UNAVAILABLE",
                          f"builder {worker!r} reports unavailable"
                          + (f": {reason}" if reason else " (capability probe failed)")
                          + " — an unavailable worker is never dispatched",
                          unavailable_reason=reason)
        if hasattr(builder, "set_capabilities"):
            builder.set_capabilities({"network_capability": bool(payload.get("network_capability", False))})
        workspace = Path(payload["workspace"])
        allowed = list(payload.get("allowed_paths") or [])
        correction = bool(payload.get("correction", False))
        return builder.build(payload["mission"], workspace, run_dir, allowed, correction)

    def _dispatch_reviewer(self, worker: str, payload: dict, run_dir: Path) -> dict[str, Any]:
        factory = self.reviewer_factories.get(worker)
        if factory is None:
            return _block("WORKER_HOST_UNKNOWN_WORKER", f"no reviewer registered for worker {worker!r}")
        reviewer = factory()  # fresh, request-scoped instance — never shared across requests
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
                 request_timeout: int = 1800, *, builders: dict | None = None, reviewers: dict | None = None,
                 allowed_workspace_roots: list | None = None, allowed_run_dir_roots: list | None = None):
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
        self.host = WorkerHost(state_dir, builders=builders, reviewers=reviewers,
                               allowed_workspace_roots=allowed_workspace_roots,
                               allowed_run_dir_roots=allowed_run_dir_roots)

    def health(self) -> dict[str, Any]:
        mode = self.socket_path.stat().st_mode & 0o777
        # Boss directive (2026-07-21): the trust boundary is stated honestly
        # in every health response — this HMAC-authenticated Unix socket is
        # defense-in-depth against an accidental/misconfigured same-machine
        # client, never a claim of isolation from another process running as
        # the same user (see `hmac_auth`'s module docstring).
        return {"ok": True, "socket": str(self.socket_path), "socket_mode": oct(mode), "pid": os.getpid(),
                "trust_boundary": hmac_auth.WORKER_HOST_TRUST_BOUNDARY,
                "strong_same_uid_process_isolation_claimed": hmac_auth.STRONG_SAME_UID_PROCESS_ISOLATION_CLAIMED}

    def close(self) -> None:
        self.server_close()
        self.socket_path.unlink(missing_ok=True)


def _production_allowed_roots(state_dir: Path) -> tuple[list, list]:
    """Real production allowlists (Boss directive, 2026-07-21): `workspace`
    must be inside a configured project root (the controller's own
    ProjectRegistry — this repo's `project_profiles/`/`projects/` dirs, plus
    each registered project's own declared `repository_path`); `run_dir`
    inside the JOAO controller's own state root. Never a blind "anything
    under $HOME" allowance."""
    from ..bubble.project_registry import ProjectRegistry
    registry = ProjectRegistry()
    profiles_root = registry.resolve_profiles_root()
    projects_root = registry.resolve_projects_root()
    workspace_roots = [r.path for r in (profiles_root, projects_root) if r.exists]
    if profiles_root.exists:
        for entry in sorted(Path(profiles_root.path).iterdir()):
            if entry.is_dir() and (entry / "profile.json").is_file():
                repo_path = registry.project_repository_path(entry.name)
                if repo_path is not None:
                    workspace_roots.append(repo_path)
    # The JOAO controller's own state root (default `~/.local/share/joao`,
    # matching `cli/joao.py`'s `ui` subcommand default) — where real run_dirs
    # (`<state_root>/runs/<run_id>`) actually live; distinct from the
    # worker-host's OWN operational `state_dir`.
    run_dir_roots = [Path("~/.local/share/joao").expanduser()]
    return workspace_roots, run_dir_roots


def serve_forever(socket_path: Path | None = None, state_dir: Path = DEFAULT_STATE_DIR) -> None:
    workspace_roots, run_dir_roots = _production_allowed_roots(state_dir)
    server = WorkerHostServer(socket_path=socket_path, state_dir=state_dir,
                              allowed_workspace_roots=workspace_roots, allowed_run_dir_roots=run_dir_roots)
    try:
        server.serve_forever()
    finally:
        server.close()


if __name__ == "__main__":
    serve_forever()
