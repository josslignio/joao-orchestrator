"""joao_worker_host.hmac_auth + WorkerHost server-side restrictions (Boss
directive, 2026-07-21): a defense-in-depth HMAC-SHA256 request envelope
between JOAO's controller and the standalone `joao-worker-host` process, plus
independent, HMAC-orthogonal restrictions on `workspace`/`run_dir`.

Honest trust-boundary note (see `hmac_auth`'s own module docstring): this is
defense-in-depth against an accidental/misconfigured same-machine client, not
a claim of isolation from another process running as the same macOS user.
"""
from __future__ import annotations

import time

import pytest

from src.joao_orchestrator.worker_host import client as client_mod
from src.joao_orchestrator.worker_host import hmac_auth
from src.joao_orchestrator.worker_host.server import WorkerHost


def _body(**overrides):
    body = {"request_id": "req-1", "run_id": "run-1", "mission_id": "run-1",
           "worker": "zai-coding-plan", "role": "builder", "model": "m",
           "workspace": "/tmp/ws", "timeout": 60, "mission": "fix it"}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# sign_request / verify_envelope — pure round-trip and tamper detection
# ---------------------------------------------------------------------------
def test_sign_then_verify_round_trips(tmp_path):
    secret = hmac_auth.ensure_secret(tmp_path).read_bytes()
    signed = hmac_auth.sign_request(_body(), secret=secret)
    ledger = hmac_auth.ReplayLedger(tmp_path / "nonces.jsonl")
    hmac_auth.verify_envelope(signed, secret=secret, replay_ledger=ledger)  # no raise == pass


def test_secret_is_owner_only_and_never_regenerated(tmp_path):
    path1 = hmac_auth.ensure_secret(tmp_path)
    mode = path1.stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)
    first_bytes = path1.read_bytes()
    path2 = hmac_auth.ensure_secret(tmp_path)  # idempotent — never rotates
    assert path2.read_bytes() == first_bytes


def test_missing_envelope_fields_block(tmp_path):
    secret = hmac_auth.ensure_secret(tmp_path).read_bytes()
    ledger = hmac_auth.ReplayLedger(tmp_path / "nonces.jsonl")
    with pytest.raises(hmac_auth.EnvelopeError) as exc:
        hmac_auth.verify_envelope(_body(), secret=secret, replay_ledger=ledger)  # unsigned
    assert exc.value.reason_code == "WORKER_HOST_UNSIGNED_REQUEST"


def test_tampered_body_after_signing_is_detected(tmp_path):
    secret = hmac_auth.ensure_secret(tmp_path).read_bytes()
    ledger = hmac_auth.ReplayLedger(tmp_path / "nonces.jsonl")
    signed = hmac_auth.sign_request(_body(), secret=secret)
    signed["mission"] = "do something ENTIRELY different"  # tamper in transit
    with pytest.raises(hmac_auth.EnvelopeError) as exc:
        hmac_auth.verify_envelope(signed, secret=secret, replay_ledger=ledger)
    assert exc.value.reason_code == "WORKER_HOST_BODY_HASH_MISMATCH"


def test_wrong_secret_is_rejected(tmp_path):
    secret = hmac_auth.ensure_secret(tmp_path).read_bytes()
    wrong_secret = hmac_auth.ensure_secret(tmp_path / "elsewhere").read_bytes()
    ledger = hmac_auth.ReplayLedger(tmp_path / "nonces.jsonl")
    signed = hmac_auth.sign_request(_body(), secret=secret)
    with pytest.raises(hmac_auth.EnvelopeError) as exc:
        hmac_auth.verify_envelope(signed, secret=wrong_secret, replay_ledger=ledger)
    assert exc.value.reason_code == "WORKER_HOST_BAD_SIGNATURE"


def test_stale_issued_at_is_rejected(tmp_path):
    secret = hmac_auth.ensure_secret(tmp_path).read_bytes()
    ledger = hmac_auth.ReplayLedger(tmp_path / "nonces.jsonl")
    signed = hmac_auth.sign_request(_body(), secret=secret)
    # Verify "now" far outside the freshness window — simulates a stale/
    # replayed envelope resent long after issuance, without needing a real
    # clock sleep.
    with pytest.raises(hmac_auth.EnvelopeError) as exc:
        hmac_auth.verify_envelope(signed, secret=secret, replay_ledger=ledger,
                                  now=time.time() + 3600, freshness_window_s=60)
    assert exc.value.reason_code == "WORKER_HOST_STALE_REQUEST"


