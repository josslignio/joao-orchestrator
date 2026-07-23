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
    """Require the whole provider response to be one valid verdict object."""
    value = json.loads(str(text).strip())
    if not isinstance(value, dict):
        raise ValueError("review must be one JSON object")
    verdict = str(value.get("verdict", "")).upper()
    reason = str(value.get("reason", "")).strip()
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

    diff = Path(args.diff).read_text(encoding="utf-8", errors="replace")[:50000]
    tests = Path(args.tests).read_text(encoding="utf-8", errors="replace")[-16000:]
    prompt = (
        "You are Codex performing the mandatory independent JOAO M9 exact-SHA review. "
        "Review M7 Supervisor Core, M8 Provider Bridge, the M9 red-team matrix and "
        "the M10 live-smoke implementation. Return exactly one JSON object and no "
        "markdown: {\"verdict\":\"ACCEPT|BLOCK\",\"reason\":\"...\"}. "
        "BLOCK identity confusion, same-family review, unbounded calls, raw prompt or "
        "secret persistence, auto approval/promotion, write-tier bypass, weakened tests, "
        "fake provider-success detection, or a non-exact M10 marker.\n"
        f"CANDIDATE_SHA={args.candidate_sha}\n"
        f"DIFF_SHA256={hashlib.sha256(diff.encode()).hexdigest()}\n"
        f"DIFF={diff}\nTESTS={tests}"
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
        records.append({
            "provider": codex.name,
            "family": codex.family,
            "status": result.status,
            "verdict": verdict,
            "reason": reason,
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
