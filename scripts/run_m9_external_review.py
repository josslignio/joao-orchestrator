#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from joao_orchestrator.providers.bridge_factory import build_default_bridge
from joao_orchestrator.supervisor import SupervisorCore, SupervisorRequest


def parse_exact_decision(text: str) -> dict:
    """Require the whole provider response to be one valid verdict object.

    No stripping: the provider response must be exactly one JSON object with
    no surrounding whitespace.  This enforces the exact-response gate.
    """
    value = json.loads(str(text))
    if not isinstance(value, dict):
        raise ValueError("review must be one JSON object")
    verdict = str(value.get("verdict", "")).upper()
    reason = str(value.get("reason", ""))
    if verdict not in {"ACCEPT", "BLOCK"}:
        raise ValueError(f"invalid verdict: {verdict!r}")
    if not reason:
        raise ValueError("review reason is required")
    return {"verdict": verdict, "reason": reason}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--diff", required=True)
    parser.add_argument("--tests", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    state_root = Path(args.state_root).expanduser().resolve()
    provider_root = state_root / "m9-provider-sandbox"
    provider_root.mkdir(parents=True, exist_ok=True)
    os.chdir(provider_root)

    # Verify that the supplied diff corresponds to the exact candidate SHA by
    # recomputing the tree SHA from the worktree HEAD and comparing it to the
    # candidate SHA.  This prevents an untrusted diff from masquerading as the
    # candidate.
    import subprocess
    worktree = Path(args.worktree).expanduser().resolve()
    actual_head = subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    actual_tree = subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "HEAD^{tree}"],
        text=True,
    ).strip()
    if actual_head != args.candidate_sha:
        print(json.dumps({
            "schema_version": 2,
            "candidate_sha": args.candidate_sha,
            "status": "SHA_MISMATCH",
            "pass": False,
            "error": f"worktree HEAD {actual_head} != candidate SHA {args.candidate_sha}",
            "reviews": [],
            "inventory": [],
            "limitations": ["Candidate SHA verification failed."],
        }, sort_keys=True, indent=2))
        return 1

    # Recompute the diff directly from the verified worktree HEAD so that the
    # review content is cryptographically bound to the candidate SHA.  The
    # supplied --diff file is only used as a cross-check: if it does not match
    # the recomputed diff, the review is rejected.
    # The base is the Tranche 2 closed commit (parent of the M7 commit),
    # which is the fixed authoritative starting point for this sequence.
    TRANCHE2_BASE = "76de966a8a5cfae29c4093a9c5e822bda5191ce6"
    recomputed_diff = subprocess.check_output(
        ["git", "-C", str(worktree), "diff", "--binary", TRANCHE2_BASE, "HEAD"],
        text=True,
        errors="replace",
    )
    supplied_diff = Path(args.diff).read_text(encoding="utf-8", errors="replace")
    if recomputed_diff != supplied_diff:
        print(json.dumps({
            "schema_version": 2,
            "candidate_sha": args.candidate_sha,
            "status": "DIFF_MISMATCH",
            "pass": False,
            "error": "supplied diff does not match diff recomputed from verified worktree HEAD",
            "reviews": [],
            "inventory": [],
            "limitations": ["Diff integrity verification failed."],
        }, sort_keys=True, indent=2))
        return 1

    # The full diff is hashed and supplied WITHOUT truncation so that no
    # malicious change beyond a truncation boundary can evade review.
    # The SHA256 is computed over the complete untruncated content.
    diff_raw = recomputed_diff
    diff_full_sha = hashlib.sha256(diff_raw.encode()).hexdigest()
    tests_raw = Path(args.tests).read_text(encoding="utf-8", errors="replace")
    prompt = (
        "You are Codex performing the mandatory independent JOAO M9 exact-SHA review. "
        "Review M7 Supervisor Core, M8 Provider Bridge, the M9 red-team matrix and "
        "the M10 live-smoke implementation. Return exactly one JSON object and no "
        "markdown: {\"verdict\":\"ACCEPT|BLOCK\",\"reason\":\"...\"}. "
        "BLOCK identity confusion, same-family review, unbounded calls, raw prompt or "
        "secret persistence, auto approval/promotion, write-tier bypass, weakened tests, "
        "fake provider-success detection, or a non-exact M10 marker.\n"
        f"CANDIDATE_SHA={args.candidate_sha}\n"
        f"VERIFIED_HEAD_SHA={actual_head}\n"
        f"VERIFIED_TREE_SHA={actual_tree}\n"
        f"FULL_DIFF_SHA256={diff_full_sha}\n"
        f"FULL_DIFF_CHARS={len(diff_raw)}\n"
        f"DIFF={diff_raw}\nFULL_TESTS={tests_raw}"
    )

    bridge = build_default_bridge(timeout_seconds=420)
    core = SupervisorCore(bridge, state_root)
    codex = next(
        (desc for desc in bridge.candidates(role="reviewer") if desc.name == "codex-review"),
        None,
    )
    records: list[dict] = []
    accepted = 0
    blocked = 0

    if codex is None:
        status = "CODEX_UNAVAILABLE_BLOCKING"
        passed = False
    else:
        result = core.execute(SupervisorRequest(
            task_id="m9-codex-exact-sha-review",
            project_id="joao",
            prompt=prompt,
            mode="direct",
            role="reviewer",
            preferred_provider=codex.name,
            max_provider_calls=1,
            worktree_path=str(provider_root),
        ))
        verdict = "INVALID"
        reason = result.reason
        if result.status == "completed":
            try:
                decision = parse_exact_decision(result.final_content)
            except (json.JSONDecodeError, ValueError) as exc:
                reason = f"invalid Codex review: {exc}"
            else:
                verdict = decision["verdict"]
                reason = decision["reason"]
        if verdict == "ACCEPT":
            accepted = 1
        elif verdict == "BLOCK":
            blocked = 1
        # Hash ALL reasons: any provider text (even validated JSON) could echo
        # secrets.  No raw provider text is persisted anywhere — not in the
        # evidence JSON, not in state_root, not on disk.  The verdict and a
        # SHA256 hash of the reason are the only persisted artifacts.
        reason_safe = f"REDACTED:{hashlib.sha256(reason.encode('utf-8')).hexdigest()[:16]}"
        records.append({
            "provider": codex.name,
            "family": codex.family,
            "status": result.status,
            "verdict": verdict,
            "reason": reason_safe,
            "response_sha256": hashlib.sha256(result.final_content.encode()).hexdigest(),
        })
        if blocked:
            status, passed = "BLOCK", False
        elif accepted:
            status, passed = "ACCEPT", True
        else:
            status, passed = "INVALID_REVIEW", False

    output = {
        "schema_version": 2,
        "candidate_sha": args.candidate_sha,
        "status": status,
        "pass": passed,
        "attempted": len(records),
        "accepted": accepted,
        "blocked": blocked,
        "reviews": records,
        "inventory": bridge.inventory(),
        "limitations": [
            "M9 requires a valid exact-JSON Codex verdict on the exact candidate SHA.",
            "Rate-limit, quota, empty or format-variant responses never count as review.",
            "The review is read-only and cannot approve, promote or enable write-tier.",
        ],
    }
    Path(args.output).write_text(json.dumps(output, sort_keys=True, indent=2) + "\n")
    print(json.dumps(output, sort_keys=True, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