def test_replayed_nonce_is_rejected_even_with_a_fresh_timestamp_check(tmp_path):
    secret = hmac_auth.ensure_secret(tmp_path).read_bytes()
    ledger = hmac_auth.ReplayLedger(tmp_path / "nonces.jsonl")
    signed = hmac_auth.sign_request(_body(), secret=secret)
    hmac_auth.verify_envelope(signed, secret=secret, replay_ledger=ledger)  # first use: fine
    with pytest.raises(hmac_auth.EnvelopeError) as exc:
        hmac_auth.verify_envelope(signed, secret=secret, replay_ledger=ledger)  # replay
    assert exc.value.reason_code == "WORKER_HOST_REPLAYED_NONCE"


def test_replay_ledger_persists_across_new_instances(tmp_path):
    """Mirrors WorkerHost's own on-disk request_id ledger discipline: a
    fresh ReplayLedger instance pointed at the same file must not forget a
    nonce a prior instance already consumed."""
    path = tmp_path / "nonces.jsonl"
    ledger1 = hmac_auth.ReplayLedger(path)
    assert ledger1.mark("nonce-a") is True
    ledger2 = hmac_auth.ReplayLedger(path)  # simulates a process restart
    assert ledger2.mark("nonce-a") is False


def test_unsupported_protocol_version_is_rejected(tmp_path):
    secret = hmac_auth.ensure_secret(tmp_path).read_bytes()
    ledger = hmac_auth.ReplayLedger(tmp_path / "nonces.jsonl")
    signed = hmac_auth.sign_request(_body(), secret=secret)
    signed["protocol_version"] = "999"
    with pytest.raises(hmac_auth.EnvelopeError) as exc:
        hmac_auth.verify_envelope(signed, secret=secret, replay_ledger=ledger)
    assert exc.value.reason_code == "WORKER_HOST_UNSUPPORTED_PROTOCOL_VERSION"


def test_unknown_controller_id_is_rejected(tmp_path):
    secret = hmac_auth.ensure_secret(tmp_path).read_bytes()
    ledger = hmac_auth.ReplayLedger(tmp_path / "nonces.jsonl")
    signed = hmac_auth.sign_request(_body(), secret=secret)
    signed["controller_id"] = "some-other-controller"
    with pytest.raises(hmac_auth.EnvelopeError) as exc:
        hmac_auth.verify_envelope(signed, secret=secret, replay_ledger=ledger)
    assert exc.value.reason_code == "WORKER_HOST_UNKNOWN_CONTROLLER"


# ---------------------------------------------------------------------------
# Client-side: send_request refuses to dispatch without a controller secret
# ---------------------------------------------------------------------------
def test_client_refuses_to_dispatch_without_a_secret(tmp_path):
    (tmp_path / "sock").touch()  # socket path must exist to get past that check
    result = client_mod.send_request({"request_id": "x", "run_id": "y", "mission_id": "y"},
                                     socket_path=tmp_path / "sock", state_dir=tmp_path / "no-secret-here")
    assert result["ok"] is False
    assert result["reason_code"] == "WORKER_HOST_HMAC_SECRET_MISSING"


def test_ping_never_requires_a_secret(tmp_path):
    """A health check carries no build authority — the client must not
    refuse to even attempt one just because no secret has been provisioned
    yet (e.g. before the bootstrap script's first install)."""
    result = client_mod.send_request({"ping": True}, socket_path=tmp_path / "no-such-socket.sock")
    assert result["ok"] is False
    assert result["reason_code"] == "WORKER_HOST_UNAVAILABLE"  # never HMAC-related


# ---------------------------------------------------------------------------
# Server-side restrictions independent of the HMAC layer (Boss directive,
# 2026-07-21): workspace inside configured project roots; run_dir inside the
# JOAO state root. Opt-in (None by default) so every pre-existing unit test
# using arbitrary tmp_path fixtures is unaffected.
# ---------------------------------------------------------------------------
class _FakeBuilder:
    provider = "fake-provider"; model = "fake-model"; provider_family = "fake-family"
    def available(self):
        return True
    def build(self, mission, workspace, run_dir, allowed, correction):
        return {"ok": True}


