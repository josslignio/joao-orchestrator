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
    result = gates.gate_dbl_audit(risk_tier="normal", builder_provider="zai-coding-plan",
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
    result = gates.gate_sha_bound_proof(artifact={"candidate_tree": TREE}, expected_candidate_tree=TREE,
                                        recomputed_candidate_tree=OTHER_TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_SHA_BOUND_MISMATCH"


def test_g_sha_bound_red_cross_candidate():
    result = gates.gate_sha_bound_proof(artifact={"candidate_tree": OTHER_TREE}, expected_candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_SHA_BOUND_CROSS_CANDIDATE"


def test_g_sha_bound_green_exact_match():
    result = gates.gate_sha_bound_proof(artifact={"candidate_tree": TREE}, expected_candidate_tree=TREE,
                                        recomputed_candidate_tree=TREE)
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
                                     canary_record={"candidate_tree": TREE, "passed": False},
                                     candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_CANARY_FIRST_FAILED"


def test_g_canary_first_red_stale_candidate():
    result = gates.gate_canary_first(canary_required=True,
                                     canary_record={"candidate_tree": OTHER_TREE, "passed": True},
                                     candidate_tree=TREE)
    assert result["ok"] is False
    assert result["reason_code"] == "G_CANARY_FIRST_STALE_CANDIDATE"


def test_g_canary_first_green_not_required_skips():
    result = gates.gate_canary_first(canary_required=False, canary_record=None, candidate_tree=TREE)
    assert result["ok"] is True and result["reason_code"] == "G_CANARY_FIRST_NOT_REQUIRED"


def test_g_canary_first_green_exact_green_canary():
    result = gates.gate_canary_first(canary_required=True,
                                     canary_record={"candidate_tree": TREE, "passed": True},
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
    result = gates.gate_frozen_finish_line(frozen_mission=_frozen_mission(risk_tier=None), changed_paths=[])
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"


def test_g_frozen_finish_line_red_forbidden_path():
    result = gates.gate_frozen_finish_line(
        frozen_mission=_frozen_mission(),
        changed_paths=[{"path": "memory/lessons.jsonl", "action": "modify"}])
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"


def test_g_frozen_finish_line_red_scope_creep_unmapped_path_eighth_gate_style():
    # simulates "an 8th gate appearing" — a changed path with no covering AC at all
    result = gates.gate_frozen_finish_line(
        frozen_mission=_frozen_mission(),
        changed_paths=[{"path": "src/joao_orchestrator/bubble/eighth_gate.py", "action": "create"}])
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"


def test_g_frozen_finish_line_red_correction_without_acceptance_criterion_id():
    result = gates.gate_frozen_finish_line(
        frozen_mission=_frozen_mission(), changed_paths=[],
        corrections=[{"acceptance_criterion_id": None}])
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"


def test_g_frozen_finish_line_red_correction_requires_new_authority():
    result = gates.gate_frozen_finish_line(
        frozen_mission=_frozen_mission(), changed_paths=[],
        corrections=[{"acceptance_criterion_id": None, "out_of_scope_but_valid": True}])
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
    result = gates.gate_frozen_finish_line(frozen_mission=_frozen_mission(spec_sha=None), changed_paths=[])
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert result["missing_field"] == "spec_sha"


def test_g_frozen_finish_line_red_roadmap_sha_missing():
    result = gates.gate_frozen_finish_line(frozen_mission=_frozen_mission(roadmap_sha=""), changed_paths=[])
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert result["missing_field"] == "roadmap_sha"


def test_g_frozen_finish_line_red_authority_instruction_hash_missing():
    result = gates.gate_frozen_finish_line(frozen_mission=_frozen_mission(authority_instruction_hash=None),
                                           changed_paths=[])
    assert result["ok"] is False
    assert result["reason_code"] == "G_FROZEN_FINISH_LINE_SCOPE_CREEP"
    assert result["missing_field"] == "authority_instruction_hash"


def test_g_frozen_finish_line_red_criterion_bindings_missing():
    result = gates.gate_frozen_finish_line(frozen_mission=_frozen_mission(criterion_bindings={}),
                                           changed_paths=[])
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
        frozen_mission=_frozen_mission(criterion_bindings=empty_required_tests_bindings), changed_paths=[])
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
