"""Append-only event log reader/writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from ..storage.atomic import append_line


def write_event(events_path: Path, event: dict) -> None:
    """Append one event dict as a JSONL line."""
    append_line(events_path, json.dumps(event, sort_keys=True))


def read_events(events_path: Path) -> List[dict]:
    """Read all events from a JSONL file (oldest first). Tolerant of bad lines."""
    if not events_path.is_file():
        return []
    out = []
    with open(events_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
