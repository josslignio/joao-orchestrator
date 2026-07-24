"""Evidence / review-packet generator (migrated + generalized from V1.4.0).

No full diff, no datasets, no secrets. Captured output is redacted + truncated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List

from ..domain.models import TaskMeta, ValidationRun


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _truncate(text: str, limit: int = 400) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f" …[+{len(text) - limit} bytes]"


def build_review_packet(task: TaskMeta,
                        validation: ValidationRun,
                        changed_paths: Iterable[str],
                        git_diff_stat: str,
                        diff_check_output: str = "",
                        reviewed_patch_sha256: str = "") -> str:
    changed = list(changed_paths)
    changed_block = "\n".join(f"- `{p}`" for p in changed) if changed else "_(none)_"

    cmd_rows = []
    for c in validation.commands:
        argv_str = " ".join(c.get("argv", []))
        rc = c.get("returncode", "?")
        ok = "OK" if c.get("ok") else "FAIL"
        reason = c.get("reason", "")
        tail = _truncate((c.get("stderr") or c.get("stdout") or ""))
        note = f" — {reason}" if reason else ""
        cmd_rows.append(f"| {ok} | `{rc}` | `{argv_str}` | {tail}{note} |")
    cmd_table = ("| Result | Exit | Command | Summary |\n|---|---|---|---|\n"
                 + "\n".join(cmd_rows)) if cmd_rows else "_(no commands)_"

    violations_block = ("\n".join(f"- {v}" for v in validation.violations)
                        if validation.violations else "_(none)_")
    
    # Fail-closed: validation.ok alone must never produce PASSED.
    # Before verified independent reviewer evidence, status must be REVIEW_NOT_RUN.
    if not reviewed_patch_sha256:
        verdict = "REVIEW_NOT_RUN — awaiting independent reviewer evidence"
    elif validation.ok:
        verdict = "PASSED — ready for human review"
    else:
        verdict = "FAILED — do not approve"
    
    diff_stat_block = _truncate(git_diff_stat or "", 800) or "_(unavailable)_"
    diff_check_block = _truncate(diff_check_output or "", 400)

    checklist = "\n".join(
        f"- [ ] {item}" for item in (
            "Changes are confined to allowed paths.",
            "No forbidden paths (.env, venvs, .git, secrets) modified.",
            "All required offline checks pass locally.",
            "No commits, pushes, or scheduler changes were made.",
            "No secrets are present in the diff or captured output.",
            "Changes align with the task request and done criteria.",
        )
    )

    return f"""# Review packet — {task.task_id}

- **Task ID:** `{task.task_id}`
- **Project:** `{task.project_id}`
- **Title:** {task.title}
- **Branch:** `{task.branch or "(unknown)"}`
- **State:** {task.state}
- **Size class:** {task.size_class or "(unset)"}
- **Generated:** {_now_iso()}
- **Schema version:** {task.schema_version}

## Verdict
**{verdict}**

## Done criteria
{task.done_criteria or "_(not specified)_"}

## Changed paths
{changed_block}

## Git diff stat
```
{diff_stat_block}
```

## git diff --check
```
{diff_check_block}
```

## Validation commands
{cmd_table}

## Policy violations
{violations_block}

## Human approval checklist
{checklist}

---
Evidence only. No automatic approval. A human must explicitly approve before
any commit. No full diff, datasets, or secrets are included.
"""
