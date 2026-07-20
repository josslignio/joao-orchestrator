"""Secure evidence import (C8-B, pre-C8-B correction #2, `JOAO_WORKER_
INTEGRATION_SPEC.md` §5).

A reviewer verdict imported from outside JOAO's own dispatch (a formal GPT
counter-audit run manually and dropped in as a file) is exactly the kind of
proof a compromised or malicious builder could try to forge — nothing stops
a builder from writing a hand-crafted "ACCEPT" JSON to disk unless the import
path itself is authenticated. This module is that authentication layer: the
controller mints a single-use, candidate_tree-bound challenge into an inbox
directory the builder is NEVER granted write access to (the inbox lives
under the `RunRuntime` state root, a sibling of `runs/<run_id>/` — never
inside any given run's own `run_dir`, which the builder's own dispatch IS
granted write access to via `extra_write_paths`), and only accepts an import
that presents that exact, unconsumed, unexpired nonce, bound to the exact
run/candidate it was minted for.

Reuses `reviewer_contract.validate_reviewer_verdict` UNCHANGED for the
embedded verdict payload itself — this module adds exactly one
authentication layer on top of the existing RI-4/A0-2 contract, never a
second, divergent JSON parser for the verdict shape.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from ..storage.atomic import atomic_write_json
from .reviewer_contract import validate_reviewer_verdict


class SecureImportError(RuntimeError):
    pass


def _block(reason_code: str, reason: str) -> dict[str, Any]:
    return {"ok": False, "decision": "block", "reason_code": reason_code,
            "reason": reason, "nonce_verified": False}


def issue_challenge(inbox_dir: Path, *, run_id: str, mission_id: str, candidate_tree: str,
                    expected_reviewer_provider: str, issued_at: str, expires_at: str) -> dict[str, Any]:
    """Mint a single-use, candidate_tree-bound challenge into `inbox_dir` —
    the controller-owned inbox. Returns the challenge record (the same
    record persisted to disk as `challenge-<nonce>.json`)."""
    inbox_dir = Path(inbox_dir)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    nonce = secrets.token_hex(32)
    challenge = {
        "schema_version": 1,
        "challenge_id": secrets.token_hex(12),
        "nonce": nonce,
        "run_id": run_id,
        "mission_id": mission_id,
        "candidate_tree": candidate_tree,
        "expected_reviewer_provider": expected_reviewer_provider,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "consumed": False,
        "consumed_at": None,
    }
    atomic_write_json(inbox_dir / f"challenge-{nonce}.json", challenge)
    return challenge


def _challenge_path(inbox_dir: Path, nonce: str) -> Path:
    # A nonce WE mint is always a `secrets.token_hex` value (hex digits
    # only), but an imported envelope's nonce is untrusted input — never
    # build a filesystem path from it without validating the character set
    # first, or a crafted nonce (e.g. "../../../etc/passwd") could escape
    # `inbox_dir` entirely.
    if not nonce or not isinstance(nonce, str) or not all(ch in "0123456789abcdef" for ch in nonce):
        raise SecureImportError("nonce is missing or not a valid hex token")
    return Path(inbox_dir) / f"challenge-{nonce}.json"


def consume_import(inbox_dir: Path, envelope_text: str, *, run_id: str, mission_id: str,
                   candidate_tree: str, expected_reviewer_provider: str, expected_model: str,
                   now: str) -> dict[str, Any]:
    """Validate + single-use-consume an imported review envelope.

    `envelope_text` is the raw text of the imported file — a JSON object
    `{"nonce": str, "verdict_payload": str}`. `verdict_payload` (itself a
    JSON STRING, never a nested object) is handed to
    `validate_reviewer_verdict` UNCHANGED once every authentication check
    below passes.

    Returns a BLOCK-shaped dict (`{"ok": False, "decision": "block", ...}`)
    on every authentication failure; never raises for a malformed/malicious
    envelope (`SecureImportError` is reserved for a genuine internal misuse
    this function itself cannot safely proceed past, e.g. an unsafe nonce).
    """
    try:
        envelope = json.loads((envelope_text or "").strip())
    except (json.JSONDecodeError, ValueError):
        return _block("SECURE_IMPORT_MALFORMED_ENVELOPE", "import envelope is not valid JSON")
    if not isinstance(envelope, dict):
        return _block("SECURE_IMPORT_MALFORMED_ENVELOPE", "import envelope is not a JSON object")

    nonce = envelope.get("nonce")
    verdict_payload = envelope.get("verdict_payload")
    if not isinstance(nonce, str) or not nonce:
        return _block("SECURE_IMPORT_MISSING_NONCE", "import envelope has no nonce")
    if not isinstance(verdict_payload, str) or not verdict_payload:
        return _block("SECURE_IMPORT_MALFORMED_ENVELOPE", "import envelope has no verdict_payload string")

    try:
        challenge_path = _challenge_path(inbox_dir, nonce)
    except SecureImportError as exc:
        return _block("SECURE_IMPORT_INVALID_NONCE", str(exc))
    if not challenge_path.is_file():
        return _block("SECURE_IMPORT_UNKNOWN_NONCE",
                      "no challenge was ever issued for this nonce — cannot be trusted")

    try:
        challenge = json.loads(challenge_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return _block("SECURE_IMPORT_CHALLENGE_UNREADABLE", f"{type(exc).__name__}: {exc}")

    if challenge.get("consumed"):
        return _block("SECURE_IMPORT_REPLAYED",
                      "this nonce's challenge was already consumed once — single-use, replay rejected")
    if challenge.get("run_id") != run_id:
        return _block("SECURE_IMPORT_WRONG_RUN",
                      f"challenge was issued for run {challenge.get('run_id')!r}, not {run_id!r}")
    if challenge.get("mission_id") != mission_id:
        return _block("SECURE_IMPORT_WRONG_MISSION",
                      f"challenge was issued for mission {challenge.get('mission_id')!r}, not {mission_id!r}")
    if challenge.get("candidate_tree") != candidate_tree:
        return _block("SECURE_IMPORT_WRONG_TREE",
                      f"challenge is bound to candidate_tree {challenge.get('candidate_tree')!r}, not {candidate_tree!r}")
    if challenge.get("expected_reviewer_provider") != expected_reviewer_provider:
        return _block("SECURE_IMPORT_WRONG_REVIEWER",
                      f"challenge expects reviewer {challenge.get('expected_reviewer_provider')!r}, "
                      f"not {expected_reviewer_provider!r}")
    if now >= str(challenge.get("expires_at") or ""):
        return _block("SECURE_IMPORT_EXPIRED", f"challenge expired at {challenge.get('expires_at')!r} (now {now!r})")

    # Mark single-use BEFORE validating the verdict payload itself — even a
    # malformed/rejected payload must burn the nonce, or a builder could
    # brute-force multiple crafted payloads against the same live challenge.
    challenge["consumed"] = True
    challenge["consumed_at"] = now
    atomic_write_json(challenge_path, challenge)

    result = validate_reviewer_verdict(verdict_payload, expected_candidate_tree=candidate_tree,
                                       provider=expected_reviewer_provider, model=expected_model)
    return {**result, "nonce_verified": True, "challenge_id": challenge.get("challenge_id")}
