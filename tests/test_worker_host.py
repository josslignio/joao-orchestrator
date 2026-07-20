"""joao_worker_host — the standalone controller/worker-host IPC (Boss
architecture decision, 2026-07-20: JOAO is the sole controller; Claude/GLM
workers are launched by an external sibling process, never nested inside an
active model session; no PTY — plain argv-based subprocess dispatch, same
pattern GLMBuilder/CodexCLIReviewer already use).
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path

import pytest

from src.joao_orchestrator.worker_host import client as client_mod
from src.joao_orchestrator.worker_host import protocol
from src.joao_orchestrator.worker_host.proxies import RemoteBuilderProxy, RemoteReviewerProxy
from src.joao_orchestrator.worker_host.server import WorkerHost, WorkerHostServer

TREE = "a" * 40


# ---------------------------------------------------------------------------
# protocol.validate_request — fail-closed on malformed input
# ---------------------------------------------------------------------------
def _valid_builder_request(**overrides):
    request = {"request_id": "r1", "run_id": "run-1", "mission_id": "mission-1",
              "worker": "zai-coding-plan", "role": "builder", "model": "m",
              "workspace": "/tmp/ws", "timeout": 60, "mission": "fix it"}
    request.update(overrides)
    return request


def test_protocol_accepts_a_well_formed_builder_request():
    protocol.validate_request(_valid_builder_request())


def test_protocol_rejects_non_dict():
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(["not", "a", "dict"])


def test_protocol_rejects_missing_required_field():
    request = _valid_builder_request()
    del request["mission_id"]
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(request)


def test_protocol_rejects_unknown_worker():
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(_valid_builder_request(worker="some-other-cli"))


def test_protocol_rejects_bad_role():
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(_valid_builder_request(role="architect"))


def test_protocol_rejects_non_positive_timeout():
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(_valid_builder_request(timeout=0))
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(_valid_builder_request(timeout=True))  # bool is not an int here


def test_protocol_reviewer_non_plan_stage_requires_candidate_tree_and_readonly_copy():
    request = _valid_builder_request(role="reviewer", stage="final")
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(request)
    request["candidate_tree"] = TREE
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(request)  # still missing candidate_readonly_copy
    request["candidate_readonly_copy"] = "/tmp/candidate"
    protocol.validate_request(request)  # now valid


def test_protocol_reviewer_plan_stage_does_not_require_candidate():
    request = _valid_builder_request(role="reviewer", stage="plan")
    protocol.validate_request(request)


# ---------------------------------------------------------------------------
# WorkerHost.handle — dispatch, duplicate/replay rejection, fail-closed
# ---------------------------------------------------------------------------
class _FakeBuilder:
    provider = "fake-provider"; model = "fake-model"; provider_family = "fake-family"
    def __init__(self):
        self.calls = []
    def available(self):
        return True
    def build(self, mission, workspace, run_dir, allowed, correction):
        self.calls.append({"mission": mission, "workspace": str(workspace)})
        return {"ok": True, "provider": self.provider, "model": self.model}


class _UnavailableBuilder:
    provider = "unavailable"; model = "m"; provider_family = "f"
    def available(self):
        return False
    def build(self, *a, **k):
        raise AssertionError("must never be dispatched when unavailable")


class _FakeReviewer:
    provider = "fake-reviewer"; model = "fake-model"; provider_family = "fake-family"
    def available(self):
        return True
    def review_stage(self, run, run_dir, stage, active_rules=""):
        return {"ok": True, "decision": "pass", "stage": stage,
                "proof": {"candidate_tree": run.get("candidate_tree"), "verdict": "ACCEPT",
                         "findings": [], "reviewer": {"provider": self.provider, "model": self.model}}}


class _CrashingBuilder:
    provider = "crash"; model = "m"; provider_family = "f"
    def available(self):
        return True
    def build(self, *a, **k):
        raise RuntimeError("simulated worker crash")


def _host(tmp_path, **kwargs):
    return WorkerHost(tmp_path / "state",
                      builders=kwargs.pop("builders", {"zai-coding-plan": _FakeBuilder}),
                      reviewers=kwargs.pop("reviewers", {"zai-coding-plan": _FakeReviewer}))


def test_handle_dispatches_to_the_registered_builder(tmp_path):
    host = _host(tmp_path)
    result = host.handle(_valid_builder_request(request_id="req-1", workspace=str(tmp_path)))
    assert result["ok"] is True
    assert result["request_id"] == "req-1"


def test_handle_rejects_duplicate_request_id(tmp_path):
    host = _host(tmp_path)
    req = _valid_builder_request(request_id="dup-1", workspace=str(tmp_path))
    first = host.handle(req)
    assert first["ok"] is True
    replay = host.handle(req)
    assert replay["ok"] is False
    assert replay["reason_code"] == "WORKER_HOST_DUPLICATE_REQUEST"


def test_duplicate_detection_persists_across_host_restart(tmp_path):
    """A LaunchAgent crash-restart must never re-serve a request_id a prior
    process instance already consumed — the ledger is on-disk, not
    in-memory-only."""
    state_dir = tmp_path / "state"
    host1 = WorkerHost(state_dir, builders={"zai-coding-plan": _FakeBuilder},
                       reviewers={"zai-coding-plan": _FakeReviewer})
    req = _valid_builder_request(request_id="persisted-1", workspace=str(tmp_path))
    assert host1.handle(req)["ok"] is True

    # Simulate a process restart: a brand-new WorkerHost instance, same state_dir.
    host2 = WorkerHost(state_dir, builders={"zai-coding-plan": _FakeBuilder},
                       reviewers={"zai-coding-plan": _FakeReviewer})
    replay = host2.handle(req)
    assert replay["ok"] is False
    assert replay["reason_code"] == "WORKER_HOST_DUPLICATE_REQUEST"


def test_handle_never_dispatches_an_unavailable_worker(tmp_path):
    host = _host(tmp_path, builders={"zai-coding-plan": _UnavailableBuilder})
    result = host.handle(_valid_builder_request(request_id="unavail-1", workspace=str(tmp_path)))
    assert result["ok"] is False
    assert result["reason_code"] == "WORKER_HOST_WORKER_UNAVAILABLE"


def test_explicit_claude_builder_selection_blocks_before_any_subprocess(tmp_path):
    """Boss directive: a request that explicitly selects ClaudeCodeBuilder
    must return a controlled BLOCK before launching a subprocess — uses the
    REAL ClaudeCodeBuilder factory (disabled by standing policy), not a
    fake, to prove the actual production wiring."""
    from src.joao_orchestrator.bubble.runtime import CLAUDE_BUILDER_UNAVAILABLE_REASON, ClaudeCodeBuilder
    host = WorkerHost(tmp_path / "state", builders={"claude-cli": ClaudeCodeBuilder},
                      reviewers={"zai-coding-plan": _FakeReviewer})
    result = host.handle(_valid_builder_request(request_id="claude-builder-1", worker="claude-cli",
                                                workspace=str(tmp_path)))
    assert result["ok"] is False
    assert result["reason_code"] == "WORKER_HOST_WORKER_UNAVAILABLE"
    assert result["unavailable_reason"] == CLAUDE_BUILDER_UNAVAILABLE_REASON
    assert CLAUDE_BUILDER_UNAVAILABLE_REASON in result["reason"]


def test_handle_unknown_worker_blocks(tmp_path):
    host = _host(tmp_path)
    result = host.handle(_valid_builder_request(request_id="unknown-1", worker="claude-cli",
                                                workspace=str(tmp_path)))
    assert result["ok"] is False
    assert result["reason_code"] == "WORKER_HOST_UNKNOWN_WORKER"


def test_handle_malformed_payload_blocks_not_raises(tmp_path):
    host = _host(tmp_path)
    result = host.handle({"nonsense": True})
    assert result["ok"] is False
    assert result["reason_code"] == "WORKER_HOST_MALFORMED_REQUEST"


def test_handle_worker_crash_fails_closed_never_raises(tmp_path):
    host = _host(tmp_path, builders={"zai-coding-plan": _CrashingBuilder})
    result = host.handle(_valid_builder_request(request_id="crash-1", workspace=str(tmp_path)))
    assert result["ok"] is False
    assert result["reason_code"] == "WORKER_HOST_DISPATCH_EXCEPTION"


def test_handle_reviewer_dispatch_binds_candidate_tree(tmp_path):
    host = _host(tmp_path)
    request = _valid_builder_request(request_id="rev-1", role="reviewer", stage="final",
                                     candidate_tree=TREE, candidate_readonly_copy=str(tmp_path))
    result = host.handle(request)
    assert result["ok"] is True
    assert result["proof"]["candidate_tree"] == TREE


def test_worker_identity_never_supplied_by_the_request_or_model_output(tmp_path):
    """Negative proof: even if a request/payload names a `provider` field
    trying to impersonate a different identity, the DISPATCHED adapter's own
    class-level identity (never a request field) is what ends up in the
    proof — the protocol doesn't even accept a `provider` override field."""
    request = _valid_builder_request(request_id="identity-1", role="reviewer", stage="final",
                                     candidate_tree=TREE, candidate_readonly_copy=str(tmp_path))
    request["provider"] = "forged-provider"  # not part of the contract; must be ignored
    host = _host(tmp_path)
    result = host.handle(request)
    assert result["ok"] is True
    assert result["proof"]["reviewer"]["provider"] == "fake-reviewer"  # the REAL registered reviewer's identity


