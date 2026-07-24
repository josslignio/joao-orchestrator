"""joao_worker_host.hmac_auth — a defense-in-depth HMAC-SHA256 request
envelope between JOAO's controller and the standalone `joao-worker-host`
process (Boss directive, 2026-07-21).

Honest trust-boundary declaration (do not remove or soften this): both sides
of this envelope run as the SAME macOS user account. This mechanism defends
against an ACCIDENTAL or MISCONFIGURED local client (a stray script, a typo'd
socket path, a future bug that lets some other local tool talk to the
socket) — it is NOT a claim of protection against a malicious process
already running with full access to the same user account, which could read
this very secret file. There is no XPC/code-signing redesign here, and none
is claimed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from ..storage.atomic import append_line

DEFAULT_SECRET_FILENAME = "hmac-secret.key"
PROTOCOL_VERSION = "1"
CONTROLLER_ID = "joao-controller"
FRESHNESS_WINDOW_S = 60.0

# The full set of envelope fields a signed request must carry.
ENVELOPE_REQUIRED_FIELDS = ("protocol_version", "controller_id", "request_id", "issued_at",
                           "nonce", "body_sha256", "hmac_sha256")
# Fields excluded when hashing "the body" — request_id is intentionally NOT
# stripped: it is both a protocol.py-required business field AND part of the
# signed envelope, so leaving it in the hashed body only adds coverage.
_BODY_STRIP_FIELDS = ("protocol_version", "controller_id", "issued_at", "nonce",
                      "body_sha256", "hmac_sha256")

WORKER_HOST_TRUST_BOUNDARY = "same_macOS_user"
STRONG_SAME_UID_PROCESS_ISOLATION_CLAIMED = False


class EnvelopeError(ValueError):
    def __init__(self, reason_code: str, reason: str):
        super().__init__(reason)
        self.reason_code = reason_code
        self.reason = reason


def default_secret_path(state_dir: Path) -> Path:
    return Path(state_dir).expanduser() / DEFAULT_SECRET_FILENAME


def ensure_secret(state_dir: Path) -> Path:
    """Idempotent: generate a fresh 32-byte secret the first time, at the
    declared path — owner-only (0600), inside an owner-only (0700) state
    dir. Never inside the LaunchAgent plist, never logged. An existing
    secret is left untouched (never rotated implicitly)."""
    path = default_secret_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if not path.is_file():
        path.write_bytes(secrets.token_bytes(32))
    os.chmod(path, 0o600)
    return path


def load_secret(state_dir: Path) -> bytes | None:
    path = default_secret_path(state_dir)
    if not path.is_file():
        return None
    return path.read_bytes()


def _canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hmac_message(protocol_version: str, controller_id: str, request_id: str, issued_at: str,
                  nonce: str, body_sha256: str) -> bytes:
    return "|".join([protocol_version, controller_id, request_id, issued_at, nonce, body_sha256]).encode("utf-8")


def sign_request(body: dict[str, Any], *, secret: bytes) -> dict[str, Any]:
    """Wrap `body` (the actual builder/reviewer request — must already
    contain `request_id`) with a fresh HMAC envelope. Returns the full flat
    payload to send over the socket: every original `body` field plus the
    envelope fields, merged."""
    if not isinstance(body.get("request_id"), str) or not body["request_id"].strip():
        raise ValueError("body must already contain a non-empty request_id before signing")
    request_id = body["request_id"]
    stripped = {k: v for k, v in body.items() if k not in _BODY_STRIP_FIELDS}
    body_sha256 = hashlib.sha256(_canonical_json(stripped)).hexdigest()
    issued_at = repr(time.time())
    nonce = secrets.token_hex(16)
    mac = hmac.new(secret, _hmac_message(PROTOCOL_VERSION, CONTROLLER_ID, request_id, issued_at,
                                        nonce, body_sha256), hashlib.sha256).hexdigest()
    envelope = {"protocol_version": PROTOCOL_VERSION, "controller_id": CONTROLLER_ID,
               "issued_at": issued_at, "nonce": nonce, "body_sha256": body_sha256, "hmac_sha256": mac}
    return {**body, **envelope}


class ReplayLedger:
    """Persistent, single-use nonce ledger — independent defense-in-depth
    alongside `WorkerHost`'s own request_id ledger: a nonce belongs to the
    signature itself, so a replayed signed envelope is rejected even in a
    hypothetical scenario where the request_id ledger were reset/corrupted."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._seen.add(json.loads(line)["nonce"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue

    def mark(self, nonce: str) -> bool:
        """True if newly marked; False if `nonce` was already used."""
        with self._lock:
            if nonce in self._seen:
                return False
            self._seen.add(nonce)
            append_line(self.path, json.dumps({"nonce": nonce, "seen_at": time.time()}))
            return True


def verify_envelope(payload: dict[str, Any], *, secret: bytes, replay_ledger: ReplayLedger,
                    now: float | None = None, freshness_window_s: float = FRESHNESS_WINDOW_S) -> None:
    """Verify the HMAC envelope on `payload`. Raises `EnvelopeError` (a
    controlled BLOCK, before worker selection) on ANY violation — a missing
    or malformed envelope field, a tampered body, a bad signature, a stale
    `issued_at`, or a replayed nonce. Returns nothing on success (the caller
    continues to use `payload` unchanged — the envelope fields are inert
    extras once verified, never stripped or otherwise required downstream)."""
    for field in ENVELOPE_REQUIRED_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise EnvelopeError("WORKER_HOST_UNSIGNED_REQUEST",
                               f"request is missing or has a malformed envelope field {field!r} — "
                               "unsigned or malformed requests are blocked before worker selection")

    if payload["protocol_version"] != PROTOCOL_VERSION:
        raise EnvelopeError("WORKER_HOST_UNSUPPORTED_PROTOCOL_VERSION",
                           f"unsupported protocol_version {payload['protocol_version']!r}")
    if payload["controller_id"] != CONTROLLER_ID:
        raise EnvelopeError("WORKER_HOST_UNKNOWN_CONTROLLER",
                           f"unrecognized controller_id {payload['controller_id']!r}")

    body = {k: v for k, v in payload.items() if k not in _BODY_STRIP_FIELDS}
    expected_body_sha256 = hashlib.sha256(_canonical_json(body)).hexdigest()
    if not hmac.compare_digest(expected_body_sha256, payload["body_sha256"]):
        raise EnvelopeError("WORKER_HOST_BODY_HASH_MISMATCH",
                           "request body does not match its declared body_sha256 — tampered in transit")

    expected_mac = hmac.new(secret, _hmac_message(payload["protocol_version"], payload["controller_id"],
                                                  payload["request_id"], payload["issued_at"],
                                                  payload["nonce"], payload["body_sha256"]),
                            hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_mac, payload["hmac_sha256"]):
        raise EnvelopeError("WORKER_HOST_BAD_SIGNATURE", "HMAC signature does not match — request rejected")

    try:
        issued_at = float(payload["issued_at"])
    except ValueError:
        raise EnvelopeError("WORKER_HOST_UNSIGNED_REQUEST", "issued_at is not a valid timestamp")
    now = time.time() if now is None else now
    if abs(now - issued_at) > freshness_window_s:
        raise EnvelopeError("WORKER_HOST_STALE_REQUEST",
                           f"issued_at is outside the {freshness_window_s}s freshness window — "
                           "stale or replayed request rejected")

    if not replay_ledger.mark(payload["nonce"]):
        raise EnvelopeError("WORKER_HOST_REPLAYED_NONCE",
                           f"nonce {payload['nonce']!r} was already used once — single-use, replay rejected")
