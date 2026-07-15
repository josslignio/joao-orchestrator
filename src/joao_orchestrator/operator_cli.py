"""C8 Operator CLI — lightweight read-only operator interface.

Commands:
  joss status        — reads the ledger, prints run state
  joss roadmaps      — lists roadmaps for a project
  joss queue         — lists materialized queue items for a roadmap
  joss runs          — lists nightly batch reports
  joss blockers      — lists open blockers
  joss resume        — prints the exact safe resume command for a paused task
  joss report        — prints the latest morning report
  joss capabilities  — lists declared capability contracts

Read-only by default. Mutating commands (resume) require explicit scoped action
and only print the safe command — they do NOT execute it.

No web dashboard. No credential output.

Deterministic. No network. Stdlib only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .capabilities.registry import default_registry
from .roadmap.store import list_roadmaps, load_roadmap
from .roadmap.compiler import materialize_to_queue


@dataclass(frozen=True)
class CLIOutput:
    """The output of one CLI command."""

    command: str
    exit_code: int
    text: str
    redacted: bool   # True if any credential was redacted

    def __str__(self) -> str:
        return self.text


def _load_ledger(state_root: Path) -> dict[str, Any]:
    """Load the run ledger. Returns empty dict if not found."""
    p = state_root / "overnight" / "c3-c6-r5-ultra" / "ledger.json"
    if not p.is_file():
        # Fall back to any ledger under overnight/.
        overnight = state_root / "overnight"
        if overnight.is_dir():
            for run_dir in sorted(overnight.iterdir(), reverse=True):
                lp = run_dir / "ledger.json"
                if lp.is_file():
                    p = lp
                    break
    if not p.is_file():
        return {}
    return json.loads(p.read_text())


def _load_blockers(state_root: Path) -> list[dict[str, Any]]:
    p = state_root / "overnight" / "c3-c6-r5-ultra" / "blockers.json"
    if not p.is_file():
        return []
    data = json.loads(p.read_text())
    return data.get("blockers", []) if isinstance(data, dict) else []


def cmd_status(state_root: Path) -> CLIOutput:
    """joss status — print run state from the ledger."""
    ledger = _load_ledger(state_root)
    if not ledger:
        return CLIOutput("status", 1, "No run ledger found.", False)
    lines = [
        f"Run: {ledger.get('run_id', '?')}",
        f"Phase: {ledger.get('current_phase', '?')}",
        f"Status: {ledger.get('status', 'RUNNING')}",
    ]
    checkpoints = ledger.get("checkpoint_status", {})
    for cp, status in sorted(checkpoints.items()):
        lines.append(f"  {cp}: {status}")
    commits = ledger.get("commits", [])
    lines.append(f"Commits: {len(commits)}")
    prs = ledger.get("PRs", [])
    if prs:
        lines.append(f"PRs: {len(prs)}")
    return CLIOutput("status", 0, "\n".join(lines), False)


def cmd_roadmaps(state_root: Path, project_id: str = "") -> CLIOutput:
    """joss roadmaps — list roadmaps for a project."""
    if not project_id:
        return CLIOutput("roadmaps", 1,
                         "Usage: joss roadmaps <project_id>", False)
    listed = list_roadmaps(state_root, project_id)
    if not listed:
        return CLIOutput("roadmaps", 0, f"No roadmaps for {project_id}.", False)
    lines = [f"Roadmaps for {project_id}:"]
    for r in listed:
        lines.append(f"  {r['roadmap_id']} [{r['state']}] tasks={r['task_count']}")
        lines.append(f"    objective: {r['objective'][:80]}")
    return CLIOutput("roadmaps", 0, "\n".join(lines), False)


def cmd_queue(state_root: Path, project_id: str = "", roadmap_id: str = "") -> CLIOutput:
    """joss queue — list materialized queue items for a roadmap."""
    if not project_id or not roadmap_id:
        return CLIOutput("queue", 1,
                         "Usage: joss queue <project_id> <roadmap_id>", False)
    rm = load_roadmap(state_root, project_id, roadmap_id)
    if rm is None:
        return CLIOutput("queue", 1, f"Roadmap not found: {roadmap_id}", False)
    if rm.state != "READY":
        return CLIOutput("queue", 1,
                         f"Roadmap is {rm.state}; only READY roadmaps materialize.", False)
    items = materialize_to_queue(rm)
    lines = [f"Queue for {roadmap_id} ({len(items)} tasks):"]
    for i in items:
        deps = ", ".join(i["dependencies"]) if i["dependencies"] else "-"
        lines.append(f"  {i['task_id']} [{i['complexity']}] deps={deps}")
    return CLIOutput("queue", 0, "\n".join(lines), False)


def cmd_runs(state_root: Path) -> CLIOutput:
    """joss runs — list nightly batch reports."""
    nightly = state_root / "nightly"
    if not nightly.is_dir():
        return CLIOutput("runs", 0, "No nightly runs found.", False)
    reports = sorted(nightly.glob("*.json"))
    reports = [r for r in reports if r.name != "batch_events.jsonl"]
    if not reports:
        return CLIOutput("runs", 0, "No nightly runs found.", False)
    lines = ["Nightly runs:"]
    for rp in reports[-10:]:
        try:
            data = json.loads(rp.read_text())
            lines.append(
                f"  {data.get('batch_id', rp.stem)} [{data.get('state', '?')}] "
                f"attempted={data.get('tasks_attempted', '?')} "
                f"accepted={data.get('tasks_accepted', '?')}"
            )
        except (json.JSONDecodeError, OSError):
            continue
    return CLIOutput("runs", 0, "\n".join(lines), False)


def cmd_blockers(state_root: Path) -> CLIOutput:
    """joss blockers — list open blockers."""
    blockers = _load_blockers(state_root)
    if not blockers:
        return CLIOutput("blockers", 0, "No open blockers.", False)
    lines = [f"Open blockers ({len(blockers)}):"]
    for b in blockers:
        lines.append(f"  {b.get('phase', '?')}: {b.get('detail', '')}")
    return CLIOutput("blockers", 0, "\n".join(lines), False)


def cmd_resume(state_root: Path, task_id: str = "") -> CLIOutput:
    """joss resume — print the exact safe resume command for a paused task.

    Read-only: prints the command, does NOT execute it.
    """
    if not task_id:
        return CLIOutput("resume", 1, "Usage: joss resume <task_id>", False)
    drift_dir = state_root / "drift_control"
    resume_path = drift_dir / f"resume_{task_id}.json"
    if not resume_path.is_file():
        return CLIOutput("resume", 1,
                         f"No resume point for task {task_id}.", False)
    data = json.loads(resume_path.read_text())
    lines = [
        f"Resume point for {task_id}:",
        f"  state: {data.get('state', '?')}",
        f"  last_accepted_commit: {data.get('last_accepted_commit', '?')}",
        f"  remaining_tasks: {data.get('remaining_tasks', [])}",
        f"  failure_signature: {data.get('failure_signature', '')}",
        f"  required_human_decision: {data.get('required_human_decision', '')}",
        f"  next_safe_command: {data.get('next_safe_command', '(none)')}",
        "",
        "This command is read-only. To resume, run the next_safe_command manually.",
    ]
    return CLIOutput("resume", 0, "\n".join(lines), False)


def cmd_report(state_root: Path) -> CLIOutput:
    """joss report — print the latest morning report."""
    nightly = state_root / "nightly"
    if not nightly.is_dir():
        return CLIOutput("report", 1, "No reports found.", False)
    reports = sorted(nightly.glob("*.json"))
    reports = [r for r in reports if r.name != "batch_events.jsonl"]
    if not reports:
        return CLIOutput("report", 1, "No reports found.", False)
    data = json.loads(reports[-1].read_text())
    lines = [
        f"Morning report: {data.get('batch_id', '?')}",
        f"  state: {data.get('state', '?')}",
        f"  attempted/accepted/rejected: {data.get('tasks_attempted', 0)}/"
        f"{data.get('tasks_accepted', 0)}/{data.get('tasks_rejected', 0)}",
        f"  commits: {data.get('commits', 0)}",
        f"  model_call_proxy: {data.get('model_call_proxy', 0)}",
        f"  context_bytes: {data.get('context_bytes', 0)}",
        f"  cache_hit_rate: {data.get('cache_hit_rate', 0):.1%}",
        f"  drift_events: {data.get('drift_events', 0)}",
        f"  report_hash: {data.get('report_hash', '?')[:16]}",
    ]
    return CLIOutput("report", 0, "\n".join(lines), False)


def cmd_capabilities(state_root: Path) -> CLIOutput:
    """joss capabilities — list declared capability contracts.

    No credential output: required_secrets are listed by NAME only, never value.
    """
    reg = default_registry()
    lines = [f"Declared capabilities ({reg.count()}):"]
    for c in reg.all():
        secret_names = ", ".join(c.required_secrets) if c.required_secrets else "-"
        lines.append(
            f"  {c.capability_id} [{c.cost_class}] "
            f"network={c.network_required} secrets={secret_names}"
        )
    return CLIOutput("capabilities", 0, "\n".join(lines), True)


COMMANDS = {
    "status": cmd_status,
    "roadmaps": cmd_roadmaps,
    "queue": cmd_queue,
    "runs": cmd_runs,
    "blockers": cmd_blockers,
    "resume": cmd_resume,
    "report": cmd_report,
    "capabilities": cmd_capabilities,
}


def dispatch(state_root: Path, command: str, *args: str) -> CLIOutput:
    """Dispatch a CLI command. Returns a CLIOutput."""
    fn = COMMANDS.get(command)
    if fn is None:
        return CLIOutput(command, 1,
                         f"Unknown command: {command}\nCommands: {', '.join(COMMANDS)}",
                         False)
    return fn(state_root, *args)
