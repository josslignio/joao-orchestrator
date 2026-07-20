"""C8-A: unit + red->green adversarial tests for the 7 pure gate functions in
`src/joao_orchestrator/bubble/gates.py` (`JOAO_C8_GATE_CONTRACTS.md` v4).

Every reason code the contract lists gets at least one red (BLOCK) case and
one green (pass) case, exercised directly against the real gate functions
(hand-built input dicts) — the "real entrypoint" for these six gates in
C8-A IS `bubble/gates.py` itself (they are not yet wired into
`approve()`/`promote()`; that wiring is C8-B/C8-C scope per
`JOAO_C8_GATES_ROADMAP.md`). `gate_frozen_finish_line` additionally has a
real runtime call site (`RunRuntime.start()` writes the `frozen_mission.json`
it consumes) — proven separately in `tests/test_frozen_mission.py`.
"""
from __future__ import annotations

import pytest

from src.joao_orchestrator.bubble import gates

TREE = "a" * 40
OTHER_TREE = "b" * 40


# ---------------------------------------------------------------------------
# G-DBL-AUDIT
# ---------------------------------------------------------------------------


def _verdict(provider: str, family: str, tree: str = TREE, ok: bool = True, decision: str = "pass") -> dict:
    return {"provider": provider, "provider_family": family, "model": "m", "ok": ok,
            "decision": decision, "candidate_tree": tree}


