"""C-8 gates: the 7 C-8 lessons as pure, fail-closed gate functions.

Phase C8-A (`JOAO_C8_GATE_CONTRACTS.md` v4, `JOAO_C8_GATES_ROADMAP.md` LOT C8-A):
exactly seven gates, each a pure function of its inputs, returning the shared
contract shape `{"ok": bool, "decision": "pass"|"block", "reason_code": str,
"reason": str, "candidate_tree": str|None, ...}` — the same vocabulary as the
existing adapters (`execution_backend.py` `PREFLIGHT_UNAVAILABLE`,
`reviewer_contract.py` `"decision": "block"`).

Fail-closed: any doubt, missing input, or malformed shape is BLOCK, never an
implicit pass. Reason codes are stable, contractual strings — see
`JOAO_C8_GATE_CONTRACTS.md` for the authoritative list.

C8-A wires exactly one of these seven into a real runtime call site
(`RunRuntime.start()` writes `frozen_mission.json` — see `build_frozen_mission`
below); the other six stay pure, isolated functions in this module, exercised
directly by `tests/test_c8_gates.py` with hand-built input dicts. Wiring the
remaining six into `approve()`/`promote()`/the orchestrator is C8-B/C8-C scope
(`JOAO_C8_GATES_ROADMAP.md`), not this module's job.

No 8th gate. No OS-security boundary recreation (D-046) — these gates govern
delivery (who reviews, on what SHA, with what proof, in what order, against
which frozen criteria), never a process/kernel isolation claim.
"""
from __future__ import annotations

import fnmatch
from typing import Any

# ---------------------------------------------------------------------------
# Shared result shape
# ---------------------------------------------------------------------------


def _result(ok: bool, reason_code: str, reason: str, candidate_tree: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "decision": "pass" if ok else "block",
        "reason_code": reason_code,
        "reason": reason,
        "candidate_tree": candidate_tree,
        **extra,
    }


def _pass(reason_code: str, reason: str, candidate_tree: str | None, **extra: Any) -> dict[str, Any]:
    return _result(True, reason_code, reason, candidate_tree, **extra)


def _block(reason_code: str, reason: str, candidate_tree: str | None = None, **extra: Any) -> dict[str, Any]:
    return _result(False, reason_code, reason, candidate_tree, **extra)


# ---------------------------------------------------------------------------
# G-DBL-AUDIT — two independent auditors, risk-tier scaled  (v4: provider_family)
# ---------------------------------------------------------------------------

_DBL_AUDIT_REQUIRED_ACCEPTS = {"normal": 1, "critical": 2}


