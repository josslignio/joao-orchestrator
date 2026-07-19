"""RI-4: the reviewer's verdict is a strict JSON contract bound to the exact
build candidate. A hash that is absent, mismatched, or a free-text response
instead of parseable JSON is rejected by the controller outright — never
"trusted anyway" (that fail-open path is exactly ATTACK_TEST 4's scenario:
a reviewer answering for some other SHA).

A0-2 (correction pass, 2026-07-19): the original `parse_reviewer_response`
had a brace-scanning fallback that extracted the last top-level `{...}`
object out of surrounding prose when a strict `json.loads` failed. That
fallback was itself a fail-open hole — a reviewer response with prose before
or after a valid-looking ACCEPT object was silently accepted instead of
rejected. The parser is now `json.loads(raw.strip())` and nothing else: any
byte outside the single JSON object makes the whole response unparseable.
`validate_reviewer_verdict` additionally now rejects an unexpected key set,
enforces strict types on `findings`, and NEVER trusts `reviewer.provider`/
`reviewer.model` from the response body — those two fields are always
overwritten with the controller-computed `provider`/`model` arguments, since
a builder or a compromised/malicious reviewer process could otherwise forge
whichever identity it wants into the accepted evidence. An ACCEPT decision
additionally now requires the caller-supplied `returncode` to be 0 — a
non-zero process exit is never masked by an otherwise well-formed ACCEPT
payload in stdout.
"""
from __future__ import annotations

import json
from typing import Any

VALID_VERDICTS = {"ACCEPT", "P1", "BLOCK"}
_DECISION = {"ACCEPT": "pass", "P1": "p1", "BLOCK": "block"}
_ALLOWED_TOP_KEYS = {"candidate_tree", "verdict", "findings", "reviewer"}
_ALLOWED_REVIEWER_KEYS = {"provider", "model"}


def parse_reviewer_response(raw_text: str) -> dict[str, Any] | None:
    """A0-2: strict JSON only — `json.loads(raw.strip())`, nothing else.

    Any byte before or after the single top-level JSON object (prose,
    markdown fences, a trailing log line) makes the response unparseable,
    which the caller must treat as BLOCK, never as an implicit pass. There is
    deliberately no fallback that hunts for an embedded `{...}` inside a
    larger text — that behavior is exactly what let a reviewer bury a
    forged/duplicated ACCEPT object inside free text and have it accepted.
    """
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


def validate_reviewer_verdict(raw_text: str, *, expected_candidate_tree: str | None,
                              provider: str, model: str, returncode: int = 0) -> dict[str, Any]:
    """RI-4/A0-2 gate. Returns a dict with at least {"ok", "decision"}; "proof" is
    present only when a strict, hash-matched, schema-valid verdict was found.

    `expected_candidate_tree=None` skips the candidate-hash binding check —
    used only for the pre-candidate "plan" review stage, which by definition
    has no candidate yet to bind to. Every post-freeze stage ("build",
    "final") must always pass a real hash here.

    `returncode` is the reviewer process's own exit code (default 0 for
    non-process callers such as an imported proof file). A0-2: ACCEPT
    requires simultaneously returncode==0 AND schema_valid AND
    candidate_tree==expected AND verdict=="ACCEPT" — a non-zero exit can
    never be masked by an otherwise well-formed ACCEPT payload in stdout.
    """
    obj = parse_reviewer_response(raw_text)
    if obj is None:
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": "reviewer response is not strict, machine-parseable JSON matching the RI-4 contract "
                          "(json.loads(raw.strip()) only — no prose before or after the object is tolerated)",
                "raw_tail": (raw_text or "")[-2000:]}

    extra_keys = sorted(set(obj.keys()) - _ALLOWED_TOP_KEYS)
    if extra_keys:
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": f"A0-2: reviewer JSON has unexpected top-level keys {extra_keys}"}

    verdict = obj.get("verdict")
    if verdict not in VALID_VERDICTS:
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": f"verdict missing or not one of {sorted(VALID_VERDICTS)}",
                "raw_verdict": verdict}

    candidate_tree = obj.get("candidate_tree")
    if candidate_tree is not None and not isinstance(candidate_tree, str):
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": "A0-2: candidate_tree must be a string or absent"}

    findings = obj.get("findings", [])
    if not isinstance(findings, list) or not all(isinstance(item, str) for item in findings):
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": "A0-2: findings must be a list of strings"}

    reviewer_meta = obj.get("reviewer")
    if not isinstance(reviewer_meta, dict):
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": "A0-2: reviewer metadata object is missing or not an object"}
    reviewer_extra = sorted(set(reviewer_meta.keys()) - _ALLOWED_REVIEWER_KEYS)
    if reviewer_extra:
        return {"ok": False, "decision": "block", "schema_valid": False,
                "reason": f"A0-2: reviewer metadata has unexpected keys {reviewer_extra}"}

    if expected_candidate_tree is not None and (not candidate_tree or candidate_tree != expected_candidate_tree):
        return {"ok": False, "decision": "block", "schema_valid": True,
                "reason": "candidate_tree absent or mismatched",
                "expected_candidate_tree": expected_candidate_tree,
                "received_candidate_tree": candidate_tree}

    returncode_ok = returncode == 0
    accept = verdict == "ACCEPT" and returncode_ok
    return {
        "ok": accept,
        "decision": _DECISION[verdict] if returncode_ok else "block",
        "verdict": verdict, "schema_valid": True, "returncode": returncode,
        "proof": {
            "candidate_tree": candidate_tree, "verdict": verdict,
            "findings": findings,
            # A0-2: provider/model are ALWAYS the controller-computed values.
            # Whatever the reviewer's own JSON claims for `reviewer.provider`/
            # `reviewer.model` is read only far enough to validate its shape
            # above, then discarded — never trusted into the evidence record.
            "reviewer": {"provider": provider, "model": model},
        },
    }