def _signed_body(host, **overrides):
    return hmac_auth.sign_request(_body(**overrides), secret=host.secret)


def test_workspace_outside_allowed_roots_blocks(tmp_path):
    allowed_root = tmp_path / "allowed-project"
    allowed_root.mkdir()
    outside = tmp_path / "not-a-project-root"
    outside.mkdir()
    host = WorkerHost(tmp_path / "state", builders={"zai-coding-plan": _FakeBuilder},
                     allowed_workspace_roots=[allowed_root])
    result = host.handle(_signed_body(host, workspace=str(outside)))
    assert result["ok"] is False
    assert result["reason_code"] == "WORKER_HOST_WORKSPACE_OUTSIDE_ALLOWED_ROOTS"


# ---------------------------------------------------------------------------
# Real bug found while running PROOF C (Boss directive, 2026-07-21): every
# EARLY block response (HMAC failure, protocol-malformed body, an
# out-of-bounds workspace/run_dir) omitted request_id/run_id/mission_id —
# `client.send_request`'s own stale-response check then masked the REAL
# reason behind a confusing WORKER_HOST_RESPONSE_MISMATCH. Every early block
# must now echo back whichever of those three fields the request itself
# carried, so the real reason is never hidden.
# ---------------------------------------------------------------------------
def test_workspace_outside_allowed_roots_still_echoes_request_identifiers(tmp_path):
    allowed_root = tmp_path / "allowed-project"
    allowed_root.mkdir()
    outside = tmp_path / "not-a-project-root"
    outside.mkdir()
    host = WorkerHost(tmp_path / "state", builders={"zai-coding-plan": _FakeBuilder},
                     allowed_workspace_roots=[allowed_root])
    body = _body(workspace=str(outside))
    result = host.handle(hmac_auth.sign_request(body, secret=host.secret))
    assert result["reason_code"] == "WORKER_HOST_WORKSPACE_OUTSIDE_ALLOWED_ROOTS"
    assert result["request_id"] == body["request_id"]
    assert result["run_id"] == body["run_id"]
    assert result["mission_id"] == body["mission_id"]


def test_hmac_failure_block_still_echoes_request_identifiers(tmp_path):
    host = WorkerHost(tmp_path / "state", builders={"zai-coding-plan": _FakeBuilder})
    unsigned = _body()  # never signed at all -> WORKER_HOST_UNSIGNED_REQUEST
    result = host.handle(unsigned)
    assert result["reason_code"] == "WORKER_HOST_UNSIGNED_REQUEST"
    assert result["request_id"] == unsigned["request_id"]
    assert result["run_id"] == unsigned["run_id"]
    assert result["mission_id"] == unsigned["mission_id"]


def test_protocol_malformed_block_still_echoes_request_identifiers(tmp_path):
    host = WorkerHost(tmp_path / "state", builders={"zai-coding-plan": _FakeBuilder})
    body = {"request_id": "malformed-echo-1", "run_id": "run-echo-1", "mission_id": "run-echo-1",
           "worker": "not-a-real-worker-name", "role": "builder", "model": "m",
           "workspace": "/tmp/ws", "timeout": 60, "mission": "x"}
    result = host.handle(hmac_auth.sign_request(body, secret=host.secret))
    assert result["reason_code"] == "WORKER_HOST_MALFORMED_REQUEST"
    assert result["request_id"] == "malformed-echo-1"
    assert result["run_id"] == "run-echo-1"
    assert result["mission_id"] == "run-echo-1"


def test_client_no_longer_masks_an_early_block_as_a_response_mismatch(tmp_path):
    """End-to-end proof (through the real client, not just handle() directly):
    an out-of-bounds workspace surfaces its REAL reason_code to the caller,
    never WORKER_HOST_RESPONSE_MISMATCH."""
    import tempfile
    import threading
    from pathlib import Path as P

    from src.joao_orchestrator.worker_host.server import WorkerHostServer

    allowed_root = tmp_path / "allowed-project"
    allowed_root.mkdir()
    outside = tmp_path / "not-a-project-root"
    outside.mkdir()
    short_dir = P(tempfile.mkdtemp(prefix="joao-wh-echo-"))
    server = WorkerHostServer(socket_path=short_dir / "s.sock", state_dir=tmp_path / "state",
                              builders={"zai-coding-plan": _FakeBuilder},
                              allowed_workspace_roots=[allowed_root])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if client_mod.health_check(socket_path=server.socket_path, timeout=3).get("pong"):
            break
        time.sleep(0.02)
    try:
        result = client_mod.send_request(
            {"request_id": "echo-e2e-1", "run_id": "run-echo-e2e-1", "mission_id": "run-echo-e2e-1",
             "worker": "zai-coding-plan", "role": "builder", "model": "m", "workspace": str(outside),
             "timeout": 30, "mission": "x"},
            socket_path=server.socket_path, state_dir=tmp_path / "state")
        assert result["reason_code"] == "WORKER_HOST_WORKSPACE_OUTSIDE_ALLOWED_ROOTS"
    finally:
        server.shutdown()
        server.close()
        import shutil
        shutil.rmtree(short_dir, ignore_errors=True)