def gate_dbl_audit(*, risk_tier: str | None, builder_provider: str | None,
                   builder_family: str | None, reviewer_verdicts: list[dict[str, Any]] | None,
                   candidate_tree: str | None) -> dict[str, Any]:
    """`JOAO_C8_GATE_CONTRACTS.md` G-DBL-AUDIT (v4: `provider_family`).

    `reviewer_verdicts`: list of `{"provider": str, "provider_family": str,
    "model": str, "ok": bool, "decision": "pass"|"p1"|"block",
    "candidate_tree": str|None}` — one entry per reviewer's final-stage
    verdict, exactly as `validate_reviewer_verdict`'s `proof.reviewer` +
    the controller-computed `candidate_tree` binding would produce.

    `normal` requires EXACTLY 1 ACCEPT from a provider whose family !=
    builder's — 0 or 2+ accepted verdicts both BLOCK (correction loop:
    exact cardinality, not a floor). `critical` requires EXACTLY 2 ACCEPTs
    from providers whose families are mutually distinct AND != builder's
    family — 0, 1 or 3+ accepted verdicts all BLOCK. Matching two tools of
    the SAME family (e.g. Codex + a formal-GPT import: both
    `provider_family="openai"`) does NOT satisfy critical (finding GPT v3).
    """
    # D1: risk_tier absent/invalid => BLOCK, never a silent "normal" default.
    if risk_tier not in _DBL_AUDIT_REQUIRED_ACCEPTS:
        return _block("G_DBL_AUDIT_INSUFFICIENT_REVIEWERS",
                      "D1: run.risk_tier is missing or not one of 'normal'/'critical' — "
                      "an undefined tier can never satisfy a reviewer-count requirement",
                      candidate_tree, risk_tier=risk_tier)
    if not builder_provider or not builder_family:
        return _block("G_DBL_AUDIT_INSUFFICIENT_REVIEWERS",
                      "run.builder_provider/builder_family missing — cannot verify reviewer independence",
                      candidate_tree)
    if not candidate_tree:
        # correction loop finding #2: a missing/empty candidate_tree means
        # there is nothing concrete for any verdict to be bound to — never a
        # vacuous pass just because no mismatch could be detected.
        return _block("G_DBL_AUDIT_INSUFFICIENT_REVIEWERS",
                      "candidate_tree is missing/empty — cannot bind or verify any reviewer verdict",
                      candidate_tree)
    verdicts = reviewer_verdicts or []

    accepted = [v for v in verdicts if v.get("ok") is True and v.get("decision") == "pass"]

    # Tree binding: every accepted verdict must be bound to the exact frozen candidate.
    for verdict in accepted:
        if verdict.get("candidate_tree") != candidate_tree:
            return _block("G_DBL_AUDIT_TREE_MISMATCH",
                          f"reviewer {verdict.get('provider')!r} verdict is bound to "
                          f"{verdict.get('candidate_tree')!r}, not the frozen candidate_tree {candidate_tree!r}",
                          candidate_tree, offending_provider=verdict.get("provider"))

    # The builder never counts as its own reviewer — provider OR family match.
    for verdict in accepted:
        if verdict.get("provider") == builder_provider or verdict.get("provider_family") == builder_family:
            return _block("G_DBL_AUDIT_BUILDER_SELF_REVIEW",
                          f"reviewer {verdict.get('provider')!r} (family {verdict.get('provider_family')!r}) "
                          f"matches the builder ({builder_provider!r}, family {builder_family!r})",
                          candidate_tree, offending_provider=verdict.get("provider"))

    providers = [v.get("provider") for v in accepted]
    if len(providers) != len(set(providers)):
        return _block("G_DBL_AUDIT_SAME_PROVIDER",
                      "two or more accepted verdicts share the same reviewer.provider",
                      candidate_tree, providers=providers)

    required = _DBL_AUDIT_REQUIRED_ACCEPTS[risk_tier]

    if risk_tier == "critical":
        families = [v.get("provider_family") for v in accepted]
        if len(families) != len(set(families)):
            return _block("G_DBL_AUDIT_SAME_FAMILY",
                          "critical tier: two or more accepted verdicts share the same provider_family "
                          "(e.g. Codex + a formal-GPT import are both 'openai' — not two distinct families)",
                          candidate_tree, families=families)
        if len(accepted) < required:
            # A critical run with fewer than 2 accepted, distinct-family, non-builder
            # verdicts is fail-closed BLOCK — never silently accepted with 2 same-family
            # verdicts, and never a claim that a 3rd automatic reviewer was invented.
            return _block("G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE",
                          f"critical tier requires exactly {required} ACCEPT verdicts of mutually distinct "
                          f"provider_family (all != builder family {builder_family!r}); only "
                          f"{len(accepted)} qualifying verdict(s) present",
                          candidate_tree, accepted_count=len(accepted), required=required)
        if len(accepted) > required:
            # correction loop finding #3: EXACT cardinality, not a floor — a
            # 3rd+ accepted verdict is never silently ignored/tolerated, even
            # if it is itself distinct-family and non-builder.
            return _block("G_DBL_AUDIT_TOO_MANY_REVIEWERS",
                          f"critical tier requires exactly {required} ACCEPT verdicts; "
                          f"{len(accepted)} qualifying verdicts present",
                          candidate_tree, accepted_count=len(accepted), required=required)
        return _pass("G_DBL_AUDIT_OK", "critical tier: exactly 2 ACCEPT verdicts, distinct families, tree-bound",
                    candidate_tree, providers=providers, families=families)

    if len(accepted) < required:
        return _block("G_DBL_AUDIT_INSUFFICIENT_REVIEWERS",
                      f"{risk_tier} tier requires exactly {required} ACCEPT verdict(s) from a provider != "
                      f"builder; only {len(accepted)} qualifying verdict(s) present",
                      candidate_tree, accepted_count=len(accepted), required=required)
    if len(accepted) > required:
        # correction loop finding #3: exactly 1 for normal, never 2+.
        return _block("G_DBL_AUDIT_TOO_MANY_REVIEWERS",
                      f"{risk_tier} tier requires exactly {required} ACCEPT verdict(s); "
                      f"{len(accepted)} qualifying verdicts present",
                      candidate_tree, accepted_count=len(accepted), required=required)
    return _pass("G_DBL_AUDIT_OK", f"{risk_tier} tier: exactly {required} ACCEPT verdict(s), tree-bound",
                candidate_tree, providers=providers)


