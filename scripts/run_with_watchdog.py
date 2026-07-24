#!/usr/bin/env python3
"""Run a command in its own process group with a hard timeout and signal cleanup."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from types import FrameType


def terminate_process_group(proc: subprocess.Popen, *, grace_seconds: float = 2.0) -> None:
    """Terminate the full child process group, escalating to SIGKILL.

    Order: SIGTERM → grace → reap leader (proc.wait) → SIGKILL survivors.
    Reaping the leader before the final SIGKILL prevents zombie-blocked
    killpg checks from masking surviving grandchildren.
    """
    pgid = proc.pid
    # SIGTERM the whole group
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # Grace period for graceful shutdown
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    # SIGKILL the leader first so we can reap it
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # Reap the leader BEFORE any further group checks
    try:
        proc.wait(timeout=max(0.1, grace_seconds))
    except (subprocess.TimeoutExpired, ProcessLookupError):
        pass
    # Now SIGKILL any grandchildren that survived the leader
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        raise SystemExit("missing command")
    if args.seconds <= 0:
        raise SystemExit("--seconds must be positive")

    proc = subprocess.Popen(command, start_new_session=True)
    received_signal: int | None = None

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        nonlocal received_signal
        received_signal = signum
        # Signal handler must be non-blocking — just SIGKILL the group
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous[sig] = signal.signal(sig, handle_signal)

    try:
        deadline = time.monotonic() + args.seconds
        while proc.poll() is None and time.monotonic() < deadline and received_signal is None:
            time.sleep(0.25)
        if received_signal is not None:
            print(f"RUNNIGHT_WATCHDOG_SIGNAL={received_signal}", file=sys.stderr)
            return 128 + received_signal
        if proc.poll() is None:
            terminate_process_group(proc)
            print("RUNNIGHT_HARD_TIMEOUT", file=sys.stderr)
            return 124
        return int(proc.returncode)
    finally:
        if proc.poll() is None:
            terminate_process_group(proc)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
