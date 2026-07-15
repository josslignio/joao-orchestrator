"""REN-0 — idempotent product rename event log.

Appends the PRODUCT_RENAMED event exactly once. Idempotency rule: if an event
with the exact identity transition (from_display/to_display/from_technical/
to_technical) already exists, verify its fields and do NOT append another.
A duplicate PRODUCT_RENAMED event is a failing test (enforced in
test_ren0_idempotency.py).

Stdlib only; append-only via storage.atomic.append_line; no rewrite of history.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EVENT_LOG = ROOT / "program" / "joao" / "product_events.jsonl"


def append_product_renamed() -> dict:
    """Append the PRODUCT_RENAMED event idempotently. Returns the decision dict."""
    event = {
        "event": "PRODUCT_RENAMED",
        "from_display": "JOSS",
        "to_display": "JOÃO.AI",
        "from_technical": "joss",
        "to_technical": "joao",
        "migration_status": "DECIDED",
        "historical_artifacts_rewritten": False,
    }
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if EVENT_LOG.exists():
        for line in EVENT_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                existing.append(json.loads(line))
    # Idempotency: match on the exact identity transition.
    transition = (event["from_display"], event["to_display"],
                  event["from_technical"], event["to_technical"])
    for e in existing:
        et = (e.get("from_display"), e.get("to_display"),
              e.get("from_technical"), e.get("to_technical"))
        if et == transition and e.get("event") == "PRODUCT_RENAMED":
            # Already present — verify fields match, do not append again.
            assert e == event, (
                f"existing PRODUCT_RENAMED event fields mismatch: {e} != {event}")
            return {"appended": False, "event": e, "reason": "already_present"}
    # Append exactly one.
    with open(EVENT_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
    return {"appended": True, "event": event, "reason": "appended"}


def count_product_renamed() -> int:
    """Count PRODUCT_RENAMED events with this exact transition (must be 1)."""
    if not EVENT_LOG.exists():
        return 0
    n = 0
    for line in EVENT_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        e = json.loads(line)
        if (e.get("event") == "PRODUCT_RENAMED"
                and e.get("from_display") == "JOSS"
                and e.get("to_display") == "JOÃO.AI"):
            n += 1
    return n


__all__ = ["append_product_renamed", "count_product_renamed", "EVENT_LOG"]
