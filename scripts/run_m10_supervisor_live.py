from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from joao_orchestrator.providers.bridge_factory import build_default_bridge
from joao_orchestrator.supervisor import SupervisorCore, SupervisorRequest


MARKER = "JOAO_M10_OK"


def is_exact_marker(text: str) -> bool:
    # Byte-exact comparison: no stripping, no trimming, no normalization.
    # The provider response must be exactly "JOAO_M10_OK" with no surrounding
    # whitespace, newlines, or any other characters.
    return text == MARKER


def git_status(path: Path) -> str:
    if not (path / ".git").exists():
        return "NOT_A_GIT_WORKTREE"
    return subprocess.check_output(
        ["git", "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
        text=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--preferred", default="codex-review")
    args = parser.parse_args()

    evidence_path = Path(args.evidence).expanduser().resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    worktree = Path(args.worktree).expanduser().resolve()
    before = git_status(worktree)

    state_root = Path(args.state_root).expanduser().resolve()
    provider_root = state_root / "m10-provider-sandbox"
    provider_root.mkdir(parents=True, exist_ok=True)
    os.chdir(provider_root)

    bridge = build_default_bridge(timeout_seconds=240)
    core = SupervisorCore(bridge, state_root)
    candidates = [
        desc.name for desc in bridge.candidates(role="reviewer")
        if desc.name in {"codex-review", "glm-chat", "claude-chat"}
    ]
    if args.preferred:
        candidates.sort(key=lambda name: (name != args.preferred, name))

    attempts = []
    success = None
    prompt = (
        "This is the supervised JOAO M10 read-only smoke test. "
        "Reply with exactly JOAO_M10_OK and nothing else."
    )
    for index, provider in enumerate(candidates[:3], 1):
        result = core.execute(SupervisorRequest(
            task_id=f"m10-{index}",
            project_id="joao",
            prompt=prompt,
            mode="direct",
            role="reviewer",
            preferred_provider=provider,
            max_provider_calls=1,
            worktree_path=str(provider_root),
        ))
        record = result.to_dict()
        record["marker_match"] = is_exact_marker(result.final_content)
        attempts.append(record)
        if result.status == "completed" and is_exact_marker(result.final_content):
            success = result
            break

    after = git_status(worktree)
    worktree_unchanged = before == after
    clean = worktree_unchanged and before == ""
    verdict = {
        "schema_version": 2,
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "inventory": bridge.inventory(),
        "attempts": attempts,
        "selected_provider": success.selected_provider if success else None,
        "exact_marker": bool(success) and is_exact_marker(success.final_content),
        "worktree_status_before": before,
        "worktree_status_after": after,
        "worktree_unchanged": worktree_unchanged,
        "pass": bool(success) and clean,
        "limitations": [
            "Only an exact JOAO_M10_OK response counts as live provider success.",
            "Rate-limit, quota, empty or unrelated responses are failures.",
            "No merge, promotion, deployment or autonomous night run is performed.",
        ],
    }
    evidence_path.write_text(json.dumps(verdict, sort_keys=True, indent=2) + "\n")
    print(json.dumps(verdict, sort_keys=True, indent=2))
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
