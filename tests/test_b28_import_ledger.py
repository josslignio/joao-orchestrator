"""B-28 Phase 1 — the ledger → lessons.jsonl import (stock)."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LESSONS = REPO / "memory" / "lessons.jsonl"
IMPORT = REPO / "memory" / "import_ledger.py"

sys.path.insert(0, str(REPO / "memory"))
import import_ledger  # noqa: E402

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


def test_import_is_append_only_idempotent(tmp_path):
    """A0.2 §7 (memory isolation): this test used to shell out to
    `memory/import_ledger.py` with NO arguments, which re-reads
    `~/Claude-HQ/DEFECTS_LEDGER.md` (a file entirely outside this repo and
    this test's control) and writes straight into the real, committed
    `memory/lessons.jsonl` whenever that external ledger has changed since
    the last import — the actual mechanism behind the "flaky depending on
    lessons.jsonl content" group documented in the A0/A0.1 reports (worse: if
    the external ledger ever has genuinely new, not-yet-synced content — as
    observed live during the A0.2 pass — the old version of this test would
    silently rewrite the committed stock AND still fail its own assertion,
    since `before` was captured pre-import).

    This version never touches the real repo file and never asserts
    anything about whatever the external ledger's CURRENT content happens to
    be (that is content/product drift, not a JOAO control-plane property).
    It tests the actual invariant — running the import a SECOND time changes
    nothing — self-containedly: import once into a throwaway copy (whatever
    that produces), then import again into the now-updated copy and assert
    that second run is a true no-op."""
    lessons_copy = tmp_path / "lessons.jsonl"
    real_before = LESSONS.read_bytes()
    shutil.copy2(LESSONS, lessons_copy)
    import_ledger.import_lessons(import_ledger.LEDGER, lessons_copy, import_ledger.ANALYSIS)  # first run: may add
    after_first = lessons_copy.read_text()
    result = import_ledger.import_lessons(import_ledger.LEDGER, lessons_copy, import_ledger.ANALYSIS)  # second: must not
    assert lessons_copy.read_text() == after_first, "a second import must not rewrite existing lessons"
    assert result.get("added", 0) == 0, f"expected 0 new lessons on a second import, got {result}"
    # The real repo file is never a write target of this test — read once,
    # for the copy above, and never touched again.
    assert LESSONS.read_bytes() == real_before, "this test must never write the real repo lessons.jsonl"
