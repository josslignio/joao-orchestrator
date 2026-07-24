"""Strict reviewer contract bound to controller-computed candidate identity."""
from __future__ import annotations

import json
import time
from typing import Any, Mapping

from ..integrity.records import CandidateIdentityV2, ReviewerRecordV2

VALID_VERDICTS = {"ACCEPT", "P1", "BLOCK"}
_DECISION = {"ACCEPT": "pass", "P1": "p1", "BLOCK": "block"}
_ALLOWED_TOP_KEYS = {"candidate_tree", "verdict", "findings", "reviewer"}
_ALLOWED_REVIEWER_KEYS = {"provider", "model"}


def parse_reviewer_response(raw_text: str) -> dict[str, Any] | None:
    if raw_text is None:
        return None
    text = raw_text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _load_expected_identity(
    value: CandidateIdentityV2 | Mapping[str, Any] | None,
    *, identity_signature: str | None, hmac_key: bytes | None,
) -> CandidateIdentityV2 | None:
    if value is None:
        return None
    identity = value if isinstance(value, CandidateIdentityV2) else CandidateIdentityV2.from_mapping(value)
    identity.validate()
    signature = identity_signature or identity.controller_signature
    if hmac_key is None or not signature or not identity.verify_signature(signature, hmac_key):
        raise ValueError("expected CandidateIdentityV2 signature is absent or invalid")
    return identity


def validate_reviewer_verdict(
    raw_text: str, *, expected_candidate_tree: str | None,
    provider: str, model: str, returncode: int = 0,
    expected_identity: CandidateIdentityV2 | Mapping[str, Any] | None = None,
    identity_signature: str | None = None,
    hmac_key: bytes | None = None,
) -> dict[str, Any]:
    """Validate strict reviewer JSON and sign a full ReviewerRecordV2.

    Plan-stage callers may omit ``expected_identity``. Every post-freeze caller
    must supply a signed CandidateIdentityV2 and HMAC key; the controller then
    binds the externally returned verdict to the full identity, process return
    code, provider and model. Reviewer-supplied identity/provider values are
    never trusted.
    """
    try:
        identity = _load_expected_identity(
            expected_identity, identity_signature=identity_signature,
            hmac_key=hmac_key,
        )
    except (TypeError, ValueError) as exc:
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": f"review identity invalid: {exc}"}

    if identity is not None:
        if expected_candidate_tree is None:
            expected_candidate_tree = identity.candidate_tree
        elif expected_candidate_tree != identity.candidate_tree:
            return {"ok": False, "decision": "block", "schema_valid": False,
                    "reason": "expected candidate tree disagrees with CandidateIdentityV2"}

    obj = parse_reviewer_response(raw_text)
    if obj is None:
        return {
            "ok": False, "decision": "block", "schema_valid": False,
            "reason": "reviewer response is not one strict JSON object",
            "raw_tail": (raw_text or "")[-2000:],
        }
    extra = sorted(set(obj) - _ALLOWED_TOP_KEYS)
    missing = sorted(_ALLOWED_TOP_KEYS - set(obj))
    if extra or missing:
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": f"correctif-5 reviewer JSON key mismatch missing={missing} extra={extra}"}
    verdict = obj.get("verdict")
    if verdict not in VALID_VERDICTS:
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": f"invalid verdict {verdict!r}"}
    candidate_tree = obj.get("candidate_tree")
    # Plan stages (before freeze) may have candidate_tree=None, post-freeze stages require string
    if candidate_tree is not None and not isinstance(candidate_tree, str):
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": "candidate_tree must be a string or None"}
    findings = obj.get("findings")
    if not isinstance(findings, list) or not all(isinstance(item, str) for item in findings):
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": "findings must be a list of strings"}
    reviewer = obj.get("reviewer")
    if not isinstance(reviewer, dict):
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": "reviewer metadata must be an object"}
    reviewer_extra = sorted(set(reviewer) - _ALLOWED_REVIEWER_KEYS)
    reviewer_missing = sorted(_ALLOWED_REVIEWER_KEYS - set(reviewer))
    if reviewer_extra or reviewer_missing:
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": f"correctif-5 reviewer metadata key mismatch missing={reviewer_missing} extra={reviewer_extra}"}
    if expected_candidate_tree is not None and candidate_tree != expected_candidate_tree:
        return {
            "ok": False, "decision": "block", "schema_valid": True,
            "reason": "candidate_tree absent or mismatched",
            "expected_candidate_tree": expected_candidate_tree,
            "received_candidate_tree": candidate_tree,
        }

    returncode_ok = isinstance(returncode, int) and returncode == 0
    proof: dict[str, Any] = {
        "candidate_tree": candidate_tree, "verdict": verdict,
        "findings": findings,
        "reviewer": {"provider": provider, "model": model},
    }
    if identity is not None:
        if hmac_key is None:
            return {"ok": False, "decision": "block", "schema_valid": False,
                    "reason": "post-freeze review missing HMAC key"}
        record = ReviewerRecordV2(
            identity_digest=identity.digest(),
            identity_signature=identity_signature or identity.controller_signature or "",
            base_commit=identity.base_commit,
            parent_commit=identity.parent_commit,
            candidate_commit=identity.candidate_commit,
            candidate_tree=identity.candidate_tree,
            canonical_diff_sha256=identity.canonical_diff_sha256,
            manifest_sha256=identity.manifest_sha256,
            reviewer_provider=provider,
            reviewer_model=model,
            reviewer_return_code=returncode,
            verdict=verdict,
            findings=list(findings),
            timestamp=time.time(),
        )
        try:
            record.validate()
        except ValueError as exc:
            return {"ok": False, "decision": "block", "schema_valid": False,
                    "reason": f"reviewer V2 record invalid: {exc}"}
        record.signature = record.compute_signature(hmac_key)
        proof.update({
            "identity_digest": identity.digest(),
            "identity_signature": identity_signature or identity.controller_signature,
            "reviewer_record_v2": dict(record.__dict__),
            "review_signature": record.signature,
        })

    accept = verdict == "ACCEPT" and returncode_ok
    return {
        "ok": accept,
        "decision": _DECISION[verdict] if returncode_ok else "block",
        "verdict": verdict, "schema_valid": True, "returncode": returncode,
        "proof": proof,
    }
