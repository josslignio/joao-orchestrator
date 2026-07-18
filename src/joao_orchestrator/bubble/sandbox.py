"""RI-6: sandboxed subprocess dispatch for builder/test/reviewer execution.

Guarantees attempted here: a fresh temporary HOME (zero inherited secrets via
env), a minimal allowlisted environment (`policy.environment.build_task_env`),
network denied by default, a bounded timeout, and PID/process-group tracking
so a subprocess that spawns children cannot outlive its parent's timeout kill
(RI-6 attack test 8 — the "zombie" case).

Network and file-read confinement are enforced at the OS level via macOS
Seatbelt (`sandbox-exec`) when present on PATH. `sandbox-exec` is macOS-only
and itself deprecated by Apple with no public replacement API; on any other
platform (or if the binary is missing) this module falls back to env-only
restriction and records `enforcement: "env-only"` in its result so callers
never present a soft convention as a hard boundary (D-043 — no "inattaquable"
claims; the honest claim is scoped to what `enforcement` says was used).
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import site
import tempfile
import time
from pathlib import Path
from typing import Any

from ..policy.environment import build_task_env

SANDBOX_EXEC = shutil.which("sandbox-exec")


def _quote(path: str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _interpreter_read_paths() -> list[str]:
    """Paths the running Python interpreter itself needs to read to start up."""
    paths = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}
    try:
        paths.update(site.getsitepackages())
    except Exception:
        pass
    return sorted(p for p in paths if p)


def seatbelt_profile(*, network: bool, read_write_paths: list[str], extra_read_paths: list[str]) -> str:
    """Build a deny-by-default Seatbelt profile (macOS `sandbox-exec -p`)."""
    rw = read_write_paths or []
    ro = list(dict.fromkeys(extra_read_paths + _interpreter_read_paths()))
    read_subpaths = "\n".join(f"  (subpath {_quote(p)})" for p in (ro + rw))
    write_subpaths = "\n".join(f"  (subpath {_quote(p)})" for p in rw) or '  (subpath "/private/var/empty")'
    write_subpaths += '\n  (literal "/dev/null")\n  (literal "/dev/tty")'
    net_rule = "(allow network*)" if network else "(deny network* (with no-log))"
    return f"""(version 1)
(deny default)
(allow process-fork process-exec)
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix-shm)
(allow file-read*
{read_subpaths}
  (subpath "/usr")
  (subpath "/bin")
  (subpath "/sbin")
  (subpath "/System")
  (subpath "/Library")
  (subpath "/private/etc")
  (subpath "/etc")
  (subpath "/opt/homebrew")
  (subpath "/usr/local")
  (subpath "/private/var")
  (subpath "/var")
  (subpath "/Library/Developer")
  (literal "/dev/null")
  (literal "/dev/urandom")
  (literal "/dev/random")
  (literal "/dev/tty")
  (literal "/"))
(allow file-write*
{write_subpaths})
(allow file-write-create
{write_subpaths})
{net_rule}
"""


def sandboxed_env(profile_allowlist, home_dir: Path) -> dict:
    """A minimal, deny-by-default environment: fresh temp HOME, no inherited secrets."""
    env = build_task_env(profile_allowlist)
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    env["HOME"] = str(home_dir)
    env["TMPDIR"] = str(home_dir)
    env["LANG"] = os.environ.get("LANG", "C.UTF-8")
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


class SandboxResult(dict):
    """Marker subclass so callers can tell a sandboxed run's result apart."""


def run_sandboxed(argv: list[str], *, cwd: Path, timeout: int,
                   profile_allowlist: list[str] | None = None,
                   network: bool = False,
                   extra_read_paths: list[str] | None = None,
                   extra_write_paths: list[str] | None = None) -> dict[str, Any]:
    """Run `argv` under sandbox-exec (when available) with a fresh temp HOME,
    a minimal env, network denied unless `network=True`, a bounded timeout, and
    the whole process group killed on timeout (never just the direct child).
    """
    with tempfile.TemporaryDirectory(prefix="joao-sandbox-home-") as home:
        home_dir = Path(home)
        env = sandboxed_env(profile_allowlist or [], home_dir)
        write_paths = [str(Path(cwd).resolve()), str(home_dir)] + [str(p) for p in (extra_write_paths or [])]
        enforcement = "env-only"
        full_argv = list(argv)
        if SANDBOX_EXEC:
            profile = seatbelt_profile(
                network=network,
                read_write_paths=write_paths,
                extra_read_paths=[str(p) for p in (extra_read_paths or [])],
            )
            profile_path = home_dir / "profile.sb"
            profile_path.write_text(profile)
            full_argv = [SANDBOX_EXEC, "-f", str(profile_path)] + list(argv)
            enforcement = "seatbelt"
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                full_argv, cwd=str(cwd), shell=False, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return {"ok": False, "argv": argv, "returncode": -1, "enforcement": enforcement,
                    "stdout": "", "stderr": f"failed to start: {exc}", "timed_out": False}
        pid = proc.pid
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            stdout, stderr = _kill_process_group(proc, pid)
        returncode = proc.returncode if proc.returncode is not None else 124
        duration = time.monotonic() - started
        return {
            "ok": returncode == 0 and not timed_out,
            "argv": argv, "returncode": returncode, "enforcement": enforcement,
            "network": bool(network), "pid": pid, "duration_seconds": round(duration, 3),
            "stdout": (stdout or "")[-16000:], "stderr": (stderr or "")[-16000:],
            "timed_out": timed_out,
        }


def _descendant_pids(root_pid: int) -> list[int]:
    """Walk the whole descendant tree by ppid — robust to a child that calls
    `setsid()` to escape the process group (ppid never changes on setsid,
    only pgid/sid do, so a pgid-only kill misses exactly this escape)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return [root_pid]
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            cpid, cppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(cppid, []).append(cpid)
    seen: list[int] = []
    frontier = [root_pid]
    visited: set[int] = set()
    while frontier:
        current = frontier.pop()
        if current in visited:
            continue
        visited.add(current)
        seen.append(current)
        frontier.extend(children.get(current, []))
    return seen


def _signal_all(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _kill_process_group(proc: subprocess.Popen, pid: int) -> tuple[str, str]:
    """Kill the whole process tree so a subprocess-spawned child cannot survive
    its parent's timeout — including one that calls `setsid()` to escape the
    original process group (RI-6 attack test 8 — the zombie case)."""
    descendants = _descendant_pids(pid)
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    _signal_all(descendants, signal.SIGTERM)
    try:
        stdout, stderr = proc.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        stdout, stderr = "", ""
    # Re-walk: a descendant may have spawned further children between sweeps.
    survivors = _descendant_pids(pid)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    _signal_all(survivors, signal.SIGKILL)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    return stdout or "", stderr or ""