# ---------------------------------------------------------------------------
# Real Unix socket: permissions, health check, unavailable host, stale response
# ---------------------------------------------------------------------------
@pytest.fixture
def running_server(tmp_path):
    # AF_UNIX socket paths are capped at ~104 bytes on macOS — pytest's own
    # tmp_path is often too long, so the socket itself (only the socket, the
    # rest of the state can live under tmp_path) goes under a short-named
    # directory directly under /tmp instead.
    short_dir = Path(tempfile.mkdtemp(prefix="joao-wh-"))
    server = WorkerHostServer(socket_path=short_dir / "s.sock", state_dir=tmp_path / "state",
                              builders={"zai-coding-plan": _FakeBuilder},
                              reviewers={"zai-coding-plan": _FakeReviewer})
    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Poll for real readiness instead of a flat sleep — a flat sleep is
    # flaky under host load (e.g. a concurrent real GLM/Claude dispatch
    # elsewhere competing for CPU can delay thread scheduling well past a
    # fixed 50ms).
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        probe = client_mod.health_check(socket_path=server.socket_path, timeout=3)
        if probe.get("ok") and probe.get("pong"):
            break
        time.sleep(0.02)
    try:
        yield server
    finally:
        server.shutdown()
        server.close()
        import shutil
        shutil.rmtree(short_dir, ignore_errors=True)


