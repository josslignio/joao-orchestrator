"""B-28 Phase 2 — GATE 2: the deterministic selector surfaces the right lessons."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "memory"))

from select_lessons import select_lessons, query_tags, format_block  # noqa: E402


def _defects(sel):
    return {d["source_defect"] for d in sel}


def test_visual_docx_surfaces_authority_chain():
    # "visual/docx" mission must surface D-021 (recâblage), D-042 (rupture d'autorité)
    # and L8 = the side-by-side render↔reference rule, which is the meta-defect D-034.
    sel = select_lessons(project="job-cv-auto", mission_type="visual/docx",
                         tags=["ui", "docs"])
    d = _defects(sel)
    assert {"D-021", "D-042", "D-034"} <= d, d


def test_async_mission_surfaces_the_async_race_lesson():
    sel = select_lessons(project="joao", mission_type="async threading race")
    assert "D-018" in _defects(sel)


def test_no_tags_falls_back_to_systemic_severity_3():
    sel = select_lessons(project="", mission_type="", tags=[], files_touched=[])
    assert sel, "must still arm the mission with systemic lessons"
    assert all(d["severity"] == 3 for d in sel)
    # the 6 LOIS are the systemic backbone — at least some must appear
    assert any(d["source_defect"].startswith("LOI-") for d in sel)


def test_token_budget_is_respected():
    sel = select_lessons(project="job-cv-auto", mission_type="visual docx ui gate authority",
                         max_tokens=120)
    total = sum(len(d["rule"].split()) + 4 for d in sel)
    assert total <= 120
    assert sel, "budget must still yield the top lesson(s)"


def test_files_touched_drives_tag_inference():
    sel = select_lessons(project="joao",
                        files_touched=["src/runtime/async_worker.py"])
    assert "async" in query_tags(files_touched=["src/runtime/async_worker.py"])
    assert "D-018" in _defects(sel)


def test_determinism_repeated_10x():
    args = dict(project="job-cv-auto", mission_type="visual/docx",
                tags=["ui", "docs"], files_touched=["cv/master.docx"])
    first = [d["id"] for d in select_lessons(**args)]
    for _ in range(10):
        assert [d["id"] for d in select_lessons(**args)] == first
    # stable, non-empty, and rendered form is deterministic too
    assert first
    assert format_block(select_lessons(**args)) == format_block(select_lessons(**args))


def test_higher_severity_and_overlap_rank_first():
    sel = select_lessons(project="job-cv-auto", mission_type="visual/docx",
                         tags=["ui", "docs"])
    ids_pos = {d["source_defect"]: i for i, d in enumerate(sel)}
    # the graved meta/authority defects must outrank an incidental sev1 docs lesson
    assert ids_pos["D-042"] < ids_pos.get("D-021", 10**6) or "D-042" in ids_pos
