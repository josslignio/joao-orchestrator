"""V1.3 — F3/F5/F6/F9: narration, mission on card, ETA, timeline."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_provider_routing import runtime  # noqa: E402
from test_result_persistence import OneRepairLoopReviewer, http  # noqa: E402

from joao_orchestrator.bubble.api import LocalAPIServer  # noqa: E402


MISSION = "Ajoute une fonction slugify avec ses tests unitaires."


def finished_run(api, review_mode="claude"):
    launched = http(api, "quick-missions", {"mission": MISSION,
                                            "builder_name": "glm", "review_mode": review_mode})
    api.workers[launched["run_id"]].join(timeout=30)
    return launched["run_id"]


def test_card_data_carries_the_user_mission_and_narration(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        run_id = finished_run(api)
        run = http(api, f"runs/{run_id}")
        # F5: the user's words, not the sandbox contract preamble.
        assert run["mission_display"] == MISSION
        assert "disposable Git sandbox" not in run["mission_display"]
        listed = http(api, "runs")["runs"][0]
        assert listed["mission_excerpt"] == MISSION
        # F3/F6: human-language narration with the review verdict at decision time.
        assert run["status"] == "needs_approval"
        assert run["narration"].startswith("Résultat prêt")
        assert "ACCEPT" in run["narration"]
        assert "à toi de décider" in run["narration"]
        assert run["step_elapsed_seconds"] == 0 or run["status"] not in {"accepted"}
    finally:
        api.close()


def test_narration_covers_live_and_terminal_states(tmp_path):
    rt = runtime(tmp_path)
    base = {"run_id": "run-x", "builder_name": "glm", "reviewer_names": ["claude"],
            "review_policy": "claude", "corrections_used": 0, "tasks": [
                {"status": "completed"}, {"status": "completed"},
                {"status": "pending"}, {"status": "pending"}]}
    assert rt.narrate({**base, "status": "building"}) == "GLM écrit le code…"
    assert rt.narrate({**base, "status": "testing"}) == "Tests en cours…"
    reviewing = rt.narrate({**base, "status": "reviewing"})
    assert "Claude relit le diff" in reviewing and "produit par GLM" in reviewing
    assert "gate 3/4" in reviewing
    correcting = rt.narrate({**base, "status": "correcting"})
    assert "Correction demandée" in correcting
    rebuild = rt.narrate({**base, "status": "building", "corrections_used": 1})
    assert "corrige le code" in rebuild
    assert "conservé" in rt.narrate({**base, "status": "accepted"})


def test_eta_uses_past_runs_of_same_config_and_size(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        first = finished_run(api)
        # Backdate the finished run so it has a measurable duration.
        run = api.runtime._read(first)
        run["created_at"] = "2026-07-17T10:00:00Z"
        run["updated_at"] = "2026-07-17T10:03:20Z"
        api.runtime._write(run)
        estimate = api.runtime.estimate_eta("glm", "claude", len(MISSION))
        assert estimate["based_on_runs"] == 1
        assert estimate["eta_seconds"] == 200
        # No history for that config: honest null estimate.
        empty = api.runtime.estimate_eta("codex", "codex", len(MISSION))
        assert empty == {"eta_seconds": None, "based_on_runs": 0, "bucket": "small"}
    finally:
        api.close()


def test_timeline_reads_like_a_story_with_durations(tmp_path):
    api = LocalAPIServer(runtime(tmp_path, claude=OneRepairLoopReviewer()))
    api.serve_in_thread()
    try:
        run_id = finished_run(api)
        assert api.runtime.get(run_id)["corrections_used"] == 1
        http(api, f"runs/{run_id}/approve", {})
        steps = http(api, f"runs/{run_id}/timeline")["steps"]
        labels = [step["label"] for step in steps]
        assert "Plan borné" in labels
        assert "Écriture du code" in labels
        assert "Correction demandée" in labels
        assert "Re-build après correction" in labels
        assert "Tests" in labels
        assert any(label.startswith("Review build par") for label in labels)
        assert "Approbation humaine" in labels and "Accepté" in labels
        assert all("duration_seconds" in step for step in steps)
    finally:
        api.close()