# ---------------------------------------------------------------------------
# G-HERMETIC — the default suite touches no real/external sensitive root
# ---------------------------------------------------------------------------

# Reason codes this gate can emit for an uninjected touch of a covered root.
_HERMETIC_ROOT_REASON_CODES = {
    "real_memory": "G_HERMETIC_REAL_MEMORY_TOUCHED",
    "external_ledger": "G_HERMETIC_EXTERNAL_LEDGER_DEPENDENCY",
}
_HERMETIC_DEFAULT_REASON_CODE = "G_HERMETIC_UNINJECTED_ROOT"


def gate_hermetic(*, touches: list[dict[str, Any]] | None, candidate_tree: str | None = None) -> dict[str, Any]:
    """`JOAO_C8_GATE_CONTRACTS.md` G-HERMETIC.

    `touches`: a list of `{"root": str, "path": str, "injected": bool}`
    records — one per resolved covered-root open detected by the real
    entrypoint auditor (`tests/conftest.py`'s file-open hook). `root` is one
    of the sensitive/mutable data roots this gate is explicitly bounded to
    (real memory, external ledgers, sibling repos, user workspaces,
    credentials/config, runtime artifacts) — repo sources, Python/stdlib,
    installed packages and `.pytest_cache` are never reported as touches at
    all (they are explicitly out of scope, never even classified).

    `ok=true` only if every touch of a covered root was `injected=True`
    (resolved under `tmp_path`/an explicitly injected substitute root).
    """
    for touch in touches or []:
        if touch.get("injected") is True:
            continue
        root = touch.get("root")
        reason_code = _HERMETIC_ROOT_REASON_CODES.get(root, _HERMETIC_DEFAULT_REASON_CODE)
        return _block(reason_code,
                      f"uninjected open of covered root {root!r} at {touch.get('path')!r} — "
                      "must resolve under tmp_path/an injected substitute",
                      candidate_tree, root=root, path=touch.get("path"))
    return _pass("G_HERMETIC_OK", "no uninjected touch of a covered sensitive/mutable data root", candidate_tree)


# ---------------------------------------------------------------------------
# G-AUTH-IO — control is exercised via the REAL protected entrypoint
# ---------------------------------------------------------------------------


def gate_auth_io(*, gate_name: str, required_entrypoint_symbols: list[str] | None,
                 referenced_symbols: list[str] | None, resolvable_symbols: list[str] | None,
                 candidate_tree: str | None = None) -> dict[str, Any]:
    """`JOAO_C8_GATE_CONTRACTS.md` G-AUTH-IO.

    `required_entrypoint_symbols`: the real production entrypoints (e.g.
    `"CodexEvidenceReviewer.review"`) this gate's own test file must
    reference — never only an internal helper.
    `referenced_symbols`: symbols the test file actually references
    (static, from `scripts/audit_test_entrypoints.py`'s AST scan).
    `resolvable_symbols`: the subset of `required_entrypoint_symbols` that
    can still be imported/constructed (a stale name after a refactor is
    unresolvable).
    """
    required = list(required_entrypoint_symbols or [])
    if not required:
        return _block("G_AUTH_IO_HELPER_ONLY_COVERAGE",
                      f"{gate_name}: no required entrypoint symbols declared — cannot prove real-entrypoint coverage",
                      candidate_tree)
    resolvable = set(resolvable_symbols or [])
    unreachable = [sym for sym in required if sym not in resolvable]
    if unreachable:
        return _block("G_AUTH_IO_ENTRYPOINT_UNREACHABLE",
                      f"{gate_name}: entrypoint symbol(s) named in the contract cannot be resolved: {unreachable}",
                      candidate_tree, unreachable=unreachable)
    referenced = set(referenced_symbols or [])
    uncovered = [sym for sym in required if sym not in referenced]
    if uncovered:
        return _block("G_AUTH_IO_HELPER_ONLY_COVERAGE",
                      f"{gate_name}: test file never references real entrypoint(s) {uncovered} — "
                      "helper-only coverage is insufficient",
                      candidate_tree, uncovered=uncovered)
    return _pass("G_AUTH_IO_OK", f"{gate_name}: every required real entrypoint is referenced and resolvable",
                candidate_tree)


