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

A0-5 (correction pass, 2026-07-19) adds three things RI-6/RI-7 were missing:

1. `protected=True` dispatches now fail closed (return `ok: False`,
   `enforcement: "refused-no-kernel-sandbox"`) rather than silently falling
   back to `env-only` when Seatbelt is unavailable. A caller marks a
   dispatch protected by passing `protected=True`; `RunRuntime` does this
   for any run with `critical=True`.
2. OS resource limits (CPU seconds, open file descriptors, process count via
   RLIMIT_NPROC) are applied via a `preexec_fn` on every sandboxed dispatch,
   protected or not — a runaway fork bomb is bounded by the kernel, not
   merely by an external timeout. `RLIMIT_AS` is ALSO set as a best-effort,
   defense-in-depth layer, but it is NOT the mechanism this module relies on
   to catch a memory hog: empirically, on this module's primary target
   platform (macOS/Darwin), `setrlimit(RLIMIT_AS, ...)` either fails
   outright for any bound below the parent interpreter's already-mapped
   virtual address space, or is silently not enforced by the XNU VM
   subsystem for `mmap`-backed allocations (which is how CPython allocates
   large objects) — a documented, well-known Darwin limitation, not a bug in
   this code. The mechanism this module actually relies on and that the
   A0-5 attack test verifies is a portable, parent-side RSS watchdog: a
   background thread polls the dispatched process tree's resident memory
   (`ps -o rss=`, same descendant/token tracking as the timeout-kill sweep)
   and kills the whole tree the moment it crosses `memory_bytes` — this
   works identically on Linux and macOS because it never depends on the
   kernel actually enforcing an rlimit.
3. The timeout-kill sweep, and now also the RSS watchdog's kill, are no
   longer purely ppid-based. A double-fork
   ("daemonize") pattern — fork, the immediate parent exits, the grandchild
   is reparented to PID 1 — breaks the ppid chain back to the tracked root
   PID entirely (that is the whole point of the trick), so RI-6's existing
   `_descendant_pids` walk structurally cannot find it. A per-dispatch
   random `JOAO_SANDBOX_TOKEN` environment variable — inherited across
   fork()/exec() by any well-behaved descendant, including a reparented one
   — lets the kill sweep additionally scan the ENTIRE process table
   (`ps -E`, same-UID only) for that token and kill matches the ppid walk
   missed. This is scoped honestly: a descendant that explicitly execs a
   fresh, stripped environment removes the marker and evades this specific
   check (documented in the A0.1 report's LIMITES section, not silently
   assumed away).
