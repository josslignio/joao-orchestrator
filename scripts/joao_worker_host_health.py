#!/usr/bin/env python3
"""Standalone health probe for `joao-worker-host` — never dispatches a
builder/reviewer, never consumes a request_id from the duplicate-detection
ledger; just proves the socket is up and answering. Exit 0 on healthy, 1
otherwise. Used by `scripts/joao_worker_host_bootstrap.sh` and callable
directly (`python3 scripts/joao_worker_host_health.py`).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.joao_orchestrator.worker_host.client import health_check  # noqa: E402
from src.joao_orchestrator.worker_host.server import default_socket_path  # noqa: E402


def main(argv: list[str]) -> int:
    socket_path = Path(argv[0]).expanduser() if argv else default_socket_path()
    result = health_check(socket_path=socket_path)
    print(json.dumps(result))
    return 0 if (result.get("ok") and result.get("pong")) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