def test_workspace_inside_allowed_roots_passes(tmp_path):
    allowed_root = tmp_path / "allowed-project"
    (allowed_root / "sub").mkdir(parents=True)
    host = WorkerHost(tmp_path / "state", builders={"zai-coding-plan": _FakeBuilder},
                     allowed_workspace_roots=[allowed_root])
    result = host.handle(_signed_body(host, workspace=str(allowed_root / "sub")))
    assert result["ok"] is True


def test_run_dir_outside_allowed_roots_blocks(tmp_path):
    allowed_workspace = tmp_path / "allowed-project"
    allowed_workspace.mkdir()
    allowed_run_dir_root = tmp_path / "joao-state"
    allowed_run_dir_root.mkdir()
    outside_run_dir = tmp_path / "some-other-place" / "runs" / "run-1"
    host = WorkerHost(tmp_path / "state", builders={"zai-coding-plan": _FakeBuilder},
                     allowed_workspace_roots=[allowed_workspace],
                     allowed_run_dir_roots=[allowed_run_dir_root])
    result = host.handle(_signed_body(host, workspace=str(allowed_workspace), run_dir=str(outside_run_dir)))
    assert result["ok"] is False
    assert result["reason_code"] == "WORKER_HOST_RUN_DIR_OUTSIDE_ALLOWED_ROOTS"


def test_no_allowed_roots_configured_skips_the_check(tmp_path):
    """Default (None) — pre-existing unit tests with arbitrary tmp_path
    workspaces must be entirely unaffected."""
    host = WorkerHost(tmp_path / "state", builders={"zai-coding-plan": _FakeBuilder})
    result = host.handle(_signed_body(host, workspace=str(tmp_path / "anywhere-at-all")))
    assert result["ok"] is True


def test_arbitrary_home_directory_workspace_outside_configured_roots_is_rejected(tmp_path):
    """The allowlist is a SPECIFIC set of project roots, never a blanket
    '$HOME is fine' allowance — a path elsewhere under the (fake, tmp_path-
    simulated) home directory that isn't one of the configured project
    roots is rejected exactly like any other out-of-bounds path."""
    fake_home = tmp_path / "fake-home"
    allowed_root = fake_home / "the-one-real-project"
    allowed_root.mkdir(parents=True)
    (fake_home / "some-other-random-home-dir-stuff").mkdir()
    host = WorkerHost(tmp_path / "state", builders={"zai-coding-plan": _FakeBuilder},
                     allowed_workspace_roots=[allowed_root])
    result = host.handle(_signed_body(host, workspace=str(fake_home / "some-other-random-home-dir-stuff")))
    assert result["ok"] is False
    assert result["reason_code"] == "WORKER_HOST_WORKSPACE_OUTSIDE_ALLOWED_ROOTS"


# ---------------------------------------------------------------------------
# Trust-boundary declaration is honest and surfaced
# ---------------------------------------------------------------------------
def test_trust_boundary_is_declared_same_macos_user_not_strong_isolation():
    assert hmac_auth.WORKER_HOST_TRUST_BOUNDARY == "same_macOS_user"
    assert hmac_auth.STRONG_SAME_UID_PROCESS_ISOLATION_CLAIMED is False


def test_ping_response_surfaces_trust_boundary(tmp_path):
    host = WorkerHost(tmp_path / "state", builders={"zai-coding-plan": _FakeBuilder})
    result = host.handle({"ping": True})
    assert result["trust_boundary"] == "same_macOS_user"
    assert result["strong_same_uid_process_isolation_claimed"] is False
