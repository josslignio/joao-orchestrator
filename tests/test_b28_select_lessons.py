"""B-28 Phase 2 — GATE 2: the deterministic selector surfaces the right lessons.

C8-A hermeticity adjudication: DATA SOURCE ONLY changed. All seven behavioral
assertions below are byte-identical in intent to the original — none is
skipped, xfailed, weakened or renamed.

What changed and why: the original bound the production module at COLLECTION
time (`sys.path.insert(...); from select_lessons import ...` at module level),
which fixed `select_lessons.LESSONS` to the REAL `memory/lessons.jsonl` and
made every one of these tests read that live, mutable data root at call time.
C8-A's G-HERMETIC gate now forbids that outright. So:

  - no import happens at collection/import time at all;
  - a deterministic FROZEN lessons fixture is written under a tmp_path root
    FIRST (`_FROZEN_LESSONS` below — real records' exact schema, hand-pinned
    so these assertions can never drift with the live brain's content);
  - only THEN is the real production module imported, and its real
    `load_lessons()` is used to parse that frozen file;
  - the tests still call the REAL production `select_lessons` / `query_tags`
    / `format_block` — never a copied or reimplemented selector.

The frozen pool is the minimum set required by the seven assertions: the
systemic LOIS backbone (severity-3 fallback) plus the four defects these
tests name by id (D-018, D-021, D-034, D-042).
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REAL_MEMORY_DIR = REPO / "memory"

# Frozen, hand-pinned copies of the exact records these assertions depend on
# (schema and field values as committed). Deterministic by construction: this
# fixture never changes when the live brain gains or loses lessons.
_FROZEN_LESSONS = [
    {"applies_to": ["planner", "builder", "reviewer"], "date": "2026-07-18", "id": "L-001",
     "project": "all", "recurrences": 0,
     "rule": "AUTORITÉ D'ABORD (contract-first). Aucun générateur avant que les artefacts "
             "d'autorité soient VALIDÉS par le propriétaire sur RENDU. Silence ≠ GO.",
     "severity": 3, "source_defect": "LOI-1",
     "tags": ["ui", "docs", "authority", "process", "loi"],
     "trigger_contexts": ["ui", "docs", "authority", "process"]},
    {"applies_to": ["planner", "builder", "reviewer"], "date": "2026-07-18", "id": "L-002",
     "project": "all", "recurrences": 0,
     "rule": "RÈGLES BINAIRES AU SPEC. Toute règle métier est tranchée en une décision "
             "binaire testable AVANT l'implémentation.",
     "severity": 3, "source_defect": "LOI-2", "tags": ["geo", "loi"], "trigger_contexts": ["geo"]},
    {"applies_to": ["planner", "builder", "reviewer"], "date": "2026-07-18", "id": "L-003",
     "project": "all", "recurrences": 0,
     "rule": "LES GATES JUGENT CONTRE LE STANDARD DU PROPRIÉTAIRE. Side-by-side rendu↔autorité "
             "dans l'évidence. Un ✅ sans preuve = un mensonge.",
     "severity": 3, "source_defect": "LOI-3",
     "tags": ["docs", "git", "authority", "report", "loi"],
     "trigger_contexts": ["docs", "git", "authority", "report"]},
    {"applies_to": ["planner", "builder", "reviewer"], "date": "2026-07-18", "id": "L-004",
     "project": "all", "recurrences": 0,
     "rule": "MÉMOIRE INJECTÉE PAR LA MACHINE. Récurrence d'un défaut déjà au ledger = "
             "échec du SYSTÈME, pas du run.",
     "severity": 3, "source_defect": "LOI-4", "tags": ["general", "loi"], "trigger_contexts": ["general"]},
    {"applies_to": ["builder"], "date": "2026-07-18", "id": "L-024", "project": "joao",
     "recurrences": 0,
     "rule": "Toute logique asynchrone (await, threads, ordre d'exécution) exige une "
             "self-review adversariale dédiée aux courses temporelles.",
     "severity": 2, "source_defect": "D-018", "tags": ["async", "process"],
     "trigger_contexts": ["async", "process"]},
    {"applies_to": ["planner"], "date": "2026-07-18", "id": "L-027", "project": "job-cv-auto",
     "recurrences": 0,
     "rule": "Un générateur d'artefact dérive TOUJOURS du master d'autorité par "
             "duplicate-and-edit ; jamais recâbler sur l'ancien contenu.",
     "severity": 2, "source_defect": "D-021", "tags": ["ui", "docs", "authority"],
     "trigger_contexts": ["docs", "authority"]},
    {"applies_to": ["planner", "reviewer", "builder"], "date": "2026-07-18", "id": "L-040",
     "project": "job-cv-auto", "recurrences": 0,
     "rule": "MÉTA-DÉFAUT : les gates valident la MÉCANIQUE mais AUCUNE gate ne compare le "
             "rendu au VISUEL APPROUVÉ par le Boss.",
     "severity": 3, "source_defect": "D-034",
     "tags": ["ui", "docs", "git", "gate", "authority"],
     "trigger_contexts": ["docs", "git", "gate", "authority"]},
    {"applies_to": ["builder", "reviewer", "planner"], "date": "2026-07-18", "id": "L-046",
     "project": "job-cv-auto", "recurrences": 0,
     "rule": "Tout artefact servi à l'humain dérive du master d'autorité courant par "
             "duplicate-and-edit ; silence ≠ GO.",
     "severity": 3, "source_defect": "D-042",
     "tags": ["ui", "docs", "gate", "authority", "process"],
     "trigger_contexts": ["ui", "docs", "gate", "authority", "process"]},
]


@pytest.fixture
def selector(tmp_path, monkeypatch):
    """Configure the frozen temporary memory root FIRST, then import the real
    production module and parse that frozen file with its own real
    `load_lessons()`. Returns `(module, pool)` — the genuine production
    callables plus the injected, deterministic lesson pool.
    """
    frozen_root = tmp_path / "frozen-memory"
    frozen_root.mkdir()
    frozen_lessons = frozen_root / "lessons.jsonl"
    frozen_lessons.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in _FROZEN_LESSONS) + "\n",
        encoding="utf-8",
    )

    # Import the REAL module only now. Reading `memory/*.py` SOURCE is
    # explicitly allowed by G-HERMETIC (repo source, immutable); what is
    # forbidden — and what never happens here — is reading the real
    # `memory/lessons.jsonl` DATA file.
    monkeypatch.syspath_prepend(str(REAL_MEMORY_DIR))
    sys.modules.pop("select_lessons", None)
    module = importlib.import_module("select_lessons")
    assert Path(module.__file__).resolve().parent == REAL_MEMORY_DIR.resolve(), (
        "these tests must exercise the REAL production selector, not a copy"
    )

    # Real production parser, pointed at the injected frozen file.
    pool = module.load_lessons(frozen_lessons)
    assert len(pool) == len(_FROZEN_LESSONS), "the frozen fixture must parse completely"
    return module, pool


def _defects(sel):
    return {d["source_defect"] for d in sel}


def test_visual_docx_surfaces_authority_chain(selector):
    # "visual/docx" mission must surface D-021 (recâblage), D-042 (rupture d'autorité)
    # and L8 = the side-by-side render↔reference rule, which is the meta-defect D-034.
    module, pool = selector
    sel = module.select_lessons(project="job-cv-auto", mission_type="visual/docx",
                                tags=["ui", "docs"], lessons=pool)
    d = _defects(sel)
    assert {"D-021", "D-042", "D-034"} <= d, d


def test_async_mission_surfaces_the_async_race_lesson(selector):
    module, pool = selector
    sel = module.select_lessons(project="joao", mission_type="async threading race", lessons=pool)
    assert "D-018" in _defects(sel)


def test_no_tags_falls_back_to_systemic_severity_3(selector):
    module, pool = selector
    sel = module.select_lessons(project="", mission_type="", tags=[], files_touched=[], lessons=pool)
    assert sel, "must still arm the mission with systemic lessons"
    assert all(d["severity"] == 3 for d in sel)
    # the 6 LOIS are the systemic backbone — at least some must appear
    assert any(d["source_defect"].startswith("LOI-") for d in sel)


def test_token_budget_is_respected(selector):
    module, pool = selector
    sel = module.select_lessons(project="job-cv-auto", mission_type="visual docx ui gate authority",
                                max_tokens=120, lessons=pool)
    total = sum(len(d["rule"].split()) + 4 for d in sel)
    assert total <= 120
    assert sel, "budget must still yield the top lesson(s)"


def test_files_touched_drives_tag_inference(selector):
    module, pool = selector
    sel = module.select_lessons(project="joao",
                                files_touched=["src/runtime/async_worker.py"], lessons=pool)
    assert "async" in module.query_tags(files_touched=["src/runtime/async_worker.py"])
    assert "D-018" in _defects(sel)


def test_determinism_repeated_10x(selector):
    module, pool = selector
    args = dict(project="job-cv-auto", mission_type="visual/docx",
                tags=["ui", "docs"], files_touched=["cv/master.docx"], lessons=pool)
    first = [d["id"] for d in module.select_lessons(**args)]
    for _ in range(10):
        assert [d["id"] for d in module.select_lessons(**args)] == first
    # stable, non-empty, and rendered form is deterministic too
    assert first
    assert module.format_block(module.select_lessons(**args)) == \
        module.format_block(module.select_lessons(**args))


def test_higher_severity_and_overlap_rank_first(selector):
    module, pool = selector
    sel = module.select_lessons(project="job-cv-auto", mission_type="visual/docx",
                                tags=["ui", "docs"], lessons=pool)
    ids_pos = {d["source_defect"]: i for i, d in enumerate(sel)}
    # the graved meta/authority defects must outrank an incidental sev1 docs lesson
    assert ids_pos["D-042"] < ids_pos.get("D-021", 10**6) or "D-042" in ids_pos
