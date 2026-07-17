"""V1.2 — the elapsed timer must freeze on terminal cards."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_provider_routing import runtime, start  # noqa: E402


def iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sandbox(tmp_path):
    import subprocess
    root = tmp_path / "ws"
    root.mkdir()
    for argv in (["git", "init", "-q"], ["git", "config", "user.email", "t@e.i"],
                 ["git", "config", "user.name", "t"]):
        subprocess.run(argv, cwd=root, check=True)
    (root / "test_todo.py").write_text("def test_ok():\n    assert True\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)
    return root


def test_elapsed_freezes_at_terminal_state_and_runs_while_alive(tmp_path):
    rt = runtime(tmp_path)
    root = sandbox(tmp_path)
    run_id = start(rt, root)
    rt.stop(run_id)

    reference = datetime.now(timezone.utc)
    run = rt._read(run_id)
    run["created_at"] = iso(reference - timedelta(seconds=307))
    run["updated_at"] = iso(reference - timedelta(seconds=300))
    rt._write(run)

    # Frozen: exactly the 7 seconds between creation and the stop transition,
    # not the ~307 seconds a live clock would report.
    assert rt.get(run_id)["elapsed_seconds"] == 7
    assert rt.get(run_id)["elapsed_seconds"] == 7

    # A live (ready) run keeps counting from created_at.
    run = rt._read(run_id)
    run["status"] = "ready"
    rt._write(run)
    assert rt.get(run_id)["elapsed_seconds"] >= 300
