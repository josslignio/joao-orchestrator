"""B-28 Phase 4 — GATE 4: retro loop, recurrence detection, runs_until_perfect.

Hermetic: every write path is redirected into tmp_path so the committed stock is untouched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "memory"))
import retro  # noqa: E402

SEED = [
    {"id": "L-001", "date": "2026-07-18", "project": "all", "source_defect": "LOI-1",
     "rule": "AUTORITÉ D'ABORD : aucun artefact avant validation du propriétaire sur rendu.",
     "tags": ["authority", "docs"], "severity": 3, "applies_to": ["planner", "builder", "reviewer"],
     "trigger_contexts": ["authority"], "recurrences": 0},
    {"id": "L-024", "date": "2026-07-18", "project": "joao", "source_defect": "D-018",
     "rule": "Toute logique asynchrone exige une self-review adversariale dédiée aux courses temporelles.",
     "tags": ["async"], "severity": 2, "applies_to": ["builder"],
     "trigger_contexts": ["async"], "recurrences": 0},
]


@pytest.fixture
def mem(tmp_path, monkeypatch):
    lessons = tmp_path / "lessons.jsonl"
    lessons.write_text("\n".join(json.dumps(x, ensure_ascii=False, sort_keys=True) for x in SEED) + "\n")
    monkeypatch.setattr(retro, "LESSONS", lessons)
    monkeypatch.setattr(retro, "RECURRENCES", tmp_path / "recurrences.jsonl")
    monkeypatch.setattr(retro, "METRICS", tmp_path / "run_metrics.jsonl")
    monkeypatch.setattr(retro, "PROJECTS", tmp_path / "projects")
    return tmp_path


def test_new_candidate_gets_system_id_and_is_appended(mem):
    before = mem.joinpath("lessons.jsonl").read_text()
    out = retro.ingest_candidates(
        [{"rule": "Les credentials ne vivent jamais dans le dépôt ni dans les logs.",
          "tags": ["git"], "severity": 3, "root_cause": "PROCESS"}],
        project="job-cv-auto", run_id="run-x", date="2026-07-18")
    assert len(out["added"]) == 1
    lesson = out["added"][0]
    assert lesson["id"] == "L-025"  # system-assigned, next free
    assert lesson["applies_to"]  # derived from root_cause PROCESS
    after = mem.joinpath("lessons.jsonl").read_text()
    assert after.startswith(before), "append-only: existing lines must be untouched"
    assert "L-025" in after


def test_recurrence_is_detected_no_new_lesson_and_alert_raised(mem):
    before_lines = len(mem.joinpath("lessons.jsonl").read_text().splitlines())
    out = retro.ingest_candidates(
        [{"rule": "La logique asynchrone doit passer une self-review adversariale sur les courses temporelles.",
          "tags": ["async"], "severity": 2}],
        project="joao", run_id="run-recur", date="2026-07-18")
    assert out["added"] == [], "a recurring defect must NOT create a new lesson"
    assert len(out["recurrence_alerts"]) == 1
    alert = out["recurrence_alerts"][0]
    assert alert["level"] == "🔴"
    assert alert["lesson_id"] == "L-024"
    assert "récidive" in alert["message"].lower()
    assert "système d'injection" in alert["message"].lower()
    after_lines = len(mem.joinpath("lessons.jsonl").read_text().splitlines())
    assert after_lines == before_lines, "append-only stock unchanged on recurrence"


def test_recurrence_count_accumulates_via_overlay(mem):
    for _ in range(3):
        retro.ingest_candidates(
            [{"rule": "logique asynchrone self-review adversariale courses temporelles", "tags": ["async"]}],
            project="joao", run_id="run-n", date="2026-07-18")
    assert retro.recurrence_counts()["L-024"] == 3
    lesson = next(d for d in retro.load_lessons() if d["id"] == "L-024")
    assert retro.effective_recurrences(lesson) == 3  # baseline 0 + 3 overlay


def test_source_defect_match_is_a_recurrence(mem):
    out = retro.ingest_candidates(
        [{"rule": "completely unrelated wording here", "source_defect": "D-018"}],
        project="joao", run_id="r", date="2026-07-18")
    assert out["added"] == []
    assert out["recurrence_alerts"][0]["lesson_id"] == "L-024"


def test_runs_until_perfect(mem):
    retro.record_run_metric("p", "r1", perfect=False)
    retro.record_run_metric("p", "r2", perfect=False)
    assert retro.runs_until_perfect("p") == 2
    retro.record_run_metric("p", "r3", perfect=True)
    assert retro.runs_until_perfect("p") == 0
    retro.record_run_metric("p", "r4", perfect=False)
    assert retro.runs_until_perfect("p") == 1


def test_close_mission_e2e_recurrence_triggers_red_alert_and_memory(mem):
    # GATE 4: a SIMULATED recurrence must raise the 🔴 alert end-to-end.
    outcome = retro.close_mission(
        "joao", "run-e2e",
        [{"rule": "La logique asynchrone doit avoir une self-review adversariale sur les courses temporelles.",
          "tags": ["async"]}],
        perfect=True, date="2026-07-18")
    assert outcome.recurrence_alerts, "recurrence must surface"
    assert outcome.recurrence_alerts[0]["level"] == "🔴"
    pm = (mem / "projects" / "joao" / "PROJECT_MEMORY.md").read_text()
    assert "🔴" in pm and "RÉCIDIVE" in pm.upper()
    # a run with a recurrence is NOT perfect
    metrics = [json.loads(x) for x in (mem / "run_metrics.jsonl").read_text().splitlines() if x.strip()]
    assert metrics[-1]["perfect"] is False


def test_retro_template_has_taxonomy_and_candidate_section(mem):
    text = retro.render_retro_template("joao", "run-t", "toy mission", spec="x", result="y")
    for cause in retro.TAXONOMY:
        assert cause in text
    assert "SANS id" in text
    assert "runs_until_perfect" in text
