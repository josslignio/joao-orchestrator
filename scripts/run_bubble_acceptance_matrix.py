#!/usr/bin/env python3
"""Run the eight Bubble routing selections sequentially with auditable evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joao_orchestrator.bubble.api import LocalAPIServer  # noqa: E402
from joao_orchestrator.bubble.runtime import (  # noqa: E402
    ClaudeCLIReviewer,
    CodexBuilder,
    CodexCLIReviewer,
    GLMBuilder,
    RunRuntime,
)


MISSION = """JOÃO SIMPLE TODO TEST

Work only inside a new disposable sandbox.

Create `todo.py` with:

- `add TEXT`
- `list`
- `done ID`
- `delete ID`

Store tasks in a local JSON file.

Requirements:

- stable numeric IDs;
- clear error for an unknown ID;
- clear error for invalid JSON;
- atomic JSON writes;
- no external dependency;
- no network access.

Create 6 deterministic tests covering:

1. empty list;
2. adding a task;
3. adding multiple tasks;
4. marking a task done;
5. deleting a task;
6. invalid or unknown data handling.

Run the tests and finish in `needs_approval`.

Never touch JOÃO, Job/CV Bot, Trading Radar or another existing repository."""

MATRIX = [
    ("glm", "none"),
    ("glm", "codex"),
    ("glm", "claude"),
    ("glm", "codex_and_claude"),
    ("codex", "none"),
    ("codex", "codex"),
    ("codex", "claude"),
    ("codex", "codex_and_claude"),
]
TERMINAL = {"needs_approval", "blocked", "failed", "stopped", "accepted"}


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def request(api: LocalAPIServer, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        api.url + path.lstrip("/"), data=data,
        method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json", "X-JOAO-Token": api.token},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read() or b"{}")
        raise RuntimeError(body.get("error", f"HTTP {exc.code}")) from exc


def wait_for(api: LocalAPIServer, run_id: str, wanted: set[str], timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = request(api, f"runs/{run_id}")
        if run["status"] in wanted:
            return run
        time.sleep(0.5)
    raise TimeoutError(f"run {run_id} did not reach {sorted(wanted)} within {timeout}s")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime(state_root: Path) -> RunRuntime:
    glm = GLMBuilder()
    codex_builder = CodexBuilder()
    codex_reviewer = CodexCLIReviewer()
    claude_reviewer = ClaudeCLIReviewer()
    return RunRuntime(
        state_root, builder=glm, builders={"glm": glm, "codex": codex_builder},
        reviewer=codex_reviewer,
        reviewers={"codex": codex_reviewer, "claude": claude_reviewer},
        allow_test_adapters=False,
    )


def execute_row(api: LocalAPIServer, state_root: Path, builder: str, review: str,
                timeout: int) -> dict:
    row = {
        "builder": builder, "review_mode": review, "started_from_bubble": False,
        "real_provider_verified": False, "tests": "NOT_RUN", "review_result": "NOT_RUN",
        "delivery_state": "NOT_STARTED", "post_control_state": None,
        "pause_resume": "NOT_RUN", "stop": "NOT_RUN", "restart_restored": False,
        "evidence": None, "scenario_status": "FAILED", "error": None,
    }
    runs_before = set((state_root / "runs").glob("run-*")) if (state_root / "runs").exists() else set()
    try:
        launched = request(api, "quick-missions", {
            "mission": MISSION, "builder_name": builder, "review_mode": review,
        })
    except Exception as exc:
        runs_after = set((state_root / "runs").glob("run-*")) if (state_root / "runs").exists() else set()
        row.update({
            "delivery_state": "PREFLIGHT_REFUSED", "review_result": "UNAVAILABLE",
            "scenario_status": "PREFLIGHT_REFUSED", "error": str(exc),
            "preflight_created_no_run": runs_before == runs_after,
        })
        return row

    run_id = launched["run_id"]
    row.update({"started_from_bubble": True, "run_id": run_id,
                "evidence": str(state_root / "runs" / run_id)})

    request(api, f"runs/{run_id}/pause", {})
    paused_or_terminal = wait_for(api, run_id, TERMINAL | {"paused"}, timeout)
    if paused_or_terminal["status"] == "paused":
        request(api, f"runs/{run_id}/resume", {})
        row["pause_resume"] = "VERIFIED"
    else:
        row["pause_resume"] = "REACHED_TERMINAL_BEFORE_PAUSE"

    final = wait_for(api, run_id, TERMINAL, timeout)
    row["delivery_state"] = final["status"]
    folder = state_root / "runs" / run_id
    builder_evidence = json.loads((folder / "builder-evidence.json").read_text()) if (folder / "builder-evidence.json").exists() else {}
    tests = json.loads((folder / "test-results.json").read_text()) if (folder / "test-results.json").exists() else {}
    review_evidence = json.loads((folder / "review-evidence.json").read_text()) if (folder / "review-evidence.json").exists() else {}
    row["real_provider_verified"] = bool(
        builder_evidence.get("real_or_mock") == "real"
        and builder_evidence.get("provider") == final.get("builder_provider")
        and builder_evidence.get("model") == final.get("builder_model")
        and builder_evidence.get("returncode") == 0
    )
    row["tests"] = "PASS" if tests.get("all_passed") else "FAIL"
    row["review_result"] = (
        "NONE_HUMAN_GATE" if review == "none" else review_evidence.get("proof", {}).get("verdict", "MISSING")
    )
    restored = runtime(state_root).get(run_id)
    row["restart_restored"] = restored["status"] == final["status"]
    row["final_diff_sha256"] = final.get("final_diff_sha256")
    row["manifest_sha256"] = sha256(folder / "manifest.json") if (folder / "manifest.json").exists() else None
    row["scenario_status"] = "VERIFIED" if (
        final["status"] == "needs_approval" and row["real_provider_verified"]
        and row["tests"] == "PASS" and row["restart_restored"]
        and (review == "none" or row["review_result"] == "ACCEPT")
    ) else "FAILED"

    stopped = request(api, f"runs/{run_id}/stop", {})
    row["post_control_state"] = stopped["status"]
    row["stop"] = "VERIFIED" if stopped["status"] == "stopped" else "FAILED"
    return row


def markdown(report: dict) -> str:
    lines = [
        "# JOÃO Bubble provider-routing acceptance matrix", "",
        f"Generated: `{report['generated_at']}`", "",
        "| Builder | Review mode | Started from Bubble | Real provider verified | Tests | Review result | Delivery state | Scenario | Evidence |",
        "|---|---|---:|---:|---|---|---|---|---|",
    ]
    for row in report["rows"]:
        lines.append(
            f"| {row['builder']} | {row['review_mode']} | {row['started_from_bubble']} | "
            f"{row['real_provider_verified']} | {row['tests']} | {row['review_result']} | "
            f"{row['delivery_state']} | {row['scenario_status']} | {row.get('evidence') or row.get('error')} |"
        )
    lines.extend(["", "Unavailable configurations are tested preflight refusals, never PASS or skipped.", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--timeout", type=int, default=2400)
    args = parser.parse_args()
    state_root = (args.state_root or Path("~/.local/share/joao/acceptance").expanduser() / ("provider-routing-" + utc_stamp())).resolve()
    state_root.mkdir(parents=True, exist_ok=False)
    api = LocalAPIServer(runtime(state_root))
    api.serve_in_thread()
    try:
        report = {
            "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "state_root": str(state_root), "mission_sha256": hashlib.sha256(MISSION.encode()).hexdigest(),
            "preflight": api.capabilities(), "rows": [],
        }
        for builder, review in MATRIX:
            report["rows"].append(execute_row(api, state_root, builder, review, args.timeout))
            (state_root / "matrix-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        (state_root / "matrix-report.md").write_text(markdown(report))
        sums = [
            f"{sha256(state_root / name)}  {name}"
            for name in ("matrix-report.json", "matrix-report.md")
        ]
        (state_root / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n")
        print(json.dumps({"state_root": str(state_root), "rows": report["rows"]}, indent=2))
        return 0 if all(row["scenario_status"] in {"VERIFIED", "PREFLIGHT_REFUSED"} for row in report["rows"]) else 1
    finally:
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