"""
from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import sys
import site
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:
    import resource
    _HAVE_RESOURCE = True
except ImportError:  # pragma: no cover - non-POSIX
    resource = None  # type: ignore[assignment]
    _HAVE_RESOURCE = False

from ..policy.environment import build_task_env

SANDBOX_EXEC = shutil.which("sandbox-exec")

# A0-5: default OS-level resource ceilings applied to every sandboxed
# dispatch. Generous enough not to trip on a legitimate short build/test
# command on this codebase's own CI/dev hosts, tight enough to bound a
# runaway fork bomb or memory hog well before it can do host-wide damage.
# Callers needing a tighter bound for a specific attack surface (e.g. an
# attack test proving the mechanism) pass an explicit override.
DEFAULT_CPU_SECONDS = 120
DEFAULT_MEMORY_BYTES = 3_000_000_000  # ~3 GB address space (RLIMIT_AS)
DEFAULT_MAX_OPEN_FILES = 256
DEFAULT_MAX_NEW_PROCESSES = 64  # headroom ABOVE the current per-UID process count


def _current_uid_process_count() -> int:
    """A0-5: RLIMIT_NPROC is a per-real-UID ceiling, not a per-subtree one —
    on a shared dev host the UID may already own hundreds of unrelated
    processes, so the limit we hand to a child must be `current + budget`,
    never a bare absolute number (which would either refuse immediately on a
    busy host or be meaningless on an idle one)."""
    try:
        uid = os.getuid()
    except AttributeError:  # pragma: no cover - non-POSIX
        return 0
    try:
        out = subprocess.run(["ps", "-axo", "uid="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return sum(1 for line in out.splitlines() if line.strip() == str(uid))


def _resource_limits_preexec(cpu_seconds: int | None, memory_bytes: int | None,
                             max_open_files: int | None, nproc_ceiling: int | None):
    """Build a `preexec_fn` that applies rlimits in the child right before
    exec. Each limit is best-effort/independent — one unsupported limit on a
    given platform must never prevent the others from being applied."""
    def _apply() -> None:
        if not _HAVE_RESOURCE:
            return
        for limit_name, value in (
            ("RLIMIT_CPU", cpu_seconds),
            ("RLIMIT_AS", memory_bytes),
            ("RLIMIT_NOFILE", max_open_files),
            ("RLIMIT_NPROC", nproc_ceiling),
        ):
            if value is None or not hasattr(resource, limit_name):
                continue
            try:
                resource.setrlimit(getattr(resource, limit_name), (value, value))
            except (ValueError, OSError):
                pass
    return _apply


def _token_tagged_pids(token: str) -> list[int]:
    """A0-5: system-wide (same-UID) scan for processes whose environment
    still carries our per-dispatch marker — catches a double-fork escapee
    the ppid walk cannot see because the trick deliberately orphans it to
    PID 1, breaking the ppid chain back to the tracked root PID."""
    try:
        out = subprocess.run(["ps", "-E", "-e", "-ww", "-o", "pid=,command="],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids: list[int] = []
    for line in out.splitlines():
        if token not in line:
            continue
        head = line.strip().split(None, 1)
        if head and head[0].isdigit():
            pids.append(int(head[0]))
    return pids


def _rss_bytes(pid: int) -> int:
    """Resident set size of a single PID, in bytes (0 if it no longer exists
    or `ps` cannot be read in time — never raises)."""
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        return int(out) * 1024 if out else 0
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return 0


def _total_tracked_rss_bytes(root_pid: int, token: str | None) -> int:
    """A0-5: sum of RSS across every PID this dispatch is responsible for —
    the ppid-walked descendant tree PLUS anything still carrying the
    per-dispatch environment token (catches a double-forked/reparented
    process the ppid walk alone would miss, same reasoning as the
    timeout-kill sweep)."""
    pids = set(_descendant_pids(root_pid))
    if token:
        pids.update(_token_tagged_pids(token))
    return sum(_rss_bytes(pid) for pid in pids)


def _memory_watchdog(root_pid: int, memory_bytes: int, token: str | None,
                     stop_event: "threading.Event", breach_event: "threading.Event",
                     poll_interval: float = 0.2) -> None:
    """A0-5: portable memory-exhaustion guard. Polls the dispatched process
    tree's total RSS and kills it the moment it crosses `memory_bytes` — see
    the module docstring for why this parent-side watchdog, not
    `RLIMIT_AS`, is the mechanism this module actually relies on for memory
    containment (RLIMIT_AS is empirically unreliable on macOS/Darwin for
    `mmap`-backed allocations)."""
    while not stop_event.is_set():
        if _total_tracked_rss_bytes(root_pid, token) > memory_bytes:
            breach_event.set()
            try:
                os.killpg(os.getpgid(root_pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            _signal_all(_descendant_pids(root_pid), signal.SIGKILL)
            if token:
                _signal_all(_token_tagged_pids(token), signal.SIGKILL)
            return
        stop_event.wait(poll_interval)


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


def sandboxed_env(profile_allowlist, home_dir: Path, token: str | None = None) -> dict:
    """A minimal, deny-by-default environment: fresh temp HOME, no inherited secrets."""
    env = build_task_env(profile_allowlist)
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    env["HOME"] = str(home_dir)
    env["TMPDIR"] = str(home_dir)
    env["LANG"] = os.environ.get("LANG", "C.UTF-8")
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    if token:
        # A0-5: inherited by any well-behaved descendant across fork()/exec()
        # (including one reparented to PID 1 by a double-fork) so the
        # timeout-kill sweep can find it even after the ppid chain breaks.
        env["JOAO_SANDBOX_TOKEN"] = token
    return env


class SandboxResult(dict):
    """Marker subclass so callers can tell a sandboxed run's result apart."""