# ---------------------------------------------------------------------------
# G-NO-STALE-ENTRYPOINT — exhaustive inventory, no legacy/duplicate/unprotected path
# ---------------------------------------------------------------------------


def _no_stale_is_canonical(callable_: dict[str, Any], canonical: set[str]) -> bool:
    return bool(callable_.get("canonical")) and callable_.get("name") in canonical


def gate_no_stale_entrypoint(*, discovered_callables: list[dict[str, Any]] | None,
                             canonical_entrypoints: list[str] | None,
                             candidate_tree: str | None = None) -> dict[str, Any]:
    """`JOAO_C8_GATE_CONTRACTS.md` G-NO-STALE-ENTRYPOINT.

    `discovered_callables`: AST-inventoried callables reaching a dispatch/
    promotion/artefact-read effect: `{"name": str, "effect": str,
    "canonical": bool, "protected": bool, "predates_gates": bool}`.
    `canonical_entrypoints`: the allowlist of canonical entrypoint names.

    Callables are grouped by the effect they reach first — this distinguishes
    a lone stray path with no canonical sibling (`G_NO_STALE_UNLISTED_
    DISPATCH`) from a SECOND, redundant path to an effect that already has a
    canonical, allowlisted one (`G_NO_STALE_DUPLICATE_PATH`) — both are real,
    non-overlapping failure shapes the contract names separately.
    """
    # correction loop finding #2: an "exhaustive inventory" gate that receives
    # NO inventory (or no allowlist to check it against) can prove nothing —
    # an empty/missing input must never vacuously pass as "nothing wrong
    # found". A genuinely empty repo inventory is not a real-world case this
    # gate is ever evaluated against; treat it as malformed/unusable input.
    if not discovered_callables:
        return _block("G_NO_STALE_UNLISTED_DISPATCH",
                      "discovered_callables is missing/empty — cannot prove an exhaustive entrypoint "
                      "inventory ran; a vacuous pass is never accepted as evidence",
                      candidate_tree)
    if not canonical_entrypoints:
        return _block("G_NO_STALE_UNLISTED_DISPATCH",
                      "canonical_entrypoints allowlist is missing/empty — cannot verify any discovered "
                      "callable against it",
                      candidate_tree)
    canonical = set(canonical_entrypoints)
    effects: dict[str, list[dict[str, Any]]] = {}
    for callable_ in discovered_callables or []:
        effects.setdefault(callable_.get("effect"), []).append(callable_)

    for effect, group in effects.items():
        non_canonical = [c for c in group if not _no_stale_is_canonical(c, canonical)]
        if non_canonical and len(group) > 1:
            names = [c.get("name") for c in group]
            return _block("G_NO_STALE_DUPLICATE_PATH",
                          f"effect {effect!r} is reachable via more than one callable {names!r}, "
                          "at least one non-canonical/unlisted",
                          candidate_tree, effect=effect, names=names)
        if non_canonical:
            offending = non_canonical[0].get("name")
            return _block("G_NO_STALE_UNLISTED_DISPATCH",
                          f"callable {offending!r} reaches effect {effect!r} without being in the "
                          "canonical entrypoint allowlist, and has no canonical sibling",
                          candidate_tree, offending=offending)
        for callable_ in group:
            if callable_.get("predates_gates") and not callable_.get("protected"):
                return _block("G_NO_STALE_LEGACY_UNPROTECTED",
                              f"entrypoint {callable_.get('name')!r} predates the current gate chain "
                              "and does not call it",
                              candidate_tree, offending=callable_.get("name"))
    return _pass("G_NO_STALE_OK", "every discovered callable is canonical, gate-protected and effect-unique",
                candidate_tree)


