"""Event domain helpers: typed event construction for the append-only log."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_event(task_id: str, from_state: Optional[str], to_state: str,
               reason: str = "", schema_version: int = 1, **extra) -> dict:
    """Construct a timestamped event dict for events.jsonl."""
    ev = {
        "ts": now_iso(),
        "task_id": task_id,
        "from": from_state,
        "to": to_state,
        "reason": reason,
        "schema_version": schema_version,
    }
    if extra:
        ev["extra"] = extra
    return ev