def test_socket_and_directory_permissions_are_owner_only(running_server):
    sock_path = running_server.socket_path
    mode = sock_path.stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)
    dir_mode = sock_path.parent.stat().st_mode & 0o777
    assert dir_mode == 0o700, oct(dir_mode)


def test_health_check_over_real_socket(running_server):
    result = client_mod.health_check(socket_path=running_server.socket_path)
    assert result["ok"] is True
    assert result["pong"] is True


def test_real_socket_round_trip_dispatch(running_server, tmp_path):
    request = {"request_id": "socket-1", "run_id": "run-1", "mission_id": "mission-1",
              "worker": "zai-coding-plan", "role": "builder", "model": "m",
              "workspace": str(tmp_path), "timeout": 30, "mission": "do it"}
    result = client_mod.send_request(request, socket_path=running_server.socket_path)
    assert result["ok"] is True
    assert result["request_id"] == "socket-1"


def test_client_reports_controlled_block_when_worker_host_unavailable(tmp_path):
    result = client_mod.send_request({"request_id": "x"}, socket_path=tmp_path / "no-such-socket.sock")
    assert result["ok"] is False
    assert result["reason_code"] == "WORKER_HOST_UNAVAILABLE"


def test_client_rejects_a_response_bound_to_a_different_run(monkeypatch, tmp_path):
    """Stale/cross-wired response => BLOCK, never silently accepted."""
    import socket as socket_mod

    class _FakeSocket:
        def __init__(self, *a, **k):
            self.sent = None
        def settimeout(self, t):
            pass
        def connect(self, path):
            pass
        def sendall(self, data):
            self.sent = data
        def shutdown(self, how):
            pass
        def recv(self, n):
            if getattr(self, "_served", False):
                return b""
            self._served = True
            return json.dumps({"ok": True, "request_id": "x", "run_id": "WRONG-RUN",
                               "mission_id": "m"}).encode() + b"\n"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    monkeypatch.setattr(socket_mod, "socket", lambda *a, **k: _FakeSocket())
    (tmp_path / "fake.sock").touch()
    result = client_mod.send_request({"request_id": "x", "run_id": "run-1", "mission_id": "m"},
                                     socket_path=tmp_path / "fake.sock")
    assert result["ok"] is False
    assert result["reason_code"] == "WORKER_HOST_RESPONSE_MISMATCH"


# ---------------------------------------------------------------------------
# Proxies — same BuilderAdapter/ReviewerAdapter contract as GLMBuilder/etc.
# ---------------------------------------------------------------------------
def test_remote_builder_proxy_sends_correct_worker_and_identity(running_server, tmp_path):
    from src.joao_orchestrator.bubble.runtime import BuilderAdapter
    proxy = RemoteBuilderProxy(worker="zai-coding-plan", provider="zai-coding-plan", model="m",
                              provider_family="zai", socket_path=running_server.socket_path)
    assert isinstance(proxy, BuilderAdapter)
    run_dir = tmp_path / "runs" / "run-abc"
    run_dir.mkdir(parents=True)
    result = proxy.build("do it", tmp_path, run_dir, [], False)
    assert result["ok"] is True