# ---------------------------------------------------------------------------
# G-SHA-BOUND-PROOF — every artefact is bound to the exact commit/tree  (meta-gate)
# ---------------------------------------------------------------------------


def gate_sha_bound_proof(*, artifact: dict[str, Any] | None, expected_candidate_tree: str | None,
                         recomputed_candidate_tree: str | None = None) -> dict[str, Any]:
    """`JOAO_C8_GATE_CONTRACTS.md` G-SHA-BOUND-PROOF.

    `artifact`: any proof record (test/attack/canary/promotion/gate-run log)
    expected to carry a `candidate_tree` field.
    `expected_candidate_tree`: the tree the artifact is being consumed as
    proof FOR (the caller's current candidate).
    `recomputed_candidate_tree`: an independent recompute of the artifact's
    OWN candidate at consumption time, if available (catches a mutation
    between the artifact's write and its consumption) — `None` skips this
    specific check (the write-time-only call site has nothing to recompute
    against yet).
    """
    if not expected_candidate_tree:
        # correction loop finding #2: without knowing what tree the artifact
        # is being consumed FOR, no binding check below can mean anything —
        # a missing expected_candidate_tree must never let a mismatch check
        # be silently skipped into a pass.
        return _block("G_SHA_BOUND_MISSING",
                      "expected_candidate_tree is missing/empty — cannot verify any artifact binding",
                      expected_candidate_tree)
    if not isinstance(artifact, dict) or not artifact.get("candidate_tree"):
        return _block("G_SHA_BOUND_MISSING", "artifact has no candidate_tree field", expected_candidate_tree)
    artifact_tree = artifact["candidate_tree"]
    if recomputed_candidate_tree is not None and recomputed_candidate_tree != artifact_tree:
        return _block("G_SHA_BOUND_MISMATCH",
                      f"artifact claims candidate_tree {artifact_tree!r} but independent recompute at "
                      f"consumption time yields {recomputed_candidate_tree!r} — candidate mutated between "
                      "proof write and proof consumption",
                      expected_candidate_tree, artifact_tree=artifact_tree)
    if artifact_tree != expected_candidate_tree:
        return _block("G_SHA_BOUND_CROSS_CANDIDATE",
                      f"artifact is bound to candidate_tree {artifact_tree!r}, but is being presented as "
                      f"proof for a different candidate {expected_candidate_tree!r}",
                      expected_candidate_tree, artifact_tree=artifact_tree)
    return _pass("G_SHA_BOUND_OK", "artifact is bound to the exact candidate under consideration",
                expected_candidate_tree, artifact_tree=artifact_tree)


# ---------------------------------------------------------------------------
# G-CANARY-FIRST — promotion blocked without a green canary on the exact candidate
# ---------------------------------------------------------------------------


def gate_canary_first(*, canary_required: bool, canary_record: dict[str, Any] | None,
                      candidate_tree: str | None) -> dict[str, Any]:
    """`JOAO_C8_GATE_CONTRACTS.md` G-CANARY-FIRST.

    `canary_record`: `{"candidate_tree": str, "passed": bool, "proof_path": str}`
    or `None` if no canary has run for this candidate yet.
    """
    if not canary_required:
        return _pass("G_CANARY_FIRST_NOT_REQUIRED", "policy does not require a canary for this run",
                    candidate_tree, skipped=True)
    if not candidate_tree:
        # correction loop finding #2: without a real candidate_tree there is
        # nothing to bind a canary record to — never let a None==None
        # coincidence between an absent candidate_tree and an absent/stale
        # canary_record.candidate_tree read as "matched".
        return _block("G_CANARY_FIRST_MISSING",
                      "candidate_tree is missing/empty — cannot verify any canary binding", candidate_tree)
    if not isinstance(canary_record, dict):
        return _block("G_CANARY_FIRST_MISSING", "canary_required=true but no canary record exists for this candidate",
                      candidate_tree)
    if canary_record.get("candidate_tree") != candidate_tree:
        return _block("G_CANARY_FIRST_STALE_CANDIDATE",
                      f"canary record is bound to {canary_record.get('candidate_tree')!r}, "
                      f"not the candidate being promoted {candidate_tree!r}",
                      candidate_tree, canary_candidate_tree=canary_record.get("candidate_tree"))
    if not canary_record.get("passed"):
        return _block("G_CANARY_FIRST_FAILED", "canary record for this exact candidate is present but failed",
                      candidate_tree)
    return _pass("G_CANARY_FIRST_OK", "green canary present for the exact candidate_tree being promoted",
                candidate_tree)


