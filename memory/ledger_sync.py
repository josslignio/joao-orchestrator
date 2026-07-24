#!/usr/bin/env python3
"""B-37 — keep the brain current: sync DEFECTS_LEDGER.md → lessons.jsonl on staleness.

A mission launch should never inject a stale brain. This hook compares the ledger's mtime to
the last recorded import; if the ledger is newer (the Boss edited it since), it re-runs the
append-only import BEFORE the injection, then stamps a marker. Deterministic, idempotent, and
a no-op when nothing changed — so it is cheap enough to call on every launch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_ledger  # noqa: E402

MEM = Path(__file__).resolve().parent
DEFAULT_LEDGER = Path.home() / "Claude-HQ" / "DEFECTS_LEDGER.md"
DEFAULT_ANALYSIS = Path.home() / "Claude-HQ" / "JOAO_ANALYSE_SYSTEMIQUE_POURQUOI_CA_LIVRE_PAS.md"
DEFAULT_LESSONS = MEM / "lessons.jsonl"


def sync_if_stale(*, ledger: Path = DEFAULT_LEDGER, lessons: Path = DEFAULT_LESSONS,
                  analysis: Path | None = DEFAULT_ANALYSIS, marker: Path) -> dict:
    """Re-import iff `ledger` is newer than the last recorded import. Returns a status dict."""
    ledger, lessons, marker = Path(ledger), Path(lessons), Path(marker)
    if not ledger.is_file():
        return {"synced": False, "reason": "ledger absent"}
    ledger_mtime = ledger.stat().st_mtime
    last = 0.0
    if marker.is_file():
        try:
            last = float(json.loads(marker.read_text()).get("ledger_mtime", 0.0))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            last = 0.0
    if ledger_mtime <= last:
        return {"synced": False, "reason": "up-to-date", "ledger_mtime": ledger_mtime}
    result = import_ledger.import_lessons(ledger, lessons, analysis)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"ledger_mtime": ledger_mtime, "result": result}, ensure_ascii=False))
    return {"synced": True, "ledger_mtime": ledger_mtime, **result}
