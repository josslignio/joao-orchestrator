"""Chat-message -> mission-intent resolution (Boss directive, 2026-07-21,
corrected before implementation: no real lot/task roadmap exists for either
project the run card names — verified via `project_profiles/`/`projects/`
inspection. This module must never fabricate a lot; it routes to the real
Phase-0 kickoff state instead.

Fully generic fixture names below (e.g. "widget-radar") — this module is
product-agnostic by design (package-boundary rule), so its tests never
reference a real product identifier either.
"""
from __future__ import annotations

from pathlib import Path

from src.joao_orchestrator.bubble.mission_intent import resolve_chat_intent


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    projects_root = tmp_path / "projects"
    profiles_root = tmp_path / "project_profiles"
    projects_root.mkdir()
    profiles_root.mkdir()
    return projects_root, profiles_root


def _register_profile(profiles_root: Path, project_id: str) -> None:
    project_dir = profiles_root / project_id
    project_dir.mkdir()
    (project_dir / "profile.json").write_text("{}")


def test_valid_instruction_routes_to_kickoff_when_no_spec_exists(tmp_path):
    projects_root, profiles_root = _roots(tmp_path)
    _register_profile(profiles_root, "widget-radar")
    result = resolve_chat_intent("Termine le prochain lot Widget Radar",
                                projects_root=projects_root, profiles_root=profiles_root)
    assert result["ok"] is False
    assert result["kind"] == "phase0_incomplete"
    assert result["project_id"] == "widget-radar"
    assert result["spec_exists"] is False
    assert result["action"] == "open_kickoff"


def test_valid_instruction_alias_case_and_casing(tmp_path):
    projects_root, profiles_root = _roots(tmp_path)
    _register_profile(profiles_root, "widget-radar")
    for phrasing in ["termine le prochain lot widget radar", "LANCE LE PROCHAIN LOT WIDGET-RADAR",
                     "Continue next lot Widget Radar"]:
        result = resolve_chat_intent(phrasing, projects_root=projects_root, profiles_root=profiles_root)
        assert result["kind"] == "phase0_incomplete"
        assert result["project_id"] == "widget-radar"


def test_unregistered_project_is_ambiguous_not_guessed(tmp_path):
    """No profile directory matches "Gadget Bot" — it must never be guessed
    or silently mapped to an unrelated registered project."""
    projects_root, profiles_root = _roots(tmp_path)
    _register_profile(profiles_root, "widget-radar")
    result = resolve_chat_intent("Lance le prochain lot Gadget Bot",
                                projects_root=projects_root, profiles_root=profiles_root)
    assert result["ok"] is False
    assert result["kind"] == "ambiguous"
    assert result["candidates"] == []


def test_multiple_matching_profiles_is_ambiguous(tmp_path):
    projects_root, profiles_root = _roots(tmp_path)
    _register_profile(profiles_root, "widget-radar")
    _register_profile(profiles_root, "widget-tracker")
    result = resolve_chat_intent("Termine le prochain lot Widget",
                                projects_root=projects_root, profiles_root=profiles_root)
    assert result["ok"] is False
    assert result["kind"] == "ambiguous"
    assert set(result["candidates"]) == {"widget-radar", "widget-tracker"}


def test_phase0_signed_but_no_lot_structure(tmp_path):
    projects_root, profiles_root = _roots(tmp_path)
    _register_profile(profiles_root, "widget-radar")
    project_dir = projects_root / "widget-radar"
    project_dir.mkdir()
    (project_dir / "PROJECT_SPEC.md").write_text("SIGNÉ : ✅ GO Boss\n\nsome content\n")
    result = resolve_chat_intent("Termine le prochain lot Widget Radar",
                                projects_root=projects_root, profiles_root=profiles_root)
    assert result["ok"] is False
    assert result["kind"] == "no_lot_structure"
    assert result["project_id"] == "widget-radar"


def test_phase0_spec_exists_but_not_signed(tmp_path):
    projects_root, profiles_root = _roots(tmp_path)
    _register_profile(profiles_root, "widget-radar")
    project_dir = projects_root / "widget-radar"
    project_dir.mkdir()
    (project_dir / "PROJECT_SPEC.md").write_text("SIGNÉ : ❌ EN ATTENTE\n\nsome content\n")
    result = resolve_chat_intent("Termine le prochain lot Widget Radar",
                                projects_root=projects_root, profiles_root=profiles_root)
    assert result["ok"] is False
    assert result["kind"] == "phase0_incomplete"
    assert result["spec_exists"] is True
    assert result["signed"] is False


def test_no_eligible_roadmap_lot_never_fabricates_a_mission():
    """No path through this resolver ever returns ok=true with a constructed
    mission — that data doesn't exist yet for any project."""
    import inspect
    from src.joao_orchestrator.bubble import mission_intent
    source = inspect.getsource(mission_intent.resolve_chat_intent)
    assert '"ok": True' not in source


def test_malformed_non_mission_message_is_recognized_as_such(tmp_path):
    projects_root, profiles_root = _roots(tmp_path)
    result = resolve_chat_intent("what's the weather like",
                                projects_root=projects_root, profiles_root=profiles_root)
    assert result["ok"] is False
    assert result["kind"] == "not_a_mission_intent"


def test_duplicate_or_replayed_instruction_is_deterministic_not_stateful(tmp_path):
    """This resolver is a pure function of its inputs — calling it twice with
    the exact same input is idempotent, never a hidden replay concern
    (unlike the worker-host's own request_id ledger, a different, already-
    covered layer)."""
    projects_root, profiles_root = _roots(tmp_path)
    _register_profile(profiles_root, "widget-radar")
    first = resolve_chat_intent("Termine le prochain lot Widget Radar",
                                projects_root=projects_root, profiles_root=profiles_root)
    second = resolve_chat_intent("Termine le prochain lot Widget Radar",
                                 projects_root=projects_root, profiles_root=profiles_root)
    assert first == second


def test_no_profiles_directory_at_all_is_ambiguous_not_a_crash(tmp_path):
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    missing_profiles_root = tmp_path / "does-not-exist"
    result = resolve_chat_intent("Termine le prochain lot Widget Radar",
                                projects_root=projects_root, profiles_root=missing_profiles_root)
    assert result["ok"] is False
    assert result["kind"] == "ambiguous"
