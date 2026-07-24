from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_watchdog_cleans_child_group_on_sigterm(tmp_path: Path):
    pid_file = tmp_path / "child.pid"
    child = (
        "import os,time,pathlib; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    watchdog = subprocess.Popen(
        [sys.executable, "scripts/run_with_watchdog.py", "--seconds", "60", "--", sys.executable, "-c", child],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_file.exists()
    child_pid = int(pid_file.read_text())
    watchdog.send_signal(signal.SIGTERM)
    stdout, stderr = watchdog.communicate(timeout=8)
    assert watchdog.returncode == 128 + signal.SIGTERM
    assert "RUNNIGHT_WATCHDOG_SIGNAL" in stderr
    deadline = time.monotonic() + 3
    while _pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(child_pid)