def run_sandboxed(argv: list[str], *, cwd: Path, timeout: int,
                   profile_allowlist: list[str] | None = None,
                   network: bool = False,
                   extra_read_paths: list[str] | None = None,
                   extra_write_paths: list[str] | None = None,
                   protected: bool = False,
                   preserve_host_environment: bool = False,
                   cpu_seconds: int | None = DEFAULT_CPU_SECONDS,
                   memory_bytes: int | None = DEFAULT_MEMORY_BYTES,
                   max_open_files: int | None = DEFAULT_MAX_OPEN_FILES,
                   max_new_processes: int | None = DEFAULT_MAX_NEW_PROCESSES) -> dict[str, Any]:
    """Run `argv` under sandbox-exec (when available) with a fresh temp HOME,
    a minimal env, network denied unless `network=True`, a bounded timeout,
    OS resource limits (A0-5), and the whole process tree killed on timeout
    (never just the direct child; A0-5 additionally sweeps by environment
    token, not only by ppid — see module docstring).

    `protected=True` (A0-5) fails closed instead of silently degrading to
    `env-only` enforcement when no real kernel sandbox (Seatbelt) is present
    on this host — the caller must be told outright rather than getting a
    weaker boundary than it asked for.

    `preserve_host_environment=True` (A0.2, §13.2/ExecutionBackend): skips the
    fresh-temp-HOME/env-stripping/Seatbelt wrapping entirely and inherits the
    real process environment untouched — needed by callers (e.g. a local
    Codex CLI review) whose own auth/config lives under the real `$HOME` and
    would break under a wiped one. Process-group tracking, the timeout-kill
    sweep, resource rlimits and the RSS watchdog still apply unchanged — this
    flag narrows only the env/HOME/Seatbelt layer, never the lifecycle
    tracking. `protected=True` is refused together with this flag: a
    passthrough-environment dispatch is by definition not a kernel-sandboxed
    one, so claiming `protected` for it would be dishonest.
    """
    if protected and preserve_host_environment:
        return {"ok": False, "argv": argv, "returncode": -1, "enforcement": "refused-incompatible-flags",
                "protected": True, "network": bool(network), "pid": None, "duration_seconds": 0.0,
                "stdout": "", "timed_out": False,
                "stderr": "protected=True and preserve_host_environment=True are mutually exclusive: "
                          "a host-environment passthrough dispatch cannot also claim kernel-sandbox "
                          "protection."}
    if protected and not SANDBOX_EXEC:
        return {"ok": False, "argv": argv, "returncode": -1, "enforcement": "refused-no-kernel-sandbox",
                "protected": True, "network": bool(network), "pid": None, "duration_seconds": 0.0,
                "stdout": "", "timed_out": False,
                "stderr": "A0-5: protected dispatch refused — no real kernel sandbox (macOS Seatbelt "
                          "sandbox-exec) is available on this host; RI-6 no longer silently falls back "
                          "to env-only enforcement for a protected run."}
    with tempfile.TemporaryDirectory(prefix="joao-sandbox-home-") as home:
        home_dir = Path(home)
        token = secrets.token_hex(16)
        write_paths = [str(Path(cwd).resolve()), str(home_dir)] + [str(p) for p in (extra_write_paths or [])]
        full_argv = list(argv)
        if preserve_host_environment:
            # A0.2: no env stripping, no fresh HOME, no Seatbelt — the caller
            # explicitly needs the real host environment (e.g. reviewer CLI
            # auth). Still routed through this single dispatch point so
            # lifecycle tracking (pid/pgid, timeout kill, resource limits,
            # RSS watchdog) is uniform across every subprocess JOAO launches.
            env = None
            enforcement = "host-passthrough-tracked"
        else:
            env = sandboxed_env(profile_allowlist or [], home_dir, token=token)
            enforcement = "env-only"
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
        nproc_ceiling = (_current_uid_process_count() + max_new_processes) if max_new_processes else None
        preexec = _resource_limits_preexec(cpu_seconds, memory_bytes, max_open_files, nproc_ceiling)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                full_argv, cwd=str(cwd), shell=False, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True, preexec_fn=preexec,
            )
        except OSError as exc:
            return {"ok": False, "argv": argv, "returncode": -1, "enforcement": enforcement,
                    "stdout": "", "stderr": f"failed to start: {exc}", "timed_out": False}
        pid = proc.pid
        # A0-5: the RSS watchdog, not RLIMIT_AS, is the mechanism actually
        # relied on for memory containment (see module docstring) — it runs
        # for the whole lifetime of the dispatch, independent of the
        # timeout, so a memory hog that blows up well within `timeout` is
        # still caught.
        stop_event = threading.Event()
        breach_event = threading.Event()
        watchdog = None
        if memory_bytes:
            watchdog = threading.Thread(
                target=_memory_watchdog, args=(pid, memory_bytes, token, stop_event, breach_event),
                daemon=True,
            )
            watchdog.start()
        try:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
                stdout, stderr = _kill_process_group(proc, pid, token)
        finally:
            stop_event.set()
            if watchdog is not None:
                watchdog.join(timeout=2)
        memory_exceeded = breach_event.is_set()
        if memory_exceeded and proc.returncode is None:
            try:
                stdout2, stderr2 = proc.communicate(timeout=2)
                stdout, stderr = (stdout or "") + (stdout2 or ""), (stderr or "") + (stderr2 or "")
            except subprocess.TimeoutExpired:
                pass
        returncode = proc.returncode if proc.returncode is not None else 124
        duration = time.monotonic() - started
        return {
            "ok": returncode == 0 and not timed_out and not memory_exceeded,
            "argv": argv, "returncode": returncode, "enforcement": enforcement,
            "network": bool(network), "pid": pid, "duration_seconds": round(duration, 3),
            "stdout": (stdout or "")[-16000:], "stderr": (stderr or "")[-16000:],
            "timed_out": timed_out, "memory_limit_exceeded": memory_exceeded,
            "resource_limits": {"cpu_seconds": cpu_seconds, "memory_bytes": memory_bytes,
                               "max_open_files": max_open_files, "nproc_ceiling": nproc_ceiling},
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


def _kill_process_group(proc: subprocess.Popen, pid: int, token: str | None = None) -> tuple[str, str]:
    """Kill the whole process tree so a subprocess-spawned child cannot survive
    its parent's timeout — including one that calls `setsid()` to escape the
    original process group (RI-6 attack test 8 — the zombie case) AND
    (A0-5) one that additionally double-forks to get reparented to PID 1,
    which structurally breaks the ppid-walk's chain back to `pid` — caught
    instead by the per-dispatch environment token sweep."""
    descendants = _descendant_pids(pid)
    tagged = _token_tagged_pids(token) if token else []
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    _signal_all(descendants, signal.SIGTERM)
    _signal_all(tagged, signal.SIGTERM)
    try:
        stdout, stderr = proc.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        stdout, stderr = "", ""
    # Re-walk both ways: a descendant may have spawned further children (or
    # double-forked and been reparented to PID 1) between sweeps.
    survivors = _descendant_pids(pid)
    survivors_tagged = _token_tagged_pids(token) if token else []
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    _signal_all(survivors, signal.SIGKILL)
    _signal_all(survivors_tagged, signal.SIGKILL)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    return stdout or "", stderr or ""
