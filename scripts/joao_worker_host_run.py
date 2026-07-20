#!/usr/bin/env python3
"""Entry point launched by the `com.joao.worker-host` LaunchAgent (or
manually, for local testing). This is the standalone sibling worker-host
process (Boss architecture decision, 2026-07-20) — it is never invoked BY,
and never invokes, a Claude Code / model session. Runs no product mission
itself: it only ever listens on its Unix socket and dispatches whatever
structured requests JOAO's controller sends it.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

LOG_DIR = Path("~/Library/Logs/joao").expanduser()
_MAX_LOG_BYTES = 10 * 1024 * 1024  # bounded logs — one rotated backup, never unbounded growth


def _bound_log(path: Path) -> None:
    if path.exists() and path.stat().st_size > _MAX_LOG_BYTES:
        rotated = path.with_name(path.name + ".1")
        rotated.unlink(missing_ok=True)
        path.rename(rotated)


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _bound_log(LOG_DIR / "worker-host.log")
    _bound_log(LOG_DIR / "worker-host.err.log")
    from src.joao_orchestrator.worker_host.server import serve_forever
    serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