# ---------------------------------------------------------------------------
# G-FROZEN-FINISH-LINE — scope/criteria frozen at start(), mechanically enforced
# ---------------------------------------------------------------------------


def build_frozen_mission(*, spec_sha: str | None, roadmap_sha: str | None,
                         authority_instruction_hash: str | None, risk_tier: str | None,
                         canary_required: bool, forbidden_paths: list[str] | None,
                         criterion_bindings: dict[str, Any] | None) -> dict[str, Any]:
    """Build the immutable `frozen_mission.json` payload written once by
    `RunRuntime.start()` (`JOAO_C8_GATE_CONTRACTS.md` G-FROZEN-FINISH-LINE).

    Pure — never touches disk itself; the caller (`RunRuntime.start()`)
    persists the returned dict via `atomic_write_json`, exactly like
    `checkpoints/0000-pending.json`.
    """
    return {
        "spec_sha": spec_sha,
        "roadmap_sha": roadmap_sha,
        "authority_instruction_hash": authority_instruction_hash,
        "risk_tier": risk_tier,
        "canary_required": bool(canary_required),
        "forbidden_paths": list(forbidden_paths or []),
        "criterion_bindings": dict(criterion_bindings or {}),
    }


def _path_matches(path: str, pattern: str) -> bool:
    if path == pattern:
        return True
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatch(path, pattern)
    return False


def _path_forbidden(path: str, forbidden_paths: list[str]) -> bool:
    return any(_path_matches(path, forbidden) for forbidden in forbidden_paths)


def _binding_covers(path: str, action: str, binding: dict[str, Any]) -> bool:
    allowed_paths = binding.get("allowed_paths") or []
    allowed_actions = binding.get("allowed_actions") or []
    if action not in allowed_actions:
        return False
    return any(_path_matches(path, pattern) for pattern in allowed_paths)