def test_remote_reviewer_proxy_available_uses_health_check(running_server):
    proxy = RemoteReviewerProxy(worker="zai-coding-plan", provider="zai-coding-plan", model="m",
                               provider_family="zai", socket_path=running_server.socket_path)
    # A brief retry absorbs transient scheduling delays under heavy parallel
    # test-suite CPU load (many WorkerHostServer instances starting/stopping
    # across this file) — the underlying behavior is deterministic once the
    # server thread is actually scheduled; this is test-infra patience, not
    # product retry logic.
    deadline = time.monotonic() + 5.0
    result = False
    while time.monotonic() < deadline:
        result = proxy.available()
        if result:
            break
        time.sleep(0.05)
    assert result is True


def test_remote_reviewer_proxy_unavailable_when_no_host(tmp_path):
    proxy = RemoteReviewerProxy(worker="claude-cli", provider="claude-cli", model="m",
                               provider_family="anthropic", socket_path=tmp_path / "no-socket.sock")
    assert proxy.available() is False


# ---------------------------------------------------------------------------
# Concurrency isolation (Boss directive, 2026-07-21): `builders`/`reviewers`
# hold FACTORIES, never shared instances — this is the regression test that
# would FAIL under the pre-fix design (a single shared adapter instance
# reused across concurrent `ThreadingUnixStreamServer` requests).
# ---------------------------------------------------------------------------
class _SlowCapturingBuilder:
    """Records whatever capabilities THIS instance was given, after an
    artificial delay between `set_capabilities()` and `build()` — maximizing
    the race window a shared, mutable instance would be exposed to."""
    provider = "cap-builder"; model = "m"; provider_family = "f"

    def __init__(self):
        self._capabilities = None

    def available(self):
        return True

    def set_capabilities(self, capabilities):
        self._capabilities = capabilities

    def build(self, mission, workspace, run_dir, allowed, correction):
        import time as _time
        _time.sleep(0.05)
        return {"ok": True, "network_capability_seen": (self._capabilities or {}).get("network_capability")}


def test_concurrent_requests_never_cross_talk_capabilities(tmp_path):
    """WORKER_ADAPTERS_REQUEST_SCOPED / CAPABILITY_CROSS_TALK proof: two
    concurrent requests with OPPOSITE `network_capability` values must each
    see only their own — with a shared instance (pre-fix), the 50ms sleep
    between set_capabilities() and build() gives the other thread's
    set_capabilities() call ample time to clobber the shared
    `self._capabilities` before the first thread's build() reads it; with
    fresh-per-request instances (the fix) this is structurally impossible
    regardless of thread scheduling."""
    import threading

    host = WorkerHost(tmp_path / "state", builders={"zai-coding-plan": _SlowCapturingBuilder},
                      reviewers={"zai-coding-plan": _FakeReviewer})
    results: dict[str, dict] = {}

    def run(request_id, network_capability):
        req = _valid_builder_request(request_id=request_id, workspace=str(tmp_path))
        req["network_capability"] = network_capability
        results[request_id] = host.handle(req)

    t1 = threading.Thread(target=run, args=("concurrent-true", True))
    t2 = threading.Thread(target=run, args=("concurrent-false", False))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results["concurrent-true"]["network_capability_seen"] is True, results["concurrent-true"]
    assert results["concurrent-false"]["network_capability_seen"] is False, results["concurrent-false"]


def test_concurrent_requests_have_distinct_run_dirs_no_worktree_cross_talk(tmp_path):
    """WORKTREE_CROSS_TALK proof: two concurrent requests with different
    run_dir/workspace values must each be dispatched with exactly their own
    — never a shared/leaked path."""
    import threading

    class _PathRecordingBuilder:
        provider = "path-cap"; model = "m"; provider_family = "f"
        def available(self):
            return True
        def build(self, mission, workspace, run_dir, allowed, correction):
            import time as _time
            _time.sleep(0.05)
            return {"ok": True, "seen_workspace": str(workspace), "seen_run_dir": str(run_dir)}

    host = WorkerHost(tmp_path / "state", builders={"zai-coding-plan": _PathRecordingBuilder},
                      reviewers={"zai-coding-plan": _FakeReviewer})
    results: dict[str, dict] = {}

    def run(request_id, subdir):
        ws = tmp_path / f"workspace-{subdir}"
        ws.mkdir()
        rd = tmp_path / f"rundir-{subdir}"
        req = _valid_builder_request(request_id=request_id, workspace=str(ws), run_dir=str(rd))
        results[request_id] = host.handle(req)

    t1 = threading.Thread(target=run, args=("worktree-a", "a"))
    t2 = threading.Thread(target=run, args=("worktree-b", "b"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results["worktree-a"]["seen_workspace"].endswith("workspace-a")
    assert results["worktree-a"]["seen_run_dir"].endswith("rundir-a")
    assert results["worktree-b"]["seen_workspace"].endswith("workspace-b")
    assert results["worktree-b"]["seen_run_dir"].endswith("rundir-b")
