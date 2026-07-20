"""C8-B temporary reviewer topology (Boss decision, pre-Codex window until
2026-07-23): `bubble/worker_topology.py` selection logic, Codex live
availability capability detection, and the negative proofs the topology is
required to hold under (`JOAO_C8_GATE_CONTRACTS.md` G-DBL-AUDIT, `JOAO_
WORKER_INTEGRATION_SPEC.md` §5 identity table).
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from src.joao_orchestrator.bubble import gates
from src.joao_orchestrator.bubble.runtime import ClaudeCLIReviewer, CodexCLIReviewer, GLMBuilder, GLMReviewer
from src.joao_orchestrator.bubble.worker_topology import (
    REASON_NO_SECOND_DISTINCT_FAMILY, select_critical_reviewer_families, select_normal_reviewer_family,
)

TREE = "a" * 40


def _verdict(cls, ok=True, decision="pass", tree=TREE):
    return {"provider": cls.provider, "provider_family": cls.provider_family, "model": cls.model,
            "ok": ok, "decision": decision, "candidate_tree": tree}


# ---------------------------------------------------------------------------
# select_normal_reviewer_family — fixed PRIMARY/FALLBACK pairing
# ---------------------------------------------------------------------------
def test_normal_topology_glm_builder_pairs_with_claude():
    result = select_normal_reviewer_family("zai")
    assert result == {"ok": True, "reviewer_family": "anthropic"}


def test_normal_topology_claude_builder_pairs_with_glm():
    result = select_normal_reviewer_family("anthropic")
    assert result == {"ok": True, "reviewer_family": "zai"}


def test_normal_topology_unknown_builder_family_blocks():
    result = select_normal_reviewer_family("openai")
    assert result["ok"] is False
    assert result["reason_code"] == "C8B_NO_NORMAL_TOPOLOGY_FOR_BUILDER_FAMILY"


def test_normal_topology_never_selects_the_builders_own_family():
    for family in ("zai", "anthropic"):
        result = select_normal_reviewer_family(family)
        assert result["ok"] is True
        assert result["reviewer_family"] != family


# ---------------------------------------------------------------------------
# select_critical_reviewer_families — Codex-availability-gated
# ---------------------------------------------------------------------------
def test_critical_topology_blocks_while_codex_unavailable_glm_builder():
    result = select_critical_reviewer_families("zai", codex_available=False)
    assert result["ok"] is False
    assert result["reason_code"] == REASON_NO_SECOND_DISTINCT_FAMILY == "G_DBL_AUDIT_NO_SECOND_DISTINCT_FAMILY_AVAILABLE"


def test_critical_topology_blocks_while_codex_unavailable_claude_builder():
    result = select_critical_reviewer_families("anthropic", codex_available=False)
    assert result["ok"] is False
    assert result["reason_code"] == REASON_NO_SECOND_DISTINCT_FAMILY


def test_critical_topology_auto_allows_codex_once_available_glm_builder():
    result = select_critical_reviewer_families("zai", codex_available=True)
    assert result == {"ok": True, "reviewer_families": ["anthropic", "openai"]}


def test_critical_topology_auto_allows_codex_once_available_claude_builder():
    result = select_critical_reviewer_families("anthropic", codex_available=True)
    assert result == {"ok": True, "reviewer_families": ["zai", "openai"]}


def test_critical_topology_unknown_builder_family_blocks_even_with_codex_available():
    result = select_critical_reviewer_families("openai", codex_available=True)
    assert result["ok"] is False
    assert result["reason_code"] == "C8B_NO_CRITICAL_TOPOLOGY_FOR_BUILDER_FAMILY"


# ---------------------------------------------------------------------------
# Codex live availability capability detection — never a fabricated PASS
# ---------------------------------------------------------------------------
def test_codex_unavailable_when_executable_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nonexistent-codex-home"))
    reviewer = CodexCLIReviewer(executable=str(tmp_path / "no-such-codex-binary"))
    assert reviewer.available() is False


def test_codex_unavailable_when_binary_present_but_no_codex_home(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 0\n")
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nonexistent-codex-home"))
    reviewer = CodexCLIReviewer(executable=str(fake_codex))
    assert reviewer.available() is False


def test_codex_available_when_binary_and_codex_home_both_present(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 0\n")
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IEXEC)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    reviewer = CodexCLIReviewer(executable=str(fake_codex))
    assert reviewer.available() is True


def test_unavailable_codex_is_never_selectable_via_topology(tmp_path, monkeypatch):
    """Negative proof: an unavailable Codex must never be selected — the
    topology's own `codex_available` flag, sourced from a real live probe,
    is False, and CRITICAL BLOCKs rather than presenting Codex as ready."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nonexistent-codex-home"))
    reviewer = CodexCLIReviewer(executable=str(tmp_path / "no-such-codex-binary"))
    assert reviewer.available() is False
    result = select_critical_reviewer_families("zai", codex_available=reviewer.available())
    assert result["ok"] is False
    assert result["reason_code"] == REASON_NO_SECOND_DISTINCT_FAMILY


