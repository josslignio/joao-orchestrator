"""Chat-message -> mission-intent resolution (Boss directive, 2026-07-21):
deterministic, controller-owned alias table (data-driven from each project's
own `profile.json`, never hardcoded product identifiers in `bubble/
mission_intent.py` — package-boundary rule), replacing the earlier one-token
overlap matching that collided "Job Radar" with "Weekly Trading Radar" on
the shared word "radar" and could never resolve "JobRadar"/"Twitter Bot" at
all.

No real lot/task roadmap exists for either project named in the run card
(verified via `project_profiles/`/`projects/` inspection) — this module must
never fabricate one; it routes to the real Phase-0 kickoff state instead.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.joao_orchestrator.bubble.mission_intent import (
    load_project_aliases, resolve_chat_intent, resolve_project_alias,
)

REAL_ALIASES = {
    "job-opportunity-radar": ["JobRadar", "Job Radar", "Job Opportunity Radar", "Job CV Auto",
                             "job-cv-auto", "job-opportunity-radar"],
    "weekly-trading-radar": ["Twitter Bot", "Twitter Radar", "Trading Radar", "Weekly Trading Radar",
                            "weekly-trading-radar"],
}


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    projects_root = tmp_path / "projects"
    profiles_root = tmp_path / "project_profiles"
    projects_root.mkdir()
    profiles_root.mkdir()
    return projects_root, profiles_root


def _register_profile(profiles_root: Path, project_id: str, aliases: list[str] | None = None) -> None:
    project_dir = profiles_root / project_id
    project_dir.mkdir()
    data = {"project_id": project_id}
    if aliases is not None:
        data["aliases"] = aliases
    (project_dir / "profile.json").write_text(json.dumps(data))


def _real_profiles_root(tmp_path: Path) -> Path:
    _, profiles_root = _roots(tmp_path)
    for project_id, aliases in REAL_ALIASES.items():
        _register_profile(profiles_root, project_id, aliases)
    return profiles_root


# ---------------------------------------------------------------------------
# load_project_aliases — data-driven, no hardcoded identifiers in the module
# ---------------------------------------------------------------------------
def test_load_project_aliases_reads_from_profile_json(tmp_path):
    _, profiles_root = _roots(tmp_path)
    _register_profile(profiles_root, "widget-radar", ["Widget Radar", "WidgetRadar"])
    table = load_project_aliases(profiles_root)
    assert table == {"widget-radar": ["Widget Radar", "WidgetRadar"]}


def test_load_project_aliases_falls_back_to_project_id_when_no_aliases_field(tmp_path):
    _, profiles_root = _roots(tmp_path)
    _register_profile(profiles_root, "widget-radar")
    table = load_project_aliases(profiles_root)
    assert table == {"widget-radar": ["widget-radar"]}


def test_load_project_aliases_missing_root_returns_empty(tmp_path):
    assert load_project_aliases(tmp_path / "does-not-exist") == {}


# ---------------------------------------------------------------------------
# Required alias resolution tests (Boss directive, real project names)
# ---------------------------------------------------------------------------
def test_jobradar_one_word_alias(tmp_path):
    profiles_root = _real_profiles_root(tmp_path)
    result = resolve_project_alias("Termine le prochain lot JobRadar", profiles_root=profiles_root)
    assert result == {"ok": True, "project_id": "job-opportunity-radar", "matched_alias": "JobRadar"}


def test_jobradar_two_word_alias(tmp_path):
    profiles_root = _real_profiles_root(tmp_path)
    result = resolve_project_alias("Termine le prochain lot Job Radar", profiles_root=profiles_root)
    assert result["ok"] is True
    assert result["project_id"] == "job-opportunity-radar"


def test_jobradar_job_cv_auto_alias_maps_to_same_canonical_project(tmp_path):
    profiles_root = _real_profiles_root(tmp_path)
    result = resolve_project_alias("Lance le prochain lot Job CV Auto", profiles_root=profiles_root)
    assert result["ok"] is True
    assert result["project_id"] == "job-opportunity-radar"


def test_twitter_bot_alias_maps_to_weekly_trading_radar(tmp_path):
    profiles_root = _real_profiles_root(tmp_path)
    result = resolve_project_alias("Lance le prochain lot Twitter Bot", profiles_root=profiles_root)
    assert result == {"ok": True, "project_id": "weekly-trading-radar", "matched_alias": "Twitter Bot"}


def test_weekly_trading_radar_full_alias(tmp_path):
    profiles_root = _real_profiles_root(tmp_path)
    result = resolve_project_alias("Lance Weekly Trading Radar", profiles_root=profiles_root)
    assert result["ok"] is True
    assert result["project_id"] == "weekly-trading-radar"
    # rule 2: prefer the longest specific alias — "Weekly Trading Radar" is
    # longer than the also-present substring alias "Trading Radar".
    assert result["matched_alias"] == "Weekly Trading Radar"


def test_bare_radar_is_clarification_not_a_silent_pick(tmp_path):
    profiles_root = _real_profiles_root(tmp_path)
    result = resolve_project_alias("Lance le radar", profiles_root=profiles_root)
    assert result["ok"] is False
    assert result["reason_code"] == "AMBIGUOUS_PROJECT"
    assert set(result["candidates"]) == {"job-opportunity-radar", "weekly-trading-radar"}


def test_unknown_project_is_not_found(tmp_path):
    profiles_root = _real_profiles_root(tmp_path)
    result = resolve_project_alias("Termine le prochain lot Gadget Widget", profiles_root=profiles_root)
    assert result["ok"] is False
    assert result["reason_code"] == "PROJECT_NOT_FOUND"


def test_mixed_casing_and_punctuation(tmp_path):
    profiles_root = _real_profiles_root(tmp_path)
    result = resolve_project_alias("TERMINE LE PROCHAIN LOT: job--radar!!", profiles_root=profiles_root)
    assert result["ok"] is True
    assert result["project_id"] == "job-opportunity-radar"


def test_no_cross_project_collision_for_distinct_full_aliases(tmp_path):
    profiles_root = _real_profiles_root(tmp_path)
    jobradar = resolve_project_alias("Termine le prochain lot JobRadar", profiles_root=profiles_root)
    twitter = resolve_project_alias("Lance le prochain lot Twitter Bot", profiles_root=profiles_root)
    assert jobradar["project_id"] != twitter["project_id"]
    assert jobradar["project_id"] == "job-opportunity-radar"
    assert twitter["project_id"] == "weekly-trading-radar"


def test_hyphenated_and_underscored_forms_normalize_the_same(tmp_path):
    profiles_root = _real_profiles_root(tmp_path)
    result = resolve_project_alias("Lance le prochain lot weekly_trading_radar", profiles_root=profiles_root)
    assert result["ok"] is True
    assert result["project_id"] == "weekly-trading-radar"


# ---------------------------------------------------------------------------
# resolve_chat_intent — normalized action types
# ---------------------------------------------------------------------------
def test_valid_instruction_routes_to_phase0_kickoff_required_when_no_spec_exists(tmp_path):
    projects_root, profiles_root = _roots(tmp_path)
    _register_profile(profiles_root, "widget-radar", ["Widget Radar"])
    result = resolve_chat_intent("Termine le prochain lot Widget Radar",
                                projects_root=projects_root, profiles_root=profiles_root)
    assert result["ok"] is False
    assert result["action_type"] == "PHASE0_KICKOFF_REQUIRED"
    assert result["project_id"] == "widget-radar"
    assert result["spec_exists"] is False


def test_phase0_signed_but_no_lot_structure_is_clarification(tmp_path):
    projects_root, profiles_root = _roots(tmp_path)
    _register_profile(profiles_root, "widget-radar", ["Widget Radar"])
    project_dir = projects_root / "widget-radar"
    project_dir.mkdir()
    (project_dir / "PROJECT_SPEC.md").write_text("SIGNÉ : ✅ GO Boss\n\nsome content\n")
    result = resolve_chat_intent("Termine le prochain lot Widget Radar",
                                projects_root=projects_root, profiles_root=profiles_root)
    assert result["ok"] is False
    assert result["action_type"] == "CLARIFICATION_REQUIRED"
    assert result["project_id"] == "widget-radar"


def test_phase0_spec_exists_but_not_signed_is_signature_required(tmp_path):
    projects_root, profiles_root = _roots(tmp_path)
    _register_profile(profiles_root, "widget-radar", ["Widget Radar"])
    project_dir = projects_root / "widget-radar"
    project_dir.mkdir()
    (project_dir / "PROJECT_SPEC.md").write_text("SIGNÉ : ❌ EN ATTENTE\n\nsome content\n")
    result = resolve_chat_intent("Termine le prochain lot Widget Radar",
                                projects_root=projects_root, profiles_root=profiles_root)
    assert result["ok"] is False
    assert result["action_type"] == "PHASE0_SIGNATURE_REQUIRED"
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
    assert result["action_type"] == "NOT_A_MISSION_INTENT"


def test_project_not_found_action_type(tmp_path):
    projects_root, profiles_root = _roots(tmp_path)
    result = resolve_chat_intent("Termine le prochain lot Gadget Widget",
                                projects_root=projects_root, profiles_root=profiles_root)
    assert result["ok"] is False
    assert result["action_type"] == "PROJECT_NOT_FOUND"


def test_duplicate_or_replayed_instruction_is_deterministic_not_stateful(tmp_path):
    """This resolver is a pure function of its inputs — calling it twice with
    the exact same input is idempotent, never a hidden replay concern
    (unlike the worker-host's own request_id ledger, a different, already-
    covered layer)."""
    projects_root, profiles_root = _roots(tmp_path)
    _register_profile(profiles_root, "widget-radar", ["Widget Radar"])
    first = resolve_chat_intent("Termine le prochain lot Widget Radar",
                                projects_root=projects_root, profiles_root=profiles_root)
    second = resolve_chat_intent("Termine le prochain lot Widget Radar",
                                 projects_root=projects_root, profiles_root=profiles_root)
    assert first == second


def test_no_profiles_directory_at_all_is_not_found_not_a_crash(tmp_path):
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    missing_profiles_root = tmp_path / "does-not-exist"
    result = resolve_chat_intent("Termine le prochain lot Widget Radar",
                                projects_root=projects_root, profiles_root=missing_profiles_root)
    assert result["ok"] is False
    assert result["action_type"] == "PROJECT_NOT_FOUND"


# ---------------------------------------------------------------------------
# Real repo profiles — proves the actual committed alias data resolves
# correctly, not just a synthetic fixture.
# ---------------------------------------------------------------------------
def test_real_repo_profiles_resolve_jobradar_and_twitter_bot():
    repo_root = Path(__file__).resolve().parents[1]
    profiles_root = repo_root / "project_profiles"
    projects_root = repo_root / "projects"

    jobradar = resolve_chat_intent("Termine le prochain lot JobRadar",
                                   projects_root=projects_root, profiles_root=profiles_root)
    assert jobradar["project_id"] == "job-opportunity-radar"
    assert jobradar["action_type"] == "PHASE0_KICKOFF_REQUIRED"  # no PROJECT_SPEC.md exists for it

    twitter = resolve_chat_intent("Lance le prochain lot Twitter Bot",
                                  projects_root=projects_root, profiles_root=profiles_root)
    assert twitter["project_id"] == "weekly-trading-radar"
    assert twitter["action_type"] == "PHASE0_SIGNATURE_REQUIRED"  # PROJECT_SPEC.md exists, EN ATTENTE
