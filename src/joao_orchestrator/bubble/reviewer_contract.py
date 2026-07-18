"""RI-4: the reviewer's verdict is a strict JSON contract bound to the exact
build candidate. A hash that is absent, mismatched, or a free-text response
instead of parseable JSON is rejected by the controller outright — never
"trusted anyway" (that fail-open path is exactly ATTACK_TEST 4's scenario:
a reviewer answering for some other SHA).
"""
from __future__ import annotations

import json
from typing import Any

VALID_VERDICTS = {"ACCEPT", "P1", "BLOCK"}
_DECISION = {"ACCEPT": "pass", "P1": "p1", "BLOCK": "block"}


def parse_reviewer_response(raw_text: str) -> dict[str, Any] | None:
    """Extract the last top-level JSON object in `raw_text`. None if no valid
    JSON object is present — a free-text-only response has no verdict at all,
    which the caller must treat as BLOCK, never as an implicit pass."""
    text = (raw_text or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    depth = 0
    start = None
    candidates: list[str] = []
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(text[start:i + 1])
    for chunk in reversed(candidates):
        try:
            obj = json.loads(chunk)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def validate_reviewer_verdict(raw_text: str, *, expected_candidate_tree: str | None,
                              provider: str, model: str) -> dict[str, Any]:
    """RI-4 gate. Returns a dict with at least {"ok", "decision"}; "proof" is
    present only when a strict, hash-matched verdict was found.

    `expected_candidate_tree=None` skips the candidate-hash binding check —
    used only for the pre-candidate "plan" review stage, which by definition
    has no candidate yet to bind to. Every post-freeze stage ("build",
    "final") must always pass a real hash here.
    """
    obj = parse_reviewer_response(raw_text)
    if obj is None:
        return {"ok": False, "decision": "block",
                "reason": "reviewer response is not machine-parseable JSON matching the RI-4 contract",
                "raw_tail": (raw_text or "")[-2000:]}
    verdict = obj.get("verdict")
    candidate_tree = obj.get("candidate_tree")
    reviewer_meta = obj.get("reviewer") if isinstance(obj.get("reviewer"), dict) else {}
    if verdict not in VALID_VERDICTS:
        return {"ok": False, "decision": "block",
                "reason": f"verdict missing or not one of {sorted(VALID_VERDICTS)}",
                "raw_verdict": verdict}
    if expected_candidate_tree is not None and (not candidate_tree or candidate_tree != expected_candidate_tree):
        return {"ok": False, "decision": "block",
                "reason": "candidate_tree absent or mismatched",
                "expected_candidate_tree": expected_candidate_tree,
                "received_candidate_tree": candidate_tree}
    return {
        "ok": verdict == "ACCEPT", "decision": _DECISION[verdict], "verdict": verdict,
        "proof": {
            "candidate_tree": candidate_tree, "verdict": verdict,
            "findings": obj.get("findings", []),
            "reviewer": {"provider": reviewer_meta.get("provider") or provider,
                        "model": reviewer_meta.get("model") or model},
        },
    }
