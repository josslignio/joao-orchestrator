"""C8-B provider identity / self-review guard matrix (Boss directive,
2026-07-20): builder and reviewer adapters belonging to the same product
share ONE controller-owned identity constant — never independently-typed
string literals that could drift apart — and `gate_dbl_audit` compares the
EXACT `provider` string, never merely `provider_family` (two different
products in the same family, e.g. Codex vs. a hypothetical second `openai`
tool, must still be distinguishable when they are NOT the builder).
"""
from __future__ import annotations

from src.joao_orchestrator.bubble import gates
from src.joao_orchestrator.bubble.runtime import (
    CLAUDE_CLI_PROVIDER, ClaudeCLIReviewer, ClaudeCodeBuilder, GLMBuilder, GLMReviewer,
)

TREE = "a" * 40


def _verdict(provider, family, model, ok=True, decision="pass", tree=TREE):
    return {"provider": provider, "provider_family": family, "model": model,
            "ok": ok, "decision": decision, "candidate_tree": tree}


# ---------------------------------------------------------------------------
# Shared identity constants — never duplicated string literals
# ---------------------------------------------------------------------------
def test_glm_builder_and_reviewer_share_the_same_provider_identity():
    assert GLMBuilder.provider == GLMReviewer.provider == "zai-coding-plan"
    assert GLMBuilder.provider_family == GLMReviewer.provider_family == "zai"


def test_claude_builder_and_reviewer_share_the_same_provider_identity():
    assert ClaudeCodeBuilder.provider == ClaudeCLIReviewer.provider == CLAUDE_CLI_PROVIDER
    assert ClaudeCodeBuilder.provider_family == ClaudeCLIReviewer.provider_family == "anthropic"


# ---------------------------------------------------------------------------
# Required negative tests (Boss directive)
# ---------------------------------------------------------------------------
def test_glm_build_reviewed_by_glm_blocks_with_self_review_reason():
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider=GLMBuilder.provider, builder_family=GLMBuilder.provider_family,
        reviewer_verdicts=[_verdict(GLMReviewer.provider, GLMReviewer.provider_family, GLMReviewer.model)],
        candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_BUILDER_SELF_REVIEW"


def test_claude_build_reviewed_by_claude_blocks_with_self_review_reason():
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider=ClaudeCodeBuilder.provider,
        builder_family=ClaudeCodeBuilder.provider_family,
        reviewer_verdicts=[_verdict(ClaudeCLIReviewer.provider, ClaudeCLIReviewer.provider_family,
                                    ClaudeCLIReviewer.model)],
        candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_BUILDER_SELF_REVIEW"


def test_alias_or_model_name_difference_never_bypasses_self_review():
    """A different MODEL string (an alias/version bump) on the same
    provider/family must still be caught — self-review is keyed on
    provider/provider_family, never on the (irrelevant, model-controlled)
    `model` field."""
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider=GLMBuilder.provider, builder_family=GLMBuilder.provider_family,
        reviewer_verdicts=[_verdict(GLMReviewer.provider, GLMReviewer.provider_family,
                                    "zai-coding-plan/glm-4.9-totally-different-alias")],
        candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_BUILDER_SELF_REVIEW"


def test_alias_provider_string_never_bypasses_self_review_via_family_match_alone():
    """Even a reviewer whose `provider` string is spelled differently from
    the builder's, but shares the SAME provider_family, must still BLOCK for
    critical's distinct-family requirement (step 5 in gate_dbl_audit checks
    `provider == builder_provider OR provider_family == builder_family`) —
    an alias can never satisfy independence via family alone."""
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider=GLMBuilder.provider, builder_family=GLMBuilder.provider_family,
        reviewer_verdicts=[_verdict("zai-coding-plan-renamed-alias", GLMBuilder.provider_family, "some-model")],
        candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_BUILDER_SELF_REVIEW"


def test_same_family_reviewers_never_satisfy_distinct_family_requirement_critical():
    result = gates.gate_dbl_audit(
        risk_tier="critical", builder_provider=GLMBuilder.provider, builder_family=GLMBuilder.provider_family,
        reviewer_verdicts=[
            _verdict(ClaudeCLIReviewer.provider, ClaudeCLIReviewer.provider_family, ClaudeCLIReviewer.model),
            _verdict("another-anthropic-tool", "anthropic", "some-model"),
        ], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_SAME_FAMILY"


def test_gate_compares_exact_provider_not_only_family_when_builder_is_named():
    """Two DIFFERENT products in the SAME family as the builder must both
    still be refused (both share provider_family with the builder) — proves
    the gate isn't merely checking `provider != builder_provider` and
    silently accepting a family match."""
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider=GLMBuilder.provider, builder_family=GLMBuilder.provider_family,
        reviewer_verdicts=[_verdict("a-totally-different-zai-tool", "zai", "some-model")],
        candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_BUILDER_SELF_REVIEW"


def test_reciprocal_paths_pass_with_exact_distinct_identities():
    glm_to_claude = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider=GLMBuilder.provider, builder_family=GLMBuilder.provider_family,
        reviewer_verdicts=[_verdict(ClaudeCLIReviewer.provider, ClaudeCLIReviewer.provider_family,
                                    ClaudeCLIReviewer.model)], candidate_tree=TREE)
    assert glm_to_claude["ok"] is True and glm_to_claude["reason_code"] == "G_DBL_AUDIT_OK"

    claude_to_glm = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider=ClaudeCodeBuilder.provider,
        builder_family=ClaudeCodeBuilder.provider_family,
        reviewer_verdicts=[_verdict(GLMReviewer.provider, GLMReviewer.provider_family, GLMReviewer.model)],
        candidate_tree=TREE)
    assert claude_to_glm["ok"] is True and claude_to_glm["reason_code"] == "G_DBL_AUDIT_OK"