def test_g_dbl_audit_red_missing_risk_tier_is_insufficient_reviewers():
    result = gates.gate_dbl_audit(risk_tier=None, builder_provider="zai-coding-plan",
                                  builder_family="zai", reviewer_verdicts=[], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_INSUFFICIENT_REVIEWERS"


def test_g_dbl_audit_green_normal_one_distinct_reviewer_passes():
    result = gates.gate_dbl_audit(risk_tier="normal", builder_provider="zai-coding-plan",
                                  builder_family="zai",
                                  reviewer_verdicts=[_verdict("codex-subscription", "openai")],
                                  candidate_tree=TREE)
    assert result["ok"] is True and result["decision"] == "pass"


def test_g_dbl_audit_red_normal_insufficient_reviewers_zero_accepts():
    result = gates.gate_dbl_audit(risk_tier="normal", builder_provider="zai-coding-plan",
                                  builder_family="zai", reviewer_verdicts=[], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_INSUFFICIENT_REVIEWERS"


def test_g_dbl_audit_red_builder_self_review():
    result = gates.gate_dbl_audit(risk_tier="critical", builder_provider="zai-coding-plan",
                                  builder_family="zai",
                                  reviewer_verdicts=[_verdict("zai-coding-plan", "zai"),
                                                     _verdict("codex-subscription", "openai")],
                                  candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_BUILDER_SELF_REVIEW"


def test_g_dbl_audit_red_same_provider():
    # Exercised at `critical`, the only tier where two verdicts is the EXACT
    # required cardinality and step 6 (provider uniqueness) is therefore
    # reachable at all. Under the mandated decision order, `normal` + two
    # verdicts is settled earlier by step 3 as TOO_MANY_REVIEWERS (proved by
    # test_g_dbl_audit_red_normal_two_accepted_blocks_too_many), so asserting
    # SAME_PROVIDER there would have been asserting an unreachable branch.
    result = gates.gate_dbl_audit(risk_tier="critical", builder_provider="zai-coding-plan",
                                  builder_family="zai",
                                  reviewer_verdicts=[_verdict("codex-subscription", "openai"),
                                                     _verdict("codex-subscription", "openai")],
                                  candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_SAME_PROVIDER"


def test_g_dbl_audit_red_critical_same_family_codex_plus_gpt_insufficient():
    # finding GPT v3: Codex + a formal-GPT import are both provider_family="openai" —
    # not two distinct families, must NOT satisfy critical.
    result = gates.gate_dbl_audit(risk_tier="critical", builder_provider="zai-coding-plan",
                                  builder_family="zai",
                                  reviewer_verdicts=[_verdict("codex-subscription", "openai"),
                                                     _verdict("openai-gpt", "openai")],
                                  candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_SAME_FAMILY"


def test_g_dbl_audit_red_critical_no_distinct_family_available():
    result = gates.gate_dbl_audit(risk_tier="critical", builder_provider="zai-coding-plan",
                                  builder_family="zai",
                                  reviewer_verdicts=[_verdict("codex-subscription", "openai")],
                                  candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE"


def test_g_dbl_audit_green_critical_two_distinct_families_passes():
    result = gates.gate_dbl_audit(risk_tier="critical", builder_provider="zai-coding-plan",
                                  builder_family="zai",
                                  reviewer_verdicts=[_verdict("codex-subscription", "openai"),
                                                     _verdict("claude-cli", "anthropic")],
                                  candidate_tree=TREE)
    assert result["ok"] is True and result["decision"] == "pass"


def test_g_dbl_audit_red_tree_mismatch():
    result = gates.gate_dbl_audit(risk_tier="normal", builder_provider="zai-coding-plan",
                                  builder_family="zai",
                                  reviewer_verdicts=[_verdict("codex-subscription", "openai", tree=OTHER_TREE)],
                                  candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_TREE_MISMATCH"


# --- correction loop: mandatory-input fail-closed cases -------------------


def test_g_dbl_audit_red_candidate_tree_missing():
    result = gates.gate_dbl_audit(risk_tier="normal", builder_provider="zai-coding-plan",
                                  builder_family="zai",
                                  reviewer_verdicts=[_verdict("codex-subscription", "openai", tree=None)],
                                  candidate_tree=None)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_INSUFFICIENT_REVIEWERS"
    assert "candidate_tree" in result["reason"]


def test_g_dbl_audit_red_candidate_tree_empty_string():
    result = gates.gate_dbl_audit(risk_tier="normal", builder_provider="zai-coding-plan",
                                  builder_family="zai", reviewer_verdicts=[], candidate_tree="")
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_INSUFFICIENT_REVIEWERS"


def test_g_dbl_audit_red_builder_provider_missing():
    result = gates.gate_dbl_audit(risk_tier="normal", builder_provider=None, builder_family="zai",
                                  reviewer_verdicts=[], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_INSUFFICIENT_REVIEWERS"


def test_g_dbl_audit_red_builder_family_missing():
    result = gates.gate_dbl_audit(risk_tier="normal", builder_provider="zai-coding-plan", builder_family=None,
                                  reviewer_verdicts=[], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_INSUFFICIENT_REVIEWERS"


def test_g_dbl_audit_red_reviewer_inventory_none_is_zero_accepted():
    # `reviewer_verdicts=None` (inventory genuinely absent, not merely empty)
    # must fail exactly like an empty list — never treated differently.
    result = gates.gate_dbl_audit(risk_tier="normal", builder_provider="zai-coding-plan",
                                  builder_family="zai", reviewer_verdicts=None, candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_INSUFFICIENT_REVIEWERS"
    assert result["accepted_count"] == 0


# --- correction loop: EXACT reviewer cardinality (finding #3) -------------


def test_g_dbl_audit_red_normal_zero_accepted_blocks():
    result = gates.gate_dbl_audit(risk_tier="normal", builder_provider="zai-coding-plan",
                                  builder_family="zai", reviewer_verdicts=[], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_INSUFFICIENT_REVIEWERS"


def test_g_dbl_audit_red_normal_two_accepted_blocks_too_many():
    result = gates.gate_dbl_audit(risk_tier="normal", builder_provider="zai-coding-plan",
                                  builder_family="zai",
                                  reviewer_verdicts=[_verdict("codex-subscription", "openai"),
                                                     _verdict("claude-cli", "anthropic")],
                                  candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_TOO_MANY_REVIEWERS"


def test_g_dbl_audit_green_normal_exactly_one_accepted_passes():
    result = gates.gate_dbl_audit(risk_tier="normal", builder_provider="zai-coding-plan",
                                  builder_family="zai",
                                  reviewer_verdicts=[_verdict("codex-subscription", "openai")],
                                  candidate_tree=TREE)
    assert result["ok"] is True


def test_g_dbl_audit_red_critical_zero_accepted_blocks():
    result = gates.gate_dbl_audit(risk_tier="critical", builder_provider="zai-coding-plan",
                                  builder_family="zai", reviewer_verdicts=[], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE"


def test_g_dbl_audit_red_critical_one_accepted_blocks():
    result = gates.gate_dbl_audit(risk_tier="critical", builder_provider="zai-coding-plan",
                                  builder_family="zai",
                                  reviewer_verdicts=[_verdict("codex-subscription", "openai")],
                                  candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE"


def test_g_dbl_audit_red_critical_three_accepted_distinct_families_blocks_too_many():
    result = gates.gate_dbl_audit(risk_tier="critical", builder_provider="zai-coding-plan",
                                  builder_family="zai",
                                  reviewer_verdicts=[_verdict("codex-subscription", "openai"),
                                                     _verdict("claude-cli", "anthropic"),
                                                     _verdict("mistral-cli", "mistral")],
                                  candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_TOO_MANY_REVIEWERS"


def test_g_dbl_audit_green_critical_exactly_two_distinct_families_passes():
    result = gates.gate_dbl_audit(risk_tier="critical", builder_provider="zai-coding-plan",
                                  builder_family="zai",
                                  reviewer_verdicts=[_verdict("codex-subscription", "openai"),
                                                     _verdict("claude-cli", "anthropic")],
                                  candidate_tree=TREE)
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# G-HERMETIC
# ---------------------------------------------------------------------------


def test_g_hermetic_red_real_memory_touched():
    result = gates.gate_hermetic(touches=[{"root": "real_memory", "path": "/repo/memory/lessons.jsonl",
                                           "injected": False}])
    assert result["ok"] is False
    assert result["reason_code"] == "G_HERMETIC_REAL_MEMORY_TOUCHED"


def test_g_hermetic_red_external_ledger_dependency():
    result = gates.gate_hermetic(touches=[{"root": "external_ledger",
                                           "path": "/Users/x/Claude-HQ/DEFECTS_LEDGER.md", "injected": False}])
    assert result["ok"] is False
    assert result["reason_code"] == "G_HERMETIC_EXTERNAL_LEDGER_DEPENDENCY"


def test_g_hermetic_red_uninjected_root_generic():
    result = gates.gate_hermetic(touches=[{"root": "sibling_repos",
                                           "path": "/Users/x/job-opportunity-radar/x.yaml", "injected": False}])
    assert result["ok"] is False
    assert result["reason_code"] == "G_HERMETIC_UNINJECTED_ROOT"


def test_g_hermetic_green_injected_touch_passes():
    result = gates.gate_hermetic(touches=[{"root": "external_ledger", "path": "/tmp/x/ledger.md",
                                           "injected": True}])
    assert result["ok"] is True


def test_g_hermetic_green_no_touches_passes():
    result = gates.gate_hermetic(touches=[])
    assert result["ok"] is True and result["reason_code"] == "G_HERMETIC_OK"


# ---------------------------------------------------------------------------
# G-AUTH-IO
# ---------------------------------------------------------------------------


def test_g_auth_io_red_helper_only_coverage_no_required_symbols():
    result = gates.gate_auth_io(gate_name="G-X", required_entrypoint_symbols=[],
                                referenced_symbols=[], resolvable_symbols=[])
    assert result["ok"] is False
    assert result["reason_code"] == "G_AUTH_IO_HELPER_ONLY_COVERAGE"


def test_g_auth_io_red_helper_only_coverage_symbol_not_referenced():
    result = gates.gate_auth_io(gate_name="G-X",
                                required_entrypoint_symbols=["CodexEvidenceReviewer.review"],
                                referenced_symbols=["validate_reviewer_verdict"],
                                resolvable_symbols=["CodexEvidenceReviewer.review"])
    assert result["ok"] is False
    assert result["reason_code"] == "G_AUTH_IO_HELPER_ONLY_COVERAGE"


def test_g_auth_io_red_entrypoint_unreachable():
    result = gates.gate_auth_io(gate_name="G-X",
                                required_entrypoint_symbols=["Stale.method"],
                                referenced_symbols=["Stale.method"], resolvable_symbols=[])
    assert result["ok"] is False
    assert result["reason_code"] == "G_AUTH_IO_ENTRYPOINT_UNREACHABLE"


def test_g_auth_io_green_required_symbol_referenced_and_resolvable():
    result = gates.gate_auth_io(gate_name="G-X",
                                required_entrypoint_symbols=["CodexEvidenceReviewer.review"],
                                referenced_symbols=["CodexEvidenceReviewer.review", "other"],
                                resolvable_symbols=["CodexEvidenceReviewer.review"])
    assert result["ok"] is True and result["reason_code"] == "G_AUTH_IO_OK"


# ---------------------------------------------------------------------------
# G-NO-STALE-ENTRYPOINT
# ---------------------------------------------------------------------------


def test_g_no_stale_red_unlisted_dispatch_non_canonical():
    result = gates.gate_no_stale_entrypoint(
        discovered_callables=[{"name": "SneakyAdapter.build", "effect": "dispatch",
                               "canonical": False, "protected": False, "predates_gates": False}],
        canonical_entrypoints=["GLMBuilder.build"])
    assert result["ok"] is False
    assert result["reason_code"] == "G_NO_STALE_UNLISTED_DISPATCH"


def test_g_no_stale_red_unlisted_dispatch_claims_canonical_but_not_allowlisted():
    result = gates.gate_no_stale_entrypoint(
        discovered_callables=[{"name": "GhostBuilder.build", "effect": "dispatch",
                               "canonical": True, "protected": True, "predates_gates": False}],
        canonical_entrypoints=["GLMBuilder.build"])
    assert result["ok"] is False
    assert result["reason_code"] == "G_NO_STALE_UNLISTED_DISPATCH"


def test_g_no_stale_red_legacy_unprotected():
    result = gates.gate_no_stale_entrypoint(
        discovered_callables=[{"name": "GLMBuilder.build", "effect": "dispatch",
                               "canonical": True, "protected": False, "predates_gates": True}],
        canonical_entrypoints=["GLMBuilder.build"])
    assert result["ok"] is False
    assert result["reason_code"] == "G_NO_STALE_LEGACY_UNPROTECTED"


def test_g_no_stale_red_duplicate_path():
    # a canonical, allowlisted path to "dispatch" already exists; a SECOND,
    # non-canonical callable reaches the same effect — a redundant path, not
    # a lone stray (that distinct shape is G_NO_STALE_UNLISTED_DISPATCH,
    # covered above), so this must report DUPLICATE_PATH specifically.
    result = gates.gate_no_stale_entrypoint(
        discovered_callables=[
            {"name": "ExecutionBackend.execute", "effect": "dispatch", "canonical": True,
             "protected": True, "predates_gates": False},
            {"name": "LegacyDirectSubprocess.run", "effect": "dispatch", "canonical": False,
             "protected": False, "predates_gates": False},
        ],
        canonical_entrypoints=["ExecutionBackend.execute"])
    assert result["ok"] is False
    assert result["reason_code"] == "G_NO_STALE_DUPLICATE_PATH"


def test_g_no_stale_green_single_canonical_protected_entrypoint():
    result = gates.gate_no_stale_entrypoint(
        discovered_callables=[{"name": "ExecutionBackend.execute", "effect": "dispatch",
                               "canonical": True, "protected": True, "predates_gates": False}],
        canonical_entrypoints=["ExecutionBackend.execute"])
    assert result["ok"] is True and result["reason_code"] == "G_NO_STALE_OK"


def test_g_no_stale_red_empty_discovered_callables_never_vacuously_passes():
    result = gates.gate_no_stale_entrypoint(discovered_callables=[],
                                            canonical_entrypoints=["ExecutionBackend.execute"])
    assert result["ok"] is False
    assert result["reason_code"] == "G_NO_STALE_UNLISTED_DISPATCH"


def test_g_no_stale_red_none_discovered_callables_never_vacuously_passes():
    result = gates.gate_no_stale_entrypoint(discovered_callables=None,
                                            canonical_entrypoints=["ExecutionBackend.execute"])
    assert result["ok"] is False
    assert result["reason_code"] == "G_NO_STALE_UNLISTED_DISPATCH"


def test_g_no_stale_red_empty_canonical_entrypoints_never_vacuously_passes():
    result = gates.gate_no_stale_entrypoint(
        discovered_callables=[{"name": "ExecutionBackend.execute", "effect": "dispatch",
                               "canonical": True, "protected": True, "predates_gates": False}],
        canonical_entrypoints=[])
    assert result["ok"] is False
    assert result["reason_code"] == "G_NO_STALE_UNLISTED_DISPATCH"


# ---------------------------------------------------------------------------
# G-SHA-BOUND-PROOF
# ---------------------------------------------------------------------------


def test_g_sha_bound_red_missing():
    result = gates.gate_sha_bound_proof(artifact={}, expected_candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_SHA_BOUND_MISSING"


def test_g_sha_bound_red_mismatch_mutated_between_write_and_consumption():
    result = gates.gate_sha_bound_proof(artifact={"candidate_tree": TREE, "proof_path": "evidence/x.json"},
                                        expected_candidate_tree=TREE, recomputed_candidate_tree=OTHER_TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_SHA_BOUND_MISMATCH"


def test_g_sha_bound_red_cross_candidate():
    result = gates.gate_sha_bound_proof(artifact={"candidate_tree": OTHER_TREE, "proof_path": "evidence/x.json"},
                                        expected_candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_SHA_BOUND_CROSS_CANDIDATE"


def test_g_sha_bound_green_exact_match():
    result = gates.gate_sha_bound_proof(artifact={"candidate_tree": TREE, "proof_path": "evidence/x.json"},
                                        expected_candidate_tree=TREE, recomputed_candidate_tree=TREE)
    assert result["ok"] is True and result["reason_code"] == "G_SHA_BOUND_OK"


def test_g_sha_bound_red_expected_candidate_tree_missing_never_vacuously_passes():
    # A missing expected_candidate_tree previously skipped the CROSS_CANDIDATE
    # check entirely — any artifact.candidate_tree would then pass, since
    # nothing was compared against. Must BLOCK instead.
    result = gates.gate_sha_bound_proof(artifact={"candidate_tree": TREE}, expected_candidate_tree=None)
    assert result["ok"] is False
    assert result["reason_code"] == "G_SHA_BOUND_MISSING"


def test_g_sha_bound_red_expected_candidate_tree_empty_string():
    result = gates.gate_sha_bound_proof(artifact={"candidate_tree": TREE}, expected_candidate_tree="")
    assert result["ok"] is False
    assert result["reason_code"] == "G_SHA_BOUND_MISSING"


# ---------------------------------------------------------------------------
# G-CANARY-FIRST
# ---------------------------------------------------------------------------


def test_g_canary_first_red_missing():
    result = gates.gate_canary_first(canary_required=True, canary_record=None, candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_CANARY_FIRST_MISSING"


def test_g_canary_first_red_failed():
    result = gates.gate_canary_first(canary_required=True,
                                     canary_record={"candidate_tree": TREE, "passed": False, "proof_path": "evidence/canary.json"},
                                     candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_CANARY_FIRST_FAILED"


def test_g_canary_first_red_stale_candidate():
    result = gates.gate_canary_first(canary_required=True,
                                     canary_record={"candidate_tree": OTHER_TREE, "passed": True, "proof_path": "evidence/canary.json"},
                                     candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_CANARY_FIRST_STALE_CANDIDATE"


def test_g_canary_first_green_not_required_skips():
    result = gates.gate_canary_first(canary_required=False, canary_record=None, candidate_tree=TREE)
    assert result["ok"] is True and result["reason_code"] == "G_CANARY_FIRST_NOT_REQUIRED"


def test_g_canary_first_green_exact_green_canary():
    result = gates.gate_canary_first(canary_required=True,
                                     canary_record={"candidate_tree": TREE, "passed": True,
                                                   "proof_path": "evidence/canary.json"},
                                     candidate_tree=TREE)
    assert result["ok"] is True and result["reason_code"] == "G_CANARY_FIRST_OK"


def test_g_canary_first_red_candidate_tree_missing_never_vacuously_matches_none():
    # Without a real candidate_tree, a canary_record with candidate_tree=None
    # would otherwise "match" by coincidence (None == None) — must BLOCK.
    result = gates.gate_canary_first(canary_required=True,
                                     canary_record={"candidate_tree": None, "passed": True},
                                     candidate_tree=None)
    assert result["ok"] is False
    assert result["reason_code"] == "G_CANARY_FIRST_MISSING"


# ---------------------------------------------------------------------------
# G-FROZEN-FINISH-LINE
# ---------------------------------------------------------------------------


_BINDINGS = {
    "AC-C8-001": {
        "allowed_paths": ["src/joao_orchestrator/bubble/gates.py", "tests/test_c8_gates.py"],
        "required_tests": ["tests/test_c8_gates.py::test_g_dbl_audit_green_normal_one_distinct_reviewer_passes"],
        "allowed_actions": ["modify", "create"],
    },
}


def _frozen_mission(**overrides) -> dict:
    base = gates.build_frozen_mission(spec_sha="s" * 40, roadmap_sha="r" * 40,
                                      authority_instruction_hash="h" * 64, risk_tier="critical",
                                      canary_required=False, forbidden_paths=["memory/lessons.jsonl"],
                                      criterion_bindings=_BINDINGS)
    base.update(overrides)
    return base


def test_g_frozen_finish_line_red_missing_risk_tier():
    result = gates.gate_frozen_finish_line(frozen_mission=_frozen_mission(risk_tier=None), changed_paths=[],
                                           candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert "risk_tier" in result["reason"]


def test_g_frozen_finish_line_red_forbidden_path():
    result = gates.gate_frozen_finish_line(
        frozen_mission=_frozen_mission(),
        changed_paths=[{"path": "memory/lessons.jsonl", "action": "modify"}], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert result["offending_path"] == "memory/lessons.jsonl"


def test_g_frozen_finish_line_red_scope_creep_unmapped_path_eighth_gate_style():
    # simulates "an 8th gate appearing" — a changed path with no covering AC at all
    result = gates.gate_frozen_finish_line(
        frozen_mission=_frozen_mission(),
        changed_paths=[{"path": "src/joao_orchestrator/bubble/eighth_gate.py", "action": "create"}],
        candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert result["offending_path"] == "src/joao_orchestrator/bubble/eighth_gate.py"


def test_g_frozen_finish_line_red_correction_without_acceptance_criterion_id():
    result = gates.gate_frozen_finish_line(
        frozen_mission=_frozen_mission(), changed_paths=[],
        corrections=[{"acceptance_criterion_id": None}], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert "acceptance_criterion_id" in result["reason"]


def test_g_frozen_finish_line_red_correction_requires_new_authority():
    result = gates.gate_frozen_finish_line(
        frozen_mission=_frozen_mission(), changed_paths=[],
        corrections=[{"acceptance_criterion_id": None, "out_of_scope_but_valid": True}],
        candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_REQUIRES_NEW_AUTHORITY"


def test_g_frozen_finish_line_red_required_test_missing_evidence():
    result = gates.gate_frozen_finish_line(
        frozen_mission=_frozen_mission(),
        changed_paths=[{"path": "src/joao_orchestrator/bubble/gates.py", "action": "modify"}],
        required_test_results={}, candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert "required_test" in result["reason"]


def test_g_frozen_finish_line_red_required_test_failed():
    test_id = _BINDINGS["AC-C8-001"]["required_tests"][0]
    result = gates.gate_frozen_finish_line(
        frozen_mission=_frozen_mission(),
        changed_paths=[{"path": "src/joao_orchestrator/bubble/gates.py", "action": "modify"}],
        required_test_results={test_id: {"passed": False, "candidate_tree": TREE}}, candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"


def test_g_frozen_finish_line_red_required_test_cross_candidate():
    test_id = _BINDINGS["AC-C8-001"]["required_tests"][0]
    result = gates.gate_frozen_finish_line(
        frozen_mission=_frozen_mission(),
        changed_paths=[{"path": "src/joao_orchestrator/bubble/gates.py", "action": "modify"}],
        required_test_results={test_id: {"passed": True, "candidate_tree": OTHER_TREE}}, candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"


def test_g_frozen_finish_line_red_spec_sha_missing():
    result = gates.gate_frozen_finish_line(frozen_mission=_frozen_mission(spec_sha=None), changed_paths=[],
                                           candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert result["missing_field"] == "spec_sha"


def test_g_frozen_finish_line_red_roadmap_sha_missing():
    result = gates.gate_frozen_finish_line(frozen_mission=_frozen_mission(roadmap_sha=""), changed_paths=[],
                                           candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert result["missing_field"] == "roadmap_sha"


def test_g_frozen_finish_line_red_authority_instruction_hash_missing():
    result = gates.gate_frozen_finish_line(frozen_mission=_frozen_mission(authority_instruction_hash=None),
                                           changed_paths=[], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert result["missing_field"] == "authority_instruction_hash"


def test_g_frozen_finish_line_red_criterion_bindings_missing():
    result = gates.gate_frozen_finish_line(frozen_mission=_frozen_mission(criterion_bindings={}),
                                           changed_paths=[], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert "criterion_bindings" in result["reason"]


def test_g_frozen_finish_line_red_ac_binding_with_empty_required_tests_never_vacuously_satisfiable():
    empty_required_tests_bindings = {
        "AC-C8-EMPTY": {
            "allowed_paths": ["src/joao_orchestrator/bubble/gates.py"],
            "required_tests": [],
            "allowed_actions": ["modify"],
        }
    }
    result = gates.gate_frozen_finish_line(
        frozen_mission=_frozen_mission(criterion_bindings=empty_required_tests_bindings), changed_paths=[],
        candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert result["offending_ac"] == "AC-C8-EMPTY"


def test_g_frozen_finish_line_green_mapped_path_and_tree_bound_passing_test():
    test_id = _BINDINGS["AC-C8-001"]["required_tests"][0]
    result = gates.gate_frozen_finish_line(
        frozen_mission=_frozen_mission(),
        changed_paths=[{"path": "src/joao_orchestrator/bubble/gates.py", "action": "modify"},
                      {"path": "tests/test_c8_gates.py", "action": "modify"}],
        corrections=[{"acceptance_criterion_id": "AC-C8-001"}],
        required_test_results={test_id: {"passed": True, "candidate_tree": TREE}}, candidate_tree=TREE)
    assert result["ok"] is True and result["reason_code"] == "G_FROZEN_FINISH_LINE_OK"
    assert result["touched_acceptance_criteria"] == ["AC-C8-001"]


# ===========================================================================
# Correction loop 3 — one adversarial test per audited fail-open, each
# reproducing the counter-audit's EXACT repro case (red before the fix in
# gates.py, green after). Findings #1-#6; #7 lives in
# tests/test_g_hermetic_self_check.py (it is a conftest/fixture defect).
# ===========================================================================


def test_c8a_l3_finding1_verdict_with_no_identity_fields_is_malformed_not_accepted():
    # REPRO (was: ok=True G_DBL_AUDIT_OK with providers=[None]) — a verdict
    # carrying no provider/provider_family/model at all was counted as a
    # valid independent reviewer.
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider="zai-coding-plan", builder_family="zai",
        reviewer_verdicts=[{"ok": True, "decision": "pass", "candidate_tree": TREE}],
        candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_MALFORMED_VERDICT"
    assert set(result["missing_fields"]) == {"provider", "provider_family", "model"}


@pytest.mark.parametrize("missing_field", ["provider", "provider_family", "model"])
def test_c8a_l3_finding1_each_identity_field_is_individually_required(missing_field):
    verdict = _verdict("codex-subscription", "openai")
    verdict.pop(missing_field)
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider="zai-coding-plan", builder_family="zai",
        reviewer_verdicts=[verdict], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_MALFORMED_VERDICT"
    assert result["missing_fields"] == [missing_field]


@pytest.mark.parametrize("blank", ["", "   ", None, 123])
def test_c8a_l3_finding1_blank_or_non_string_identity_is_also_malformed(blank):
    verdict = _verdict("codex-subscription", "openai")
    verdict["provider"] = blank
    result = gates.gate_dbl_audit(
        risk_tier="normal", builder_provider="zai-coding-plan", builder_family="zai",
        reviewer_verdicts=[verdict], candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_MALFORMED_VERDICT"


def test_c8a_l3_finding2_codex_pass_claude_pass_mistral_block_is_a_disagreement():
    # REPRO (was: ok=True G_DBL_AUDIT_OK) — the dissenting BLOCK verdict was
    # silently filtered out, so a correct distinct-family ACCEPT count
    # out-voted it. D4 forbids any automatic tie-break.
    result = gates.gate_dbl_audit(
        risk_tier="critical", builder_provider="zai-coding-plan", builder_family="zai",
        reviewer_verdicts=[_verdict("codex-subscription", "openai"),
                           _verdict("claude-cli", "anthropic"),
                           _verdict("mistral-cli", "mistral", ok=False, decision="block")],
        candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_REVIEWER_DISAGREEMENT"
    assert result["dissenting_providers"] == ["mistral-cli"]


@pytest.mark.parametrize("decision,ok", [("block", False), ("p1", False), ("p1", True)])
def test_c8a_l3_finding2_any_dissent_shape_blocks_a_critical_run(decision, ok):
    result = gates.gate_dbl_audit(
        risk_tier="critical", builder_provider="zai-coding-plan", builder_family="zai",
        reviewer_verdicts=[_verdict("codex-subscription", "openai"),
                           _verdict("claude-cli", "anthropic"),
                           _verdict("mistral-cli", "mistral", ok=ok, decision=decision)],
        candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_REVIEWER_DISAGREEMENT"


def test_c8a_l3_finding2_dissent_cannot_be_outvoted_by_adding_more_accepts():
    # The dissent check runs BEFORE any counting, so piling on ACCEPTs can
    # never drown out a single BLOCK.
    result = gates.gate_dbl_audit(
        risk_tier="critical", builder_provider="zai-coding-plan", builder_family="zai",
        reviewer_verdicts=[_verdict("codex-subscription", "openai"),
                           _verdict("claude-cli", "anthropic"),
                           _verdict("gemini-cli", "google"),
                           _verdict("mistral-cli", "mistral", ok=False, decision="block")],
        candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_REVIEWER_DISAGREEMENT"


def test_c8a_l3_finding2_unanimous_critical_still_passes():
    # GREEN contrast: no dissent present -> the tier's normal rules apply.
    result = gates.gate_dbl_audit(
        risk_tier="critical", builder_provider="zai-coding-plan", builder_family="zai",
        reviewer_verdicts=[_verdict("codex-subscription", "openai"),
                           _verdict("claude-cli", "anthropic")],
        candidate_tree=TREE)
    assert result["ok"] is True and result["reason_code"] == "G_DBL_AUDIT_OK"


def test_c8a_l3_finding3_frozen_finish_line_none_tree_no_longer_self_matches():
    # REPRO (was: ok=True G_FROZEN_FINISH_LINE_OK) — candidate_tree=None and
    # evidence candidate_tree=None matched each other by coincidence.
    frozen = gates.build_frozen_mission(
        spec_sha="s" * 40, roadmap_sha="r" * 40, authority_instruction_hash="h" * 64,
        risk_tier="normal", canary_required=False, forbidden_paths=[],
        criterion_bindings={"AC-1": {"allowed_paths": ["src/x.py"],
                                     "required_tests": ["t::a"],
                                     "allowed_actions": ["modify"]}})
    result = gates.gate_frozen_finish_line(
        frozen_mission=frozen, changed_paths=[{"path": "src/x.py", "action": "modify"}],
        required_test_results={"t::a": {"passed": True, "candidate_tree": None}},
        candidate_tree=None)
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert "candidate_tree" in result["reason"]


def test_c8a_l3_finding3_frozen_finish_line_empty_string_tree_also_blocks():
    result = gates.gate_frozen_finish_line(frozen_mission=_frozen_mission(), changed_paths=[],
                                           candidate_tree="")
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert "candidate_tree" in result["reason"]


def test_c8a_l3_finding4_hermetic_missing_audit_journal_blocks():
    # REPRO (was: ok=True G_HERMETIC_OK) — `touches or []` made an absent
    # journal indistinguishable from a clean one.
    result = gates.gate_hermetic(touches=None)
    assert result["ok"] is False
    assert result["reason_code"] == "G_HERMETIC_MISSING_AUDIT_JOURNAL"


def test_c8a_l3_finding4_hermetic_empty_journal_is_still_a_clean_pass():
    # GREEN contrast: an EXPLICITLY empty journal means the auditor ran and
    # saw nothing — that remains a pass, distinct from `None`.
    result = gates.gate_hermetic(touches=[])
    assert result["ok"] is True and result["reason_code"] == "G_HERMETIC_OK"


def test_c8a_l3_finding5_callable_without_effect_field_is_malformed_inventory():
    # REPRO (was: ok=True G_NO_STALE_OK) — a record with no `effect` key was
    # grouped under None and passed because it was otherwise canonical=True.
    result = gates.gate_no_stale_entrypoint(
        discovered_callables=[{"name": "X.run", "canonical": True,
                               "protected": True, "predates_gates": False}],
        canonical_entrypoints=["X.run"])
    assert result["ok"] is False
    assert result["reason_code"] == "G_NO_STALE_UNLISTED_DISPATCH"
    assert result["missing_fields"] == ["effect"]


def test_c8a_l3_finding5_callable_without_name_field_is_malformed_inventory():
    result = gates.gate_no_stale_entrypoint(
        discovered_callables=[{"effect": "dispatch", "canonical": True,
                               "protected": True, "predates_gates": False}],
        canonical_entrypoints=["X.run"])
    assert result["ok"] is False
    assert result["reason_code"] == "G_NO_STALE_UNLISTED_DISPATCH"
    assert result["missing_fields"] == ["name"]


def test_c8a_l3_finding5_non_dict_inventory_entry_is_malformed():
    result = gates.gate_no_stale_entrypoint(
        discovered_callables=["ExecutionBackend.execute"], canonical_entrypoints=["ExecutionBackend.execute"])
    assert result["ok"] is False
    assert result["reason_code"] == "G_NO_STALE_UNLISTED_DISPATCH"


@pytest.mark.parametrize("bad_name", ["", "   ", None, 7])
def test_c8a_l3_finding6_auth_io_requires_a_real_gate_name(bad_name):
    # REPRO (was: ok=True G_AUTH_IO_OK with gate_name="") — a verdict was
    # attributed to an unnamed gate.
    result = gates.gate_auth_io(gate_name=bad_name, required_entrypoint_symbols=["A.b"],
                                referenced_symbols=["A.b"], resolvable_symbols=["A.b"])
    assert result["ok"] is False
    assert result["reason_code"] == "G_AUTH_IO_HELPER_ONLY_COVERAGE"
    assert "gate_name" in result["reason"]


def test_c8a_l3_finding6_auth_io_named_gate_still_passes():
    result = gates.gate_auth_io(gate_name="G-AUTH-IO", required_entrypoint_symbols=["A.b"],
                                referenced_symbols=["A.b"], resolvable_symbols=["A.b"])
    assert result["ok"] is True and result["reason_code"] == "G_AUTH_IO_OK"


# ===========================================================================
# Correction loop 4 (massive closeout, Phase 1) — the 13 explicitly required
# G-DBL-AUDIT cases, asserted against the mandated deterministic precedence:
#   1 structure/identity of EVERY verdict -> 2 explicit negative ->
#   3 exact total cardinality -> 4 accepted cardinality ->
#   5 builder independence -> 6 provider family.
# ONE shared disagreement reason code across both tiers.
# ===========================================================================

_L4_BUILDER = dict(builder_provider="anthropic-claude", builder_family="anthropic")


def _l4(provider, family, decision="pass", ok=True, tree=TREE, **overrides):
    verdict = {"provider": provider, "provider_family": family, "model": "m",
               "ok": ok, "decision": decision, "candidate_tree": tree}
    verdict.update(overrides)
    return verdict


def _l4_gate(tier, verdicts, tree=TREE):
    return gates.gate_dbl_audit(risk_tier=tier, reviewer_verdicts=verdicts,
                                candidate_tree=tree, **_L4_BUILDER)


# --- normal tier -----------------------------------------------------------

def test_l4_normal_one_pass_is_accepted():
    result = _l4_gate("normal", [_l4("codex-subscription", "openai")])
    assert result["ok"] is True and result["reason_code"] == "G_DBL_AUDIT_OK"


def test_l4_normal_pass_plus_block_is_disagreement_not_a_silent_filter():
    result = _l4_gate("normal", [_l4("codex-subscription", "openai"),
                                 _l4("glm", "zai", decision="block", ok=False)])
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_REVIEWER_DISAGREEMENT"
    assert result["dissenting_providers"] == ["glm"]


def test_l4_normal_pass_plus_p1_is_also_disagreement():
    result = _l4_gate("normal", [_l4("codex-subscription", "openai"),
                                 _l4("glm", "zai", decision="p1", ok=False)])
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_REVIEWER_DISAGREEMENT"


def test_l4_normal_two_pass_is_too_many_reviewers():
    result = _l4_gate("normal", [_l4("codex-subscription", "openai"),
                                 _l4("glm", "zai")])
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_TOO_MANY_REVIEWERS"
    assert result["total_count"] == 2 and result["required"] == 1


def test_l4_normal_malformed_reviewer_identity_blocks():
    result = _l4_gate("normal", [{"ok": True, "decision": "pass", "candidate_tree": TREE}])
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_MALFORMED_VERDICT"


@pytest.mark.parametrize("field", ["provider", "provider_family", "model"])
def test_l4_normal_each_missing_identity_field_blocks(field):
    verdict = _l4("codex-subscription", "openai")
    verdict.pop(field)
    result = _l4_gate("normal", [verdict])
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_MALFORMED_VERDICT"
    assert result["missing_fields"] == [field]


def test_l4_normal_zero_verdicts_blocks():
    result = _l4_gate("normal", [])
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_INSUFFICIENT_REVIEWERS"


# --- critical tier ---------------------------------------------------------

def test_l4_critical_two_valid_pass_is_accepted():
    result = _l4_gate("critical", [_l4("codex-subscription", "openai"), _l4("glm", "zai")])
    assert result["ok"] is True and result["reason_code"] == "G_DBL_AUDIT_OK"


def test_l4_critical_pass_plus_block_is_disagreement():
    result = _l4_gate("critical", [_l4("codex-subscription", "openai"),
                                   _l4("glm", "zai", decision="block", ok=False)])
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_REVIEWER_DISAGREEMENT"


def test_l4_critical_two_pass_plus_one_block_is_disagreement_negative_takes_precedence():
    # The negative verdict outranks the over-cardinality: step 2 precedes step 3.
    result = _l4_gate("critical", [_l4("codex-subscription", "openai"), _l4("glm", "zai"),
                                   _l4("mistral-cli", "mistral", decision="block", ok=False)])
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_REVIEWER_DISAGREEMENT"


def test_l4_critical_three_pass_is_too_many_reviewers():
    result = _l4_gate("critical", [_l4("codex-subscription", "openai"), _l4("glm", "zai"),
                                   _l4("mistral-cli", "mistral")])
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_TOO_MANY_REVIEWERS"
    assert result["total_count"] == 3 and result["required"] == 2


# --- candidate_tree --------------------------------------------------------

def test_l4_candidate_tree_missing_blocks():
    result = _l4_gate("normal", [_l4("codex-subscription", "openai", tree=None)], tree=None)
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_INSUFFICIENT_REVIEWERS"
    assert "candidate_tree" in result["reason"]


def test_l4_candidate_tree_mismatch_blocks():
    result = _l4_gate("normal", [_l4("codex-subscription", "openai", tree=OTHER_TREE)])
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_TREE_MISMATCH"


# --- the shared reason code + no-silent-filter guarantees ------------------

def test_l4_disagreement_uses_one_shared_reason_code_across_both_tiers():
    normal = _l4_gate("normal", [_l4("codex-subscription", "openai"),
                                 _l4("glm", "zai", decision="block", ok=False)])
    critical = _l4_gate("critical", [_l4("codex-subscription", "openai"),
                                     _l4("glm", "zai", decision="block", ok=False)])
    assert normal["reason_code"] == critical["reason_code"] == "G_DBL_AUDIT_REVIEWER_DISAGREEMENT"


def test_l4_a_negative_verdict_is_never_removed_before_the_decision():
    # A lone BLOCK must NOT be filtered down to "zero verdicts" and reported
    # as an under-count — it is a disagreement, and it must be named.
    result = _l4_gate("normal", [_l4("codex-subscription", "openai", decision="block", ok=False)])
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_REVIEWER_DISAGREEMENT"
    assert result["dissenting_providers"] == ["codex-subscription"]


def test_l4_ok_true_with_block_decision_is_still_a_dissent():
    # An adapter claiming ok=True while its decision says block must never be
    # counted as an ACCEPT.
    result = _l4_gate("normal", [_l4("codex-subscription", "openai", decision="block", ok=True)])
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_REVIEWER_DISAGREEMENT"


def test_l4_invalid_decision_value_is_malformed():
    result = _l4_gate("normal", [_l4("codex-subscription", "openai", decision="approved")])
    assert result["ok"] is False
    assert result["reason_code"] == "G_DBL_AUDIT_MALFORMED_VERDICT"


# ===========================================================================
# Correction loop 4, Phase 4 — TABLE-DRIVEN MALFORMED-INPUT MATRIX
#
# One reusable table covering the whole malformed-input CLASS across all seven
# gates, not just the individual payloads named in the audit. Every row asserts
# the same four properties: no exception escapes, decision == "block",
# ok is False, and a stable non-empty reason_code. Rows marked from the GPT
# report are called out explicitly.
# ===========================================================================

_M_TREE = "c" * 40
_M_OTHER = "d" * 40


def _mv(**over):
    verdict = {"provider": "acme-rev", "provider_family": "acme", "model": "acme-m",
               "ok": True, "decision": "pass", "candidate_tree": _M_TREE}
    verdict.update(over)
    return verdict


def _mfm(**over):
    frozen = gates.build_frozen_mission(
        spec_sha="s" * 40, roadmap_sha="r" * 40, authority_instruction_hash="h" * 64,
        risk_tier="normal", canary_required=False, forbidden_paths=["memory/lessons.jsonl"],
        criterion_bindings={"AC-M": {"allowed_paths": ["src/m.py"], "required_tests": ["t::m"],
                                     "allowed_actions": ["modify"]}})
    frozen.update(over)
    return frozen


_M_BUILDER = dict(builder_provider="anthropic-claude", builder_family="anthropic")
_M_CHANGED = [{"path": "src/m.py", "action": "modify"}]
_M_EVIDENCE = {"t::m": {"passed": True, "candidate_tree": _M_TREE}}


def _dbl(**over):
    kwargs = dict(risk_tier="normal", reviewer_verdicts=[_mv()], candidate_tree=_M_TREE, **_M_BUILDER)
    kwargs.update(over)
    return lambda: gates.gate_dbl_audit(**kwargs)


def _herm(touches):
    return lambda: gates.gate_hermetic(touches=touches)


def _auth(**over):
    kwargs = dict(gate_name="G-X", required_entrypoint_symbols=["A.b"],
                  referenced_symbols=["A.b"], resolvable_symbols=["A.b"])
    kwargs.update(over)
    return lambda: gates.gate_auth_io(**kwargs)


def _nostale(**over):
    kwargs = dict(discovered_callables=[{"name": "E.x", "effect": "dispatch", "canonical": True,
                                         "protected": True, "predates_gates": False}],
                  canonical_entrypoints=["E.x"])
    kwargs.update(over)
    return lambda: gates.gate_no_stale_entrypoint(**kwargs)


def _sha(**over):
    kwargs = dict(artifact={"candidate_tree": _M_TREE, "proof_path": "evidence/p.json"},
                  expected_candidate_tree=_M_TREE)
    kwargs.update(over)
    return lambda: gates.gate_sha_bound_proof(**kwargs)


def _canary(**over):
    kwargs = dict(canary_required=True,
                  canary_record={"candidate_tree": _M_TREE, "passed": True, "proof_path": "evidence/c.json"},
                  candidate_tree=_M_TREE)
    kwargs.update(over)
    return lambda: gates.gate_canary_first(**kwargs)


def _frozen(**over):
    kwargs = dict(frozen_mission=_mfm(), changed_paths=_M_CHANGED,
                  required_test_results=_M_EVIDENCE, candidate_tree=_M_TREE)
    kwargs.update(over)
    return lambda: gates.gate_frozen_finish_line(**kwargs)


# (label, callable) — every one must BLOCK without raising.
MALFORMED_INPUT_MATRIX = [
    # ---- G-DBL-AUDIT ----
    ("dbl: risk_tier None",                 _dbl(risk_tier=None)),
    ("dbl: risk_tier invalid string",       _dbl(risk_tier="urgent")),
    ("dbl: risk_tier wrong type",           _dbl(risk_tier=1)),
    ("dbl: candidate_tree None",            _dbl(candidate_tree=None, reviewer_verdicts=[_mv(candidate_tree=None)])),
    ("dbl: candidate_tree blank [GPT]",     _dbl(candidate_tree=" ", reviewer_verdicts=[_mv(candidate_tree=" ")])),
    ("dbl: candidate_tree wrong type",      _dbl(candidate_tree=7, reviewer_verdicts=[_mv(candidate_tree=7)])),
    ("dbl: builder_provider blank [GPT]",   _dbl(builder_provider=" ")),
    ("dbl: builder_provider None",          _dbl(builder_provider=None)),
    ("dbl: builder_family blank",           _dbl(builder_family="   ")),
    ("dbl: verdicts is a string",           _dbl(reviewer_verdicts="nope")),
    ("dbl: verdicts is a mapping",          _dbl(reviewer_verdicts={"a": 1})),
    ("dbl: verdict element None",           _dbl(reviewer_verdicts=[None])),
    ("dbl: verdict element int",            _dbl(reviewer_verdicts=[7])),
    ("dbl: verdict missing provider",       _dbl(reviewer_verdicts=[{k: v for k, v in _mv().items() if k != "provider"}])),
    ("dbl: verdict blank provider",         _dbl(reviewer_verdicts=[_mv(provider="  ")])),
    ("dbl: verdict provider is int",        _dbl(reviewer_verdicts=[_mv(provider=3)])),
    ("dbl: verdict ok='true' [GPT]",        _dbl(reviewer_verdicts=[_mv(ok="true")])),
    ("dbl: verdict ok=1",                   _dbl(reviewer_verdicts=[_mv(ok=1)])),
    ("dbl: verdict ok=None",                _dbl(reviewer_verdicts=[_mv(ok=None)])),
    ("dbl: verdict decision invalid",       _dbl(reviewer_verdicts=[_mv(decision="approved")])),
    ("dbl: verdict decision None",          _dbl(reviewer_verdicts=[_mv(decision=None)])),
    ("dbl: verdict tree mismatch",          _dbl(reviewer_verdicts=[_mv(candidate_tree=_M_OTHER)])),
    ("dbl: verdict tree blank",             _dbl(reviewer_verdicts=[_mv(candidate_tree="  ")])),
    # ---- G-HERMETIC ----
    ("hermetic: touches None",              _herm(None)),
    ("hermetic: touches [None] [GPT]",      _herm([None])),
    ("hermetic: touches is a string",       _herm("nope")),
    ("hermetic: touches [int]",             _herm([7])),
    ("hermetic: touch missing root",        _herm([{"path": "/x", "injected": False}])),
    ("hermetic: touch blank root",          _herm([{"root": " ", "path": "/x", "injected": False}])),
    ("hermetic: touch path is int",         _herm([{"root": "real_memory", "path": 5, "injected": False}])),
    ("hermetic: injected='false' [GPT]",    _herm([{"root": "real_memory", "path": "/x", "injected": "false"}])),
    ("hermetic: injected=1",                _herm([{"root": "real_memory", "path": "/x", "injected": 1}])),
    ("hermetic: injected=None",             _herm([{"root": "real_memory", "path": "/x", "injected": None}])),
    # ---- G-AUTH-IO ----
    ("auth_io: gate_name blank",            _auth(gate_name="   ")),
    ("auth_io: gate_name None",             _auth(gate_name=None)),
    ("auth_io: gate_name int",              _auth(gate_name=5)),
    ("auth_io: required has int [GPT]",     _auth(required_entrypoint_symbols=["A.b", 7])),
    ("auth_io: required has None",          _auth(required_entrypoint_symbols=["A.b", None])),
    ("auth_io: required has blank",         _auth(required_entrypoint_symbols=["A.b", "  "])),
    ("auth_io: referenced has int",         _auth(referenced_symbols=["A.b", 7])),
    ("auth_io: resolvable has int",         _auth(resolvable_symbols=["A.b", 7])),
    ("auth_io: required is a string",       _auth(required_entrypoint_symbols="A.b")),
    ("auth_io: no required symbols",        _auth(required_entrypoint_symbols=[])),
    # ---- G-NO-STALE-ENTRYPOINT ----
    ("no_stale: inventory None",            _nostale(discovered_callables=None)),
    ("no_stale: inventory empty",           _nostale(discovered_callables=[])),
    ("no_stale: allowlist empty",           _nostale(canonical_entrypoints=[])),
    ("no_stale: allowlist has int",         _nostale(canonical_entrypoints=["E.x", 7])),
    ("no_stale: element None",              _nostale(discovered_callables=[None])),
    ("no_stale: element is a string",       _nostale(discovered_callables=["E.x"])),
    ("no_stale: missing effect",            _nostale(discovered_callables=[{"name": "E.x", "canonical": True, "protected": True, "predates_gates": False}])),
    ("no_stale: canonical='false' [GPT]",   _nostale(discovered_callables=[{"name": "E.x", "effect": "dispatch", "canonical": "false", "protected": "false", "predates_gates": False}])),
    ("no_stale: protected='false' [GPT]",   _nostale(discovered_callables=[{"name": "E.x", "effect": "dispatch", "canonical": True, "protected": "false", "predates_gates": False}])),
    ("no_stale: protected=1",               _nostale(discovered_callables=[{"name": "E.x", "effect": "dispatch", "canonical": True, "protected": 1, "predates_gates": False}])),
    ("no_stale: unprotected current [GPT]", _nostale(discovered_callables=[{"name": "new", "effect": "dispatch", "canonical": True, "protected": False, "predates_gates": False}], canonical_entrypoints=["new"])),
    # ---- G-SHA-BOUND-PROOF ----
    ("sha: expected tree None",             _sha(expected_candidate_tree=None)),
    ("sha: expected tree blank",            _sha(expected_candidate_tree="  ")),
    ("sha: artifact None",                  _sha(artifact=None)),
    ("sha: artifact is a list",             _sha(artifact=[])),
    ("sha: artifact is a string",           _sha(artifact="proof")),
    ("sha: artifact tree missing",          _sha(artifact={"proof_path": "p"})),
    ("sha: artifact tree blank",            _sha(artifact={"candidate_tree": "  ", "proof_path": "p"})),
    ("sha: proof_path missing [GPT]",       _sha(artifact={"candidate_tree": _M_TREE})),
    ("sha: proof_path blank [GPT]",         _sha(artifact={"candidate_tree": _M_TREE, "proof_path": " "})),
    ("sha: cross candidate",                _sha(artifact={"candidate_tree": _M_OTHER, "proof_path": "p"})),
    # ---- G-CANARY-FIRST ----
    ("canary: required='true'",             _canary(canary_required="true")),
    ("canary: required=1",                  _canary(canary_required=1)),
    ("canary: required=None",               _canary(canary_required=None)),
    ("canary: record None",                 _canary(canary_record=None)),
    ("canary: record is a list",            _canary(canary_record=[])),
    ("canary: passed='false' [GPT]",        _canary(canary_record={"candidate_tree": _M_TREE, "passed": "false", "proof_path": "p"})),
    ("canary: passed='true' [GPT]",         _canary(canary_record={"candidate_tree": _M_TREE, "passed": "true", "proof_path": "p"})),
    ("canary: passed=1 [GPT]",              _canary(canary_record={"candidate_tree": _M_TREE, "passed": 1, "proof_path": "p"})),
    ("canary: missing proof_path [GPT]",    _canary(canary_record={"candidate_tree": _M_TREE, "passed": True})),
    ("canary: blank proof_path [GPT]",      _canary(canary_record={"candidate_tree": _M_TREE, "passed": True, "proof_path": "  "})),
    ("canary: record tree missing [GPT]",   _canary(canary_record={"passed": True, "proof_path": "p"})),
    ("canary: record tree blank [GPT]",     _canary(canary_record={"candidate_tree": " ", "passed": True, "proof_path": "p"})),
    ("canary: candidate_tree blank",        _canary(candidate_tree="  ")),
    # ---- G-FROZEN-FINISH-LINE ----
    ("frozen: candidate_tree None",         _frozen(candidate_tree=None)),
    ("frozen: candidate_tree blank [GPT]",  _frozen(candidate_tree="  ")),
    ("frozen: mission None",                _frozen(frozen_mission=None)),
    ("frozen: mission is a list",           _frozen(frozen_mission=[])),
    ("frozen: spec_sha blank",              _frozen(frozen_mission=_mfm(spec_sha="  "))),
    ("frozen: roadmap_sha None",            _frozen(frozen_mission=_mfm(roadmap_sha=None))),
    ("frozen: authority hash blank",        _frozen(frozen_mission=_mfm(authority_instruction_hash=" "))),
    ("frozen: bindings empty",              _frozen(frozen_mission=_mfm(criterion_bindings={}))),
    ("frozen: bindings is a list",          _frozen(frozen_mission=_mfm(criterion_bindings=[]))),
    ("frozen: changed_paths [None] [GPT]",  _frozen(changed_paths=[None])),
    ("frozen: changed_paths is a string",   _frozen(changed_paths="src/m.py")),
    ("frozen: changed_paths [int]",         _frozen(changed_paths=[7])),
    ("frozen: changed path blank",          _frozen(changed_paths=[{"path": " ", "action": "modify"}])),
    ("frozen: changed action missing",      _frozen(changed_paths=[{"path": "src/m.py"}])),
    ("frozen: results is a list",           _frozen(required_test_results=[])),
    ("frozen: evidence None",               _frozen(required_test_results={"t::m": None})),
    ("frozen: passed='false' [GPT]",        _frozen(required_test_results={"t::m": {"passed": "false", "candidate_tree": _M_TREE}})),
    ("frozen: passed='true' [GPT]",         _frozen(required_test_results={"t::m": {"passed": "true", "candidate_tree": _M_TREE}})),
    ("frozen: passed=1",                    _frozen(required_test_results={"t::m": {"passed": 1, "candidate_tree": _M_TREE}})),
    ("frozen: evidence tree blank",         _frozen(required_test_results={"t::m": {"passed": True, "candidate_tree": " "}})),
    ("frozen: evidence tree missing",       _frozen(required_test_results={"t::m": {"passed": True}})),
    ("frozen: forbidden_paths [None]",      _frozen(frozen_mission=_mfm(forbidden_paths=[None]))),
]


@pytest.mark.parametrize("label,call", MALFORMED_INPUT_MATRIX, ids=[row[0] for row in MALFORMED_INPUT_MATRIX])
def test_malformed_input_matrix_always_blocks_without_raising(label, call):
    try:
        result = call()
    except Exception as exc:  # noqa: BLE001 — the whole point is that NOTHING escapes
        raise AssertionError(
            f"{label}: malformed input escaped as {type(exc).__name__}: {exc} — "
            "a gate must return BLOCK, never raise, on contract-invalid input"
        ) from exc
    assert isinstance(result, dict), f"{label}: gate did not return a result mapping"
    assert result["ok"] is False, f"{label}: malformed input produced ok={result['ok']!r} (accidental PASS)"
    assert result["decision"] == "block", f"{label}: decision={result['decision']!r}"
    assert isinstance(result.get("reason_code"), str) and result["reason_code"].strip(), \
        f"{label}: missing/blank reason_code"
    assert result["reason_code"].startswith("G_"), f"{label}: unstable reason_code {result['reason_code']!r}"


def test_malformed_input_matrix_is_comprehensive():
    """Guard against the matrix silently shrinking, and prove every gate is covered."""
    assert len(MALFORMED_INPUT_MATRIX) >= 90
    prefixes = {row[0].split(":")[0] for row in MALFORMED_INPUT_MATRIX}
    assert prefixes == {"dbl", "hermetic", "auth_io", "no_stale", "sha", "canary", "frozen"}
    gpt_rows = [row[0] for row in MALFORMED_INPUT_MATRIX if "[GPT]" in row[0]]
    assert len(gpt_rows) >= 18, f"every payload named in the GPT report must be present, got {len(gpt_rows)}"


# --- positive controls: the valid shapes must still PASS --------------------

def test_positive_control_valid_normal_review_passes():
    assert gates.gate_dbl_audit(risk_tier="normal", reviewer_verdicts=[_mv()],
                                candidate_tree=_M_TREE, **_M_BUILDER)["ok"] is True


def test_positive_control_valid_critical_two_reviews_pass():
    result = gates.gate_dbl_audit(risk_tier="critical",
                                  reviewer_verdicts=[_mv(), _mv(provider="zeta-rev", provider_family="zeta")],
                                  candidate_tree=_M_TREE, **_M_BUILDER)
    assert result["ok"] is True


def test_positive_control_valid_canary_passes():
    assert _canary()()["ok"] is True


def test_positive_control_valid_frozen_mission_passes():
    assert _frozen()()["ok"] is True


def test_positive_control_valid_protected_entrypoint_inventory_passes():
    assert _nostale()()["ok"] is True


def test_positive_control_valid_sha_bound_proof_passes():
    assert _sha()()["ok"] is True


def test_positive_control_valid_hermetic_journal_passes():
    assert gates.gate_hermetic(touches=[])["ok"] is True
    assert gates.gate_hermetic(touches=[{"root": "external_ledger", "path": "/tmp/x", "injected": True}])["ok"] is True


def test_positive_control_valid_auth_io_passes():
    assert _auth()()["ok"] is True
