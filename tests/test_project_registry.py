"""ProjectRegistry (Boss directive, 2026-07-21): one centralized, auditable
resolution order for project-profile and project-authority roots — replacing
ad-hoc sibling-directory guessing. Must never silently search the user's
HOME directory, and must expose diagnostics of exactly which rule resolved
each root.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.joao_orchestrator.bubble import project_registry as pr_mod
from src.joao_orchestrator.bubble.project_registry import ProjectRegistry


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("JOAO_PROFILES_ROOT", raising=False)
    monkeypatch.delenv("JOAO_PROJECTS_ROOT", raising=False)


def test_explicit_arg_wins_over_everything(tmp_path):
    explicit = tmp_path / "my-profiles"
    explicit.mkdir()
    registry = ProjectRegistry(profiles_root=explicit)
    resolved = registry.resolve_profiles_root()
    assert resolved.source == "explicit_arg"
    assert resolved.path == explicit
    assert resolved.exists is True


def test_explicit_arg_reported_even_when_missing_not_silently_swapped(tmp_path):
    missing = tmp_path / "does-not-exist"
    registry = ProjectRegistry(profiles_root=missing)
    resolved = registry.resolve_profiles_root()
    assert resolved.source == "explicit_arg"
    assert resolved.exists is False


def test_env_var_wins_over_repo_default(tmp_path, monkeypatch):
    env_dir = tmp_path / "env-profiles"
    env_dir.mkdir()
    monkeypatch.setenv("JOAO_PROFILES_ROOT", str(env_dir))
    registry = ProjectRegistry()
    resolved = registry.resolve_profiles_root()
    assert resolved.source == "explicit_env"
    assert resolved.path == env_dir


def test_repo_default_used_when_no_explicit_or_env(tmp_path):
    registry = ProjectRegistry()
    resolved = registry.resolve_profiles_root()
    assert resolved.source == "repo"
    assert resolved.path == pr_mod.REPO_ROOT / "project_profiles"
    assert resolved.exists is True


def test_state_manifest_symlink_used_only_when_repo_default_absent(tmp_path):
    # Simulate "no repo default" by pointing REPO_ROOT-derived resolution away:
    # exercise the state-manifest branch directly via a fresh state_root whose
    # symlink is the only real profiles source, with repo_default forced absent
    # by monkeypatching the module's REPO_ROOT for this one registry instance.
    state_root = tmp_path / "state"
    state_root.mkdir()
    real_profiles = tmp_path / "real-profiles"
    real_profiles.mkdir()
    (state_root / "project_profiles").symlink_to(real_profiles)

    resolved = pr_mod._resolve(explicit=None, env_var="JOAO_PROFILES_ROOT_TEST_UNSET",
                              repo_default=tmp_path / "no-such-repo-default",
                              state_root=state_root, manifest_key="project_profiles")
    assert resolved.source == "state_manifest"
    assert resolved.path == real_profiles
    assert resolved.exists is True


def test_state_manifest_file_declares_root(tmp_path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    real_profiles = tmp_path / "declared-profiles"
    real_profiles.mkdir()
    manifest = {"project_profiles": str(real_profiles)}
    (state_root / "project_registry.manifest.json").write_text(json.dumps(manifest))

    resolved = pr_mod._resolve(explicit=None, env_var="JOAO_PROFILES_ROOT_TEST_UNSET_2",
                              repo_default=tmp_path / "no-such-repo-default",
                              state_root=state_root, manifest_key="project_profiles")
    assert resolved.source == "state_manifest"
    assert resolved.path == real_profiles


def test_no_declaration_at_all_fails_closed_never_home_search(tmp_path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    resolved = pr_mod._resolve(explicit=None, env_var="JOAO_PROFILES_ROOT_TEST_UNSET_3",
                              repo_default=tmp_path / "no-such-repo-default",
                              state_root=state_root, manifest_key="project_profiles")
    assert resolved.source == "unresolved"
    assert resolved.exists is False


def test_projects_root_mirrors_same_order(tmp_path):
    registry = ProjectRegistry()
    resolved = registry.resolve_projects_root()
    assert resolved.source == "repo"
    assert resolved.path == pr_mod.REPO_ROOT / "projects"


def test_diagnostics_reports_both_roots():
    registry = ProjectRegistry()
    diag = registry.diagnostics()
    assert set(diag) == {"profiles_root", "projects_root"}
    assert diag["profiles_root"]["source"] == "repo"
    assert diag["projects_root"]["source"] == "repo"


def test_project_repository_path_reads_declared_field_from_real_repo():
    registry = ProjectRegistry()
    path = registry.project_repository_path("job-opportunity-radar")
    assert path == Path("/Users/jocelyngrosjean/job-opportunity-radar")


def test_project_repository_path_none_when_not_declared(tmp_path):
    profiles_root = tmp_path / "project_profiles"
    profiles_root.mkdir()
    project_dir = profiles_root / "widget-radar"
    project_dir.mkdir()
    (project_dir / "profile.json").write_text(json.dumps({"project_id": "widget-radar"}))
    registry = ProjectRegistry(profiles_root=profiles_root)
    assert registry.project_repository_path("widget-radar") is None


def test_project_repository_path_none_for_unknown_project(tmp_path):
    profiles_root = tmp_path / "project_profiles"
    profiles_root.mkdir()
    registry = ProjectRegistry(profiles_root=profiles_root)
    assert registry.project_repository_path("no-such-project") is None