def gate_frozen_finish_line(*, frozen_mission: dict[str, Any] | None,
                            changed_paths: list[dict[str, Any]] | None,
                            corrections: list[dict[str, Any]] | None = None,
                            required_test_results: dict[str, dict[str, Any]] | None = None,
                            candidate_tree: str | None = None) -> dict[str, Any]:
    """`JOAO_C8_GATE_CONTRACTS.md` G-FROZEN-FINISH-LINE (v4: required_tests
    PASS+tree-bound enforcement, finding GPT v3).

    `frozen_mission`: the dict written by `build_frozen_mission`/`start()`.
    `changed_paths`: `[{"path": str, "action": "modify"|"create"|...}, ...]`.
    `corrections`: `[{"acceptance_criterion_id": str|None, "out_of_scope_but_valid": bool}, ...]`.
    `required_test_results`: `{"tests/test_x.py::test_y": {"passed": bool,
    "candidate_tree": str|None}, ...}` — evidence for every `required_tests`
    entry named by any AC covered by this run's changed paths/corrections.
    A required_test absent from this mapping, failed, or bound to a
    different tree than `candidate_tree` BLOCKs (finding GPT v3: a
    `criterion_bindings` entry is never satisfied by declaration alone).
    """
    if not isinstance(frozen_mission, dict) or frozen_mission.get("risk_tier") not in ("normal", "critical"):
        return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                      "frozen_mission is missing or has no valid risk_tier (D1) — cannot evaluate scope",
                      candidate_tree)

    # correction loop finding #2: every one of these frozen_mission fields is
    # itself a mandatory, contractually-required part of the artefact
    # (JOAO_C8_GATE_CONTRACTS.md G-FROZEN-FINISH-LINE) — missing/empty must
    # BLOCK, never be silently treated as "not applicable this run".
    for field in ("spec_sha", "roadmap_sha", "authority_instruction_hash"):
        if not frozen_mission.get(field):
            return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                          f"frozen_mission.{field} is missing/empty — a mission cannot be frozen without it",
                          candidate_tree, missing_field=field)
    if not frozen_mission.get("criterion_bindings"):
        return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                      "frozen_mission.criterion_bindings is missing/empty — no acceptance criterion could "
                      "ever be satisfied; a criterion_bindings-free mission can never map a changed path",
                      candidate_tree)

    forbidden_paths = list(frozen_mission.get("forbidden_paths") or [])
    bindings: dict[str, Any] = dict(frozen_mission.get("criterion_bindings") or {})
    for ac_id, binding in bindings.items():
        if not (binding or {}).get("required_tests"):
            # finding GPT v3, sharpened by the correction loop: an AC binding
            # with an EMPTY required_tests list would otherwise be "satisfied
            # by declaration alone" the instant it's touched — exactly the
            # loophole this gate exists to close. Malformed at freeze time,
            # not just at consumption time.
            return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                          f"criterion_bindings[{ac_id!r}].required_tests is missing/empty — this AC could "
                          "never be provably satisfied",
                          candidate_tree, offending_ac=ac_id)

    touched_acs: set[str] = set()

    for change in changed_paths or []:
        path, action = change.get("path"), change.get("action")
        if path is None or action is None:
            return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                          f"malformed changed-path record (missing path/action): {change!r}",
                          candidate_tree)
        if _path_forbidden(path, forbidden_paths):
            return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                          f"changed path {path!r} is inside a forbidden_path", candidate_tree, offending_path=path)
        matched_ac = next((ac_id for ac_id, binding in bindings.items()
                           if _binding_covers(path, action, binding)), None)
        if matched_ac is None:
            return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                          f"changed path {path!r} (action={action!r}) does not map to any frozen "
                          "acceptance_criterion_id's allowed_paths/allowed_actions",
                          candidate_tree, offending_path=path, offending_action=action)
        touched_acs.add(matched_ac)

    for correction in corrections or []:
        ac_id = correction.get("acceptance_criterion_id")
        if not ac_id or ac_id not in bindings:
            if correction.get("out_of_scope_but_valid"):
                return _block("G_FROZEN_FINISH_LINE_REQUIRES_NEW_AUTHORITY",
                              "correction is real and desirable but names no frozen acceptance_criterion_id — "
                              "requires a new Boss-authorized run, never a silent inclusion",
                              candidate_tree, correction=correction)
            return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                          f"correction does not name a frozen acceptance_criterion_id: {correction!r}",
                          candidate_tree)
        touched_acs.add(ac_id)

    # finding GPT v3: every required_test of every touched AC must be a real,
    # tree-bound PASS — a criterion_bindings entry is never satisfied by mere
    # declaration. Missing/failed/stale/cross-candidate evidence BLOCKs.
    results = required_test_results or {}
    for ac_id in touched_acs:
        for test_id in bindings.get(ac_id, {}).get("required_tests") or []:
            evidence = results.get(test_id)
            if not isinstance(evidence, dict) or not evidence.get("passed"):
                return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                              f"required_test {test_id!r} for {ac_id!r} has no PASS evidence "
                              "(missing or failed) — criterion_bindings is not satisfied by declaration alone",
                              candidate_tree, offending_ac=ac_id, offending_test=test_id)
            if evidence.get("candidate_tree") != candidate_tree:
                return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                              f"required_test {test_id!r} for {ac_id!r} PASSED but its evidence is bound to "
                              f"{evidence.get('candidate_tree')!r}, not this candidate_tree {candidate_tree!r}",
                              candidate_tree, offending_ac=ac_id, offending_test=test_id)

    return _pass("G_FROZEN_FINISH_LINE_OK",
                "every changed path and correction maps to a frozen acceptance criterion, none touches a "
                "forbidden_path, and every touched criterion's required_tests are PASS and tree-bound",
                candidate_tree, touched_acceptance_criteria=sorted(touched_acs))


GATE_NAMES = (
    "G-DBL-AUDIT",
    "G-HERMETIC",
    "G-AUTH-IO",
    "G-NO-STALE-ENTRYPOINT",
    "G-SHA-BOUND-PROOF",
    "G-CANARY-FIRST",
    "G-FROZEN-FINISH-LINE",
)
