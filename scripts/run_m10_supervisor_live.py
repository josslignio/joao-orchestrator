#!/usr/bin/env python3
"""M10 supervised read-only provider smoke.

Tries available read-only reviewer providers in a bounded order.  It never
requests workspace.write, never approves/promotes and verifies that the target
Git worktree remains byte-for-byte status-clean.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from joao_orchestrator.providers.bridge_factory import build_default_bridge
from joao_orchestrator.supervisor import SupervisorCore, SupervisorRequest


def git_status(path: Path) -> str:
    if not (path / ".git").exists():
        return "NOT_A_GIT_WORKTREE"
    return subprocess.check_output(
        ["git", "-C", str(path), "status", "--porcelain=v1"],
        text=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--worktree", default="")
    parser.add_argument("--preferred", default="")
    args = parser.parse_args()

    evidence_path = Path(args.evidence).expanduser().resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    worktree = Path(args.worktree).expanduser().resolve() if args.worktree else None
    before = git_status(worktree) if worktree else "NO_WORKTREE"

    bridge = build_default_bridge(timeout_seconds=180)
    core = SupervisorCore(bridge, Path(args.state_root))
    candidates = [
        desc.name for desc in bridge.candidates(role="reviewer")
        if desc.name in {"glm-chat", "claude-chat", "zai-coding-plan"}
    ]
    if args.preferred:
        candidates.sort(key=lambda name: name != args.preferred)

    attempts = []
    success = None
    prompt = (
        "This is the supervised JOAO M10 read-only smoke test. "
        "Reply with exactly JOAO_M10_OK and nothing else."
    )
    for index, provider in enumerate(candidates[:3], 1):
        request = SupervisorRequest(
            task_id=f"m10-{index}",
            project_id="joao",
            prompt=prompt,
            mode="direct",
            role="reviewer",
            preferred_provider=provider,
            max_provider_calls=1,
        )
        result = core.execute(request)
        record = result.to_dict()
        attempts.append(record)
        if result.status == "completed" and "JOAO_M10_OK" in result.final_content:
            success = result
            break

    after = git_status(worktree) if worktree else "NO_WORKTREE"
    clean = before == after and before in {"", "NO_WORKTREE", "NOT_A_GIT_WORKTREE"}
    verdict = {
        "schema_version": 1,
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "inventory": bridge.inventory(),
        "attempts": attempts,
        "selected_provider": success.selected_provider if success else None,
        "exact_marker": bool(success),
        "worktree_status_before": before,
        "worktree_status_after": after,
        "worktree_unchanged": before == after,
        "pass": bool(success) and clean,
        "limitations": [
            "One live read-only provider response is proved; this does not enable write-tier.",
            "No merge, promotion, deployment or autonomous night run is performed.",
        ],
    }
    evidence_path.write_text(json.dumps(verdict, sort_keys=True, indent=2) + "\n")
    print(json.dumps(verdict, sort_keys=True, indent=2))
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
