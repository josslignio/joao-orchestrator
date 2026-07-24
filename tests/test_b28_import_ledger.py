"""B-28 Phase 1 — the ledger → lessons.jsonl import (stock)."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
IMPORT = REPO / "memory" / "import_ledger.py"

sys.path.insert(0, str(REPO / "memory"))
import import_ledger  # noqa: E402

REQUIRED = {"id", "date", "project", "source_defect", "tags", "severity",
            "rule", "applies_to", "trigger_contexts", "recurrences"}


def _load(lessons_path: Path) -> list[dict]:
    return [json.loads(l) for l in lessons_path.read_text().splitlines() if l.strip()]


@pytest.fixture
def isolated_lessons(_isolated_joao_memory_dir) -> Path:
    """C8-A correction / G-HERMETIC: every assertion in this file now reads
    the SAME injected, isolated `lessons.jsonl` copy every other RunRuntime-
    mediated test already uses (`tests/conftest.py::_isolated_joao_memory_
    dir`, a byte-identical snapshot of the real, committed stock taken once
    at session start) — never the live, uninjected repo `memory/
    lessons.jsonl` directly. This validates the same real, committed content
    (the snapshot is taken before any test can mutate anything) without any
    test in this file resolving an `open()` against the real, uninjected
    root — `G_HERMETIC_REAL_MEMORY_TOUCHED` no longer has a blanket
    exemption for this file's read side."""
    path = _isolated_joao_memory_dir / "lessons.jsonl"
    assert path.is_file(), "isolated memory fixture did not seed lessons.jsonl"
    return path


def test_at_least_40_lessons(isolated_lessons):
    assert len(_load(isolated_lessons)) >= 40


def test_schema_complete_and_typed(isolated_lessons):
    for d in _load(isolated_lessons):
        assert REQUIRED <= set(d), f"missing fields in {d.get('id')}"
        assert isinstance(d["tags"], list) and d["tags"]
        assert d["severity"] in (1, 2, 3)
        assert set(d["applies_to"]) <= {"planner", "builder", "reviewer"} and d["applies_to"]
        assert d["rule"].strip()
        # rule must be an imperative/lesson, not a bare root-cause label
        assert d["rule"].strip().upper() not in ("ENVIRONNEMENT", "PROCESS", "CODE", "MOTEUR", "REVIEW", "GATE")


def test_system_ids_and_defect_mapping(isolated_lessons):
    lessons = _load(isolated_lessons)
    ids = [d["id"] for d in lessons]
    assert all(i.startswith("L-") for i in ids)
    assert len(set(ids)) == len(ids), "ids must be unique"
    # every ledger defect maps to exactly one lesson via source_defect
    defects = [d["source_defect"] for d in lessons]
    assert len(set(defects)) == len(defects)
    assert any(d.startswith("D-") for d in defects) and any(d.startswith("LOI-") for d in defects)


def test_selector_critical_tags_present(isolated_lessons):
    L = {d["source_defect"]: d for d in _load(isolated_lessons)}
    assert "async" in L["D-018"]["tags"]                    # async mission -> D-018
    assert "docs" in L["D-021"]["tags"]                     # visual/docx -> D-021
    assert {"docs", "authority"} <= set(L["D-042"]["tags"])  # authority chain -> D-042
    assert all(l.get("severity") == 3 for k, l in L.items() if k.startswith("LOI-"))  # laws are sev3


def test_import_is_append_only_idempotent(tmp_path, _isolated_joao_memory_dir):
    """A0.2 §7 / C8-A correction (G-HERMETIC): this test used to shell out to
    `memory/import_ledger.py` with NO arguments, which re-read the real
    `~/Claude-HQ/DEFECTS_LEDGER.md` (a file entirely outside this repo and
    this test's control) and could write straight into the real, committed
    `memory/lessons.jsonl` — the actual mechanism behind the "flaky
    depending on lessons.jsonl content" group documented in the A0/A0.1
    reports. It was then made hermetic on its WRITE side only (a throwaway
    `lessons_copy`), keeping a blanket read-side dependency on the real
    external ledger AND a direct read of the real `LESSONS` file, carved out
    of G-HERMETIC via a node-id exemption.

    That exemption is removed here. This version:
      - seeds `lessons_copy` from the ALREADY-INJECTED isolated memory
        snapshot (`_isolated_joao_memory_dir`), never the live real file;
      - imports from a fully synthetic, frozen `DEFECTS_LEDGER.md` under
        `tmp_path` (a single demo defect, `D-995`, chosen well outside the
        real ledger's id range so it can never collide with real content),
        never `~/Claude-HQ`;
      - passes `analysis_path=None` (no real `JOAO_ANALYSE_SYSTEMIQUE...`
        dependency either — the 6 LOIS are irrelevant to the idempotency
        invariant this test actually checks);
      - never opens the real, uninjected `memory/` root or `~/Claude-HQ` at
        all — no skip, no xfail.

    The invariant under test: running the import a SECOND time against the
    now-updated copy is a true no-op (adds nothing, rewrites nothing) —
    self-contained, deterministic, independent of any live external content.
    """
    isolated_original = _isolated_joao_memory_dir / "lessons.jsonl"
    isolated_before = isolated_original.read_bytes()
    lessons_copy = tmp_path / "lessons.jsonl"
    shutil.copy2(isolated_original, lessons_copy)

    frozen_ledger = tmp_path / "DEFECTS_LEDGER.md"
    frozen_ledger.write_text(
        "| ID | Produit | Symptôme | Cause racine | Leçon | Statut |\n|---|---|---|---|---|---|\n"
        "| D-995 | demo | frozen synthetic demo defect for idempotency test | **CODE** | "
        "run the import twice, nothing new the second time | FIXÉ |\n"
    )

    first = import_ledger.import_lessons(frozen_ledger, lessons_copy, analysis_path=None)
    assert first.get("error") is None
    assert first.get("added", 0) >= 1, "the frozen ledger's D-995 must be a genuinely new lesson on the first import"
    after_first = lessons_copy.read_text()

    second = import_ledger.import_lessons(frozen_ledger, lessons_copy, analysis_path=None)
    assert lessons_copy.read_text() == after_first, "a second import must not rewrite existing lessons"
    assert second.get("added", 0) == 0, f"expected 0 new lessons on a second import, got {second}"

    # Neither the real repo lessons.jsonl nor the isolated snapshot's own
    # on-disk copy under _isolated_joao_memory_dir is ever a write target of
    # this test — only the throwaway tmp_path copy is mutated.
    assert isolated_original.read_bytes() == isolated_before