# ---------------------------------------------------------------------------
# NEGATIVE PROOFS (Boss run card) — wired against gate_dbl_audit directly,
# using real adapter identities, exactly like test_c8_dbl_audit_wiring.py.
# ---------------------------------------------------------------------------
def test_glm_builder_plus_glm_reviewer_blocks():
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider=GLMBuilder.provider, builder_family=GLMBuilder.provider_family,
        reviewer_verdicts=[_verdict(GLMReviewer)], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_BUILDER_SELF_REVIEW"


def test_claude_builder_plus_claude_reviewer_blocks():
    # A ClaudeCodeBuilder is C8-B backlog (not implemented this lot — no
    # ANTHROPIC_API_KEY-only auth path exists on this host, see
    # JOAO_C8_GATES_ROADMAP.md); the identity claim under test — same
    # provider_family self-review must BLOCK — is exercised directly against
    # gate_dbl_audit using ClaudeCLIReviewer's real identity on both sides,
    # exactly like GLMBuilder/GLMReviewer above.
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider=ClaudeCLIReviewer.provider,
        builder_family=ClaudeCLIReviewer.provider_family,
        reviewer_verdicts=[_verdict(ClaudeCLIReviewer)], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_BUILDER_SELF_REVIEW"


def test_glm_critical_claude_only_blocks_insufficient_distinct_families():
    # One accepted, genuinely distinct-family verdict is not enough for
    # critical (requires exactly 2) — this is the POST-dispatch gate_dbl_audit
    # counterpart to the PRE-dispatch topology refusal exercised above.
    result = gates.gate_dbl_audit(
        risk_tier="critical", builder_provider=GLMBuilder.provider, builder_family=GLMBuilder.provider_family,
        reviewer_verdicts=[_verdict(ClaudeCLIReviewer)], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE"


def test_claude_critical_glm_only_blocks_insufficient_distinct_families():
    result = gates.gate_dbl_audit(
        risk_tier="critical", builder_provider=ClaudeCLIReviewer.provider,
        builder_family=ClaudeCLIReviewer.provider_family,
        reviewer_verdicts=[_verdict(GLMReviewer)], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE"


def test_glm_builder_claude_reviewer_normal_tier_passes():
    """The PRIMARY normal path this run card requires: GLM builder -> Claude
    reviewer, distinct families -> PASS."""
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider=GLMBuilder.provider, builder_family=GLMBuilder.provider_family,
        reviewer_verdicts=[_verdict(ClaudeCLIReviewer)], candidate_tree=TREE)
    assert result["ok"] is True and result["reason_code"] == "G_DBL_AUDIT_OK"


def test_claude_builder_glm_reviewer_normal_tier_passes():
    """The FALLBACK normal path this run card requires: Claude builder -> GLM
    reviewer, distinct families -> PASS."""
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider=ClaudeCLIReviewer.provider,
        builder_family=ClaudeCLIReviewer.provider_family,
        reviewer_verdicts=[_verdict(GLMReviewer)], candidate_tree=TREE)
    assert result["ok"] is True and result["reason_code"] == "G_DBL_AUDIT_OK"
