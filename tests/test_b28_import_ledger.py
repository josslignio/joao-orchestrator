"""B-28 Phase 1 — the ledger → lessons.jsonl import (stock)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LESSONS = REPO / "memory" / "lessons.jsonl"
IMPORT = REPO / "memory" / "import_ledger.py"

REQUIRED = {"id", "date", "project", "source_defect", "tags", "severity",
            "rule", "applies_to", "trigger_contexts", "recurrences"}


def _load():
    return [json.loads(l) for l in LESSONS.read_text().splitlines() if l.strip()]


def test_at_least_40_lessons():
    assert LESSONS.is_file(), "run memory/import_ledger.py first"
    assert len(_load()) >= 40


def test_schema_complete_and_typed():
    for d in _load():
        assert REQUIRED <= set(d), f"missing fields in {d.get('id')}"
        assert isinstance(d["tags"], list) and d["tags"]
        assert d["severity"] in (1, 2, 3)
        assert set(d["applies_to"]) <= {"planner", "builder", "reviewer"} and d["applies_to"]
        assert d["rule"].strip()
        # rule must be an imperative/lesson, not a bare root-cause label
        assert d["rule"].strip().upper() not in ("ENVIRONNEMENT", "PROCESS", "CODE", "MOTEUR", "REVIEW", "GATE")


def test_system_ids_and_defect_mapping():
    lessons = _load()
    ids = [d["id"] for d in lessons]
    assert all(i.startswith("L-") for i in ids)
    assert len(set(ids)) == len(ids), "ids must be unique"
    # every ledger defect maps to exactly one lesson via source_defect
    defects = [d["source_defect"] for d in lessons]
    assert len(set(defects)) == len(defects)
    assert any(d.startswith("D-") for d in defects) and any(d.startswith("LOI-") for d in defects)


def test_selector_critical_tags_present():
    L = {d["source_defect"]: d for d in _load()}
    assert "async" in L["D-018"]["tags"]                    # async mission -> D-018
    assert "docs" in L["D-021"]["tags"]                     # visual/docx -> D-021
    assert {"docs", "authority"} <= set(L["D-042"]["tags"])  # authority chain -> D-042
    assert all(l.get("severity") == 3 for k, l in L.items() if k.startswith("LOI-"))  # laws are sev3


def test_import_is_append_only_idempotent():
    before = LESSONS.read_text()
    r = subprocess.run([sys.executable, str(IMPORT)], capture_output=True, text=True)
    assert r.returncode == 0
    assert LESSONS.read_text() == before, "re-import must not rewrite existing lessons"
    assert "imported 0 new" in r.stdout
