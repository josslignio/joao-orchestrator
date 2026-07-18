#!/usr/bin/env python3
"""M0 — build SPEC_INDEX_V4.json, the machine index of active V4 documents (C-1).

A document is "active" iff it is listed here, with a REAL sha256 computed at generation
time (never hand-typed — D-043: a claim must have exactly the scope of its proof). The
loader (`governance/spec_loader.py::load_active_specs`) refuses anything not in this index.

Run: `python3 scripts/build_spec_index.py` from the repo root. Deterministic given the
same file contents (no clock/random in the hash computation itself; `generated_at` is the
only timestamp field and is NOT part of what the loader verifies).
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VERSION = "4.0.0"

# (path relative to repo root, doc type) — the CLOSED list of active V4 authorities (C-1).
ACTIVE_DOCUMENTS = [
    ("SYSTEM_CONSTITUTION_V4.md", "constitution"),
    ("specs/runtime_integrity.yaml", "spec"),
    ("specs/security.yaml", "spec"),
    ("specs/memory.yaml", "spec"),
    ("specs/phase0.yaml", "spec"),
    ("specs/provider_cascade.yaml", "spec"),
    ("specs/cv_bot.yaml", "spec"),
    ("specs/product_ui.yaml", "spec"),
    ("specs/operations.yaml", "spec"),
    ("ROADMAP_V4.md", "roadmap"),
    ("TRACEABILITY_V4.jsonl", "traceability"),
    ("DECISION_LOG.jsonl", "decision_log"),
]


def sha256_of(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1048576), b""):
            value.update(block)
    return value.hexdigest()


def build_index(repo: Path = REPO, *, at: str | None = None) -> dict:
    documents = []
    missing = []
    for rel, doc_type in ACTIVE_DOCUMENTS:
        path = repo / rel
        if not path.is_file():
            missing.append(rel)
            continue
        documents.append({
            "path": rel,
            "type": doc_type,
            "version": VERSION,
            "sha256": sha256_of(path),
            "bytes": path.stat().st_size,
        })
    if missing:
        raise FileNotFoundError(f"SPEC_INDEX_V4 build refused: missing active documents: {missing}")
    return {
        "schema_version": 1,
        "version": VERSION,
        "generated_at": at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "runtime_authority": False,  # flips to true only on an explicit Boss GO (C-2) — never self-declared
        "active_documents": documents,
    }


def main() -> int:
    index = build_index()
    out = REPO / "SPEC_INDEX_V4.json"
    out.write_text(json.dumps(index, indent=2, sort_keys=False, ensure_ascii=False) + "\n")
    print(f"wrote {out} — {len(index['active_documents'])} active documents, real sha256 computed")
    for doc in index["active_documents"]:
        print(f"  {doc['path']:40s} {doc['sha256'][:16]}… ({doc['bytes']} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
