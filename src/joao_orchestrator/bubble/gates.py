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
from collections.abc import Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Central fail-closed input validation layer
# ---------------------------------------------------------------------------
# One bounded, reusable set of validators shared by all seven gates, so
# malformed input is rejected at the BOUNDARY of every gate instead of being
# patched field-by-field after the fact.
#
# Two rules make this genuinely fail-closed rather than merely defensive:
#
#   1. NO PYTHON TRUTHINESS for contract values. `"false"`, `"true"`, `0`, `1`
#      and `None` are NOT booleans; `" "` is NOT a string identity. Every
#      contract scalar is checked by exact type, never by `if value:`.
#   2. VALIDATION ERRORS ONLY are converted into a BLOCK result. Genuine
#      programming defects (a bug inside a gate's own logic) are deliberately
#      NOT swallowed — `_MalformedInput` is a dedicated internal exception
#      raised solely by these validators, and each gate catches only that
#      type. A blanket `except Exception` would hide real defects and is
#      never used.


class _MalformedInput(ValueError):
    """Raised ONLY by the `_require_*` validators below when a caller supplies
    input that violates the gate's published input contract. Each public gate
    catches exactly this type and converts it into its stable malformed-input
    BLOCK result. Never used for internal logic errors."""


def _type_name(value: Any) -> str:
    return type(value).__name__


def _require_mapping(value: Any, label: str, *, required_keys: tuple[str, ...] = ()) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _MalformedInput(f"{label} must be a mapping, got {_type_name(value)}")
    missing = [key for key in required_keys if key not in value]
    if missing:
        raise _MalformedInput(f"{label} is missing required key(s) {missing}")
    return value


def _require_list(value: Any, label: str, *, allow_empty: bool = True) -> list[Any]:
    # A str/bytes/mapping is never accepted as "a list of things" even though
    # it is iterable — that coercion is exactly how malformed input slips in.
    if not isinstance(value, list):
        raise _MalformedInput(f"{label} must be a list, got {_type_name(value)}")
    if not allow_empty and not value:
        raise _MalformedInput(f"{label} must not be empty — the contract requires evidence here")
    return value


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise _MalformedInput(f"{label} must be a string, got {_type_name(value)}")
    stripped = value.strip()
    if not stripped:
        raise _MalformedInput(f"{label} must be a non-empty, non-whitespace string")
    return stripped


def _require_exact_bool(value: Any, label: str) -> bool:
    # `type(value) is bool` — deliberately NOT isinstance(), because
    # `isinstance(True, int)` is True and, more importantly, this must reject
    # the truthy strings "true"/"false" and the ints 0/1 that a serialized
    # payload can carry.
    if type(value) is not bool:
        raise _MalformedInput(
            f"{label} must be an exact boolean (True/False), got {_type_name(value)} {value!r} — "
            "strings like 'true'/'false' and ints 0/1 are never accepted as booleans"
        )
    return value


def _require_string_list(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    items = _require_list(value, label, allow_empty=allow_empty)
    return [_require_nonempty_string(item, f"{label}[{index}]") for index, item in enumerate(items)]


def _require_mapping_list(value: Any, label: str, *, allow_empty: bool = True,
                          required_keys: tuple[str, ...] = ()) -> list[Mapping[str, Any]]:
    items = _require_list(value, label, allow_empty=allow_empty)
    return [_require_mapping(item, f"{label}[{index}]", required_keys=required_keys)
            for index, item in enumerate(items)]


def _require_candidate_tree(value: Any, label: str = "candidate_tree") -> str:
    return _require_nonempty_string(value, label)


def _require_provider_identity(verdict: Mapping[str, Any], label: str) -> tuple[str, str, str]:
    """Every counted verdict must carry a complete, controller-trusted identity.
    Whitespace-only values are rejected exactly like absent ones."""
    return (
        _require_nonempty_string(verdict.get("provider"), f"{label}.provider"),
        _require_nonempty_string(verdict.get("provider_family"), f"{label}.provider_family"),
        _require_nonempty_string(verdict.get("model"), f"{label}.model"),
    )


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

    `normal` requires EXACTLY 1 verdict, `critical` EXACTLY 2 of mutually
    distinct `provider_family`, all families != the builder's. Matching two
    tools of the SAME family (e.g. Codex + a formal-GPT import: both
    `provider_family="openai"`) does NOT satisfy critical (finding GPT v3).

    DECISION ORDER (one explicit deterministic precedence, both tiers):

      1. structure + identity of EVERY supplied verdict (not just the
         accepted ones) — malformed/unidentified/mis-bound => BLOCK;
      2. any explicit negative verdict (block/p1/ok=false) =>
         `G_DBL_AUDIT_REVIEWER_DISAGREEMENT` — the SAME reason code in both
         tiers, evaluated before any counting so a dissent can never be
         out-voted by piling on ACCEPTs (D4: no automatic tie-break);
      3. exact TOTAL cardinality;
      4. accepted-cardinality requirement;
      5. builder/reviewer independence;
      6. provider-family requirements.

    A negative verdict is therefore NEVER silently filtered out of the
    decision — step 2 sees every verdict exactly as supplied.
    """
    # --- STEP 0: validate every contract input at the boundary -------------
    try:
        if not isinstance(risk_tier, str) or risk_tier not in _DBL_AUDIT_REQUIRED_ACCEPTS:
            # D1: risk_tier absent/invalid => BLOCK, never a silent default.
            return _block("G_DBL_AUDIT_INSUFFICIENT_REVIEWERS",
                          "D1: run.risk_tier is missing or not one of 'normal'/'critical' — "
                          "an undefined tier can never satisfy a reviewer-count requirement",
                          None, risk_tier=risk_tier)
        try:
            builder_provider = _require_nonempty_string(builder_provider, "builder_provider")
            builder_family = _require_nonempty_string(builder_family, "builder_family")
        except _MalformedInput as exc:
            return _block("G_DBL_AUDIT_INSUFFICIENT_REVIEWERS",
                          f"builder identity is unusable ({exc}) — cannot verify reviewer independence",
                          None)
        try:
            candidate_tree = _require_candidate_tree(candidate_tree)
        except _MalformedInput as exc:
            return _block("G_DBL_AUDIT_INSUFFICIENT_REVIEWERS",
                          f"candidate_tree is unusable ({exc}) — cannot bind or verify any reviewer verdict",
                          None)
        if reviewer_verdicts is None:
            verdicts: list[Any] = []
        else:
            verdicts = _require_list(reviewer_verdicts, "reviewer_verdicts")
    except _MalformedInput as exc:
        return _block("G_DBL_AUDIT_MALFORMED_INPUT", str(exc), None)

    required = _DBL_AUDIT_REQUIRED_ACCEPTS[risk_tier]

    # --- STEP 1: structure + identity of EVERY supplied verdict ------------
    # Applied to all verdicts, not only the accepted ones: a dissenting or
    # surplus verdict that cannot even be parsed/identified makes the whole
    # decision unauditable. A non-str (or blank/whitespace) value is just as
    # unusable as an absent key — neither is coerced.
    for index, verdict in enumerate(verdicts):
        label = f"reviewer_verdicts[{index}]"
        try:
            verdict = _require_mapping(verdict, label)
        except _MalformedInput as exc:
            return _block("G_DBL_AUDIT_MALFORMED_VERDICT", str(exc), candidate_tree)
        try:
            _require_provider_identity(verdict, label)
        except _MalformedInput as exc:
            missing = [field for field in ("provider", "provider_family", "model")
                       if not isinstance(verdict.get(field), str) or not verdict.get(field).strip()]
            return _block("G_DBL_AUDIT_MALFORMED_VERDICT",
                          f"a supplied verdict is missing required identity field(s) {missing} — "
                          f"an unidentified verdict can never prove reviewer independence ({exc})",
                          candidate_tree, missing_fields=missing)
        try:
            _require_exact_bool(verdict.get("ok"), f"{label}.ok")
        except _MalformedInput as exc:
            return _block("G_DBL_AUDIT_MALFORMED_VERDICT", str(exc), candidate_tree,
                          offending_provider=verdict.get("provider"))
        if verdict.get("decision") not in ("pass", "p1", "block"):
            return _block("G_DBL_AUDIT_MALFORMED_VERDICT",
                          f"verdict from {verdict.get('provider')!r} has no valid decision "
                          f"(got {verdict.get('decision')!r}; expected pass/p1/block)",
                          candidate_tree, offending_provider=verdict.get("provider"))
        try:
            verdict_tree = _require_candidate_tree(verdict.get("candidate_tree"), f"{label}.candidate_tree")
        except _MalformedInput as exc:
            return _block("G_DBL_AUDIT_TREE_MISMATCH", str(exc), candidate_tree,
                          offending_provider=verdict.get("provider"))
        if verdict_tree != candidate_tree:
            return _block("G_DBL_AUDIT_TREE_MISMATCH",
                          f"reviewer {verdict.get('provider')!r} verdict is bound to "
                          f"{verdict_tree!r}, not the frozen candidate_tree {candidate_tree!r}",
                          candidate_tree, offending_provider=verdict.get("provider"))

    # --- STEP 2: any explicit negative verdict, BOTH tiers -----------------
    # D4 (no automatic tie-break), generalized from critical to normal: a
    # reviewer that answered BLOCK/P1 (or ok=false) is a genuine
    # DISAGREEMENT. Evaluated BEFORE any counting, so a dissent can never be
    # out-voted by adding ACCEPTs, and is never silently filtered out of the
    # decision. ONE shared reason code across both tiers.
    dissenting = [v for v in verdicts
                  if v.get("decision") in ("block", "p1") or v.get("ok") is not True]
    if dissenting:
        return _block("G_DBL_AUDIT_REVIEWER_DISAGREEMENT",
                      f"{risk_tier} tier: {len(dissenting)} reviewer verdict(s) dissent "
                      "(decision block/p1, or ok is not True) — D4 forbids any automatic tie-break; "
                      "a disagreement BLOCKs and escalates to the Boss, it is never out-voted",
                      candidate_tree,
                      dissenting_providers=[v.get("provider") for v in dissenting])

    # --- STEP 3: exact TOTAL cardinality -----------------------------------
    # After step 2 there are no negatives left, so total == accepted; both are
    # reported so an over-count is unambiguous.
    if len(verdicts) > required:
        return _block("G_DBL_AUDIT_TOO_MANY_REVIEWERS",
                      f"{risk_tier} tier requires exactly {required} reviewer verdict(s); "
                      f"{len(verdicts)} supplied (all positive)",
                      candidate_tree, total_count=len(verdicts), required=required)

    # --- STEP 4: accepted-cardinality requirement --------------------------
    accepted = [v for v in verdicts if v.get("ok") is True and v.get("decision") == "pass"]
    if len(accepted) < required:
        # Under-count. `critical` keeps its dedicated fail-closed code: the
        # missing verdict is precisely the distinct non-builder family that
        # does not exist yet, never an invented third reviewer.
        if risk_tier == "critical":
            return _block("G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE",
                          f"critical tier requires exactly {required} ACCEPT verdicts of mutually distinct "
                          f"provider_family (all != builder family {builder_family!r}); only "
                          f"{len(accepted)} qualifying verdict(s) present",
                          candidate_tree, accepted_count=len(accepted), required=required)
        return _block("G_DBL_AUDIT_INSUFFICIENT_REVIEWERS",
                      f"{risk_tier} tier requires exactly {required} ACCEPT verdict(s) from a provider != "
                      f"builder; only {len(accepted)} qualifying verdict(s) present",
                      candidate_tree, accepted_count=len(accepted), required=required)

    # --- STEP 5: builder/reviewer independence -----------------------------
    for verdict in accepted:
        if verdict.get("provider") == builder_provider or verdict.get("provider_family") == builder_family:
            return _block("G_DBL_AUDIT_BUILDER_SELF_REVIEW",
                          f"reviewer {verdict.get('provider')!r} (family {verdict.get('provider_family')!r}) "
                          f"matches the builder ({builder_provider!r}, family {builder_family!r})",
                          candidate_tree, offending_provider=verdict.get("provider"))

    # --- STEP 6: provider-family requirements ------------------------------
    providers = [v.get("provider") for v in accepted]
    if len(providers) != len(set(providers)):
        return _block("G_DBL_AUDIT_SAME_PROVIDER",
                      "two or more accepted verdicts share the same reviewer.provider",
                      candidate_tree, providers=providers)
    families = [v.get("provider_family") for v in accepted]
    if risk_tier == "critical":
        if len(families) != len(set(families)):
            return _block("G_DBL_AUDIT_SAME_FAMILY",
                          "critical tier: two or more accepted verdicts share the same provider_family "
                          "(e.g. Codex + a formal-GPT import are both 'openai' — not two distinct families)",
                          candidate_tree, families=families)
        return _pass("G_DBL_AUDIT_OK", "critical tier: exactly 2 ACCEPT verdicts, distinct families, tree-bound",
                    candidate_tree, providers=providers, families=families)
    return _pass("G_DBL_AUDIT_OK", f"{risk_tier} tier: exactly {required} ACCEPT verdict(s), tree-bound",
                candidate_tree, providers=providers, families=families)


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

    `touches=None` means the audit journal is ABSENT — the auditor never ran,
    or its output was lost. That is unprovable, not clean: it BLOCKs
    (`G_HERMETIC_MISSING_AUDIT_JOURNAL`). Only an explicitly EMPTY list `[]`
    means "the auditor ran and recorded no covered-root touch".
    """
    if touches is None:
        # Correction loop 3, finding #4: `for touch in touches or []` made a
        # missing journal indistinguishable from a clean one, so an absent
        # auditor silently reported G_HERMETIC_OK.
        return _block("G_HERMETIC_MISSING_AUDIT_JOURNAL",
                      "no file-open audit journal was supplied — hermeticity cannot be proven "
                      "without one; an absent journal is never a clean journal",
                      candidate_tree)
    try:
        if candidate_tree is not None:
            candidate_tree = _require_candidate_tree(candidate_tree)
        entries = _require_mapping_list(touches, "touches", required_keys=("root", "path", "injected"))
        validated = []
        for index, touch in enumerate(entries):
            label = f"touches[{index}]"
            validated.append((
                _require_nonempty_string(touch.get("root"), f"{label}.root"),
                _require_nonempty_string(touch.get("path"), f"{label}.path"),
                _require_exact_bool(touch.get("injected"), f"{label}.injected"),
            ))
    except _MalformedInput as exc:
        return _block("G_HERMETIC_MALFORMED_INPUT", str(exc), None)

    for root, path, injected in validated:
        if injected:
            continue
        reason_code = _HERMETIC_ROOT_REASON_CODES.get(root, _HERMETIC_DEFAULT_REASON_CODE)
        return _block(reason_code,
                      f"uninjected open of covered root {root!r} at {path!r} — "
                      "must resolve under tmp_path/an injected substitute",
                      candidate_tree, root=root, path=path)
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
    # Correction loop 3, finding #6: the gate's own identity is a required
    # input — every message below names it, and a result attributed to an
    # unnamed gate is unauditable. Checked first, before anything else.
    if not isinstance(gate_name, str) or not gate_name.strip():
        return _block("G_AUTH_IO_HELPER_ONLY_COVERAGE",
                      "gate_name is missing/empty — a coverage verdict cannot be attributed to an unnamed gate",
                      candidate_tree)
    gate_name = gate_name.strip()
    # Symbol inventories must contain only real, non-empty symbol names. An
    # integer or a None inside an inventory means the inventory itself is
    # untrustworthy — it can never prove real-entrypoint coverage.
    try:
        if candidate_tree is not None:
            candidate_tree = _require_candidate_tree(candidate_tree)
        required = _require_string_list(required_entrypoint_symbols or [], "required_entrypoint_symbols")
        referenced_list = _require_string_list(referenced_symbols or [], "referenced_symbols")
        resolvable_list = _require_string_list(resolvable_symbols or [], "resolvable_symbols")
    except _MalformedInput as exc:
        return _block("G_AUTH_IO_MALFORMED_INPUT", f"{gate_name}: {exc}", None)

    if not required:
        return _block("G_AUTH_IO_HELPER_ONLY_COVERAGE",
                      f"{gate_name}: no required entrypoint symbols declared — cannot prove real-entrypoint coverage",
                      candidate_tree)
    resolvable = set(resolvable_list)
    unreachable = [sym for sym in required if sym not in resolvable]
    if unreachable:
        return _block("G_AUTH_IO_ENTRYPOINT_UNREACHABLE",
                      f"{gate_name}: entrypoint symbol(s) named in the contract cannot be resolved: {unreachable}",
                      candidate_tree, unreachable=unreachable)
    referenced = set(referenced_list)
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
    try:
        if candidate_tree is not None:
            candidate_tree = _require_candidate_tree(candidate_tree)
        _require_string_list(canonical_entrypoints, "canonical_entrypoints", allow_empty=False)
    except _MalformedInput as exc:
        return _block("G_NO_STALE_ENTRYPOINT_MALFORMED_INPUT", str(exc), None)

    canonical = set(canonical_entrypoints)
    effects: dict[str, list[dict[str, Any]]] = {}
    for callable_ in discovered_callables or []:
        # Correction loop 3, finding #5: a record with no `effect` key was
        # silently grouped under the key None, so a malformed inventory entry
        # passed as long as it was otherwise canonical=True. An inventory
        # this gate cannot interpret is malformed input, not a clean result.
        if not isinstance(callable_, Mapping):
            return _block("G_NO_STALE_UNLISTED_DISPATCH",
                          f"malformed discovered_callable (not an object): {callable_!r}", candidate_tree)
        missing = [field for field in ("name", "effect")
                   if not isinstance(callable_.get(field), str) or not callable_.get(field).strip()]
        if missing:
            return _block("G_NO_STALE_UNLISTED_DISPATCH",
                          f"malformed discovered_callable missing required field(s) {missing}: {callable_!r} — "
                          "an uninterpretable inventory entry can never prove an exhaustive inventory",
                          candidate_tree, missing_fields=missing)
        # The three inventory flags are contract booleans: `"false"` is a
        # STRING and must never be read as False (it is in fact truthy in
        # Python, which is precisely how an unprotected entrypoint slipped
        # through as "protected").
        try:
            for flag in ("canonical", "protected", "predates_gates"):
                _require_exact_bool(callable_.get(flag), f"discovered_callable[{callable_.get('name')!r}].{flag}")
        except _MalformedInput as exc:
            return _block("G_NO_STALE_ENTRYPOINT_MALFORMED_INPUT", str(exc), candidate_tree,
                          offending=callable_.get("name"))
        effects.setdefault(callable_["effect"], []).append(callable_)

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
            # ANY entrypoint whose `protected` is not exactly True BLOCKs —
            # not only a legacy one. Previously this required
            # `predates_gates` to be true as well, so a BRAND-NEW unprotected
            # dispatch entrypoint (predates_gates=false, protected=false)
            # passed and the gate reported "every discovered callable is
            # ... gate-protected", which was simply false.
            if callable_.get("protected") is not True:
                return _block("G_NO_STALE_LEGACY_UNPROTECTED",
                              f"entrypoint {callable_.get('name')!r} reaches effect "
                              f"{callable_.get('effect')!r} but is NOT gate-protected "
                              f"(protected={callable_.get('protected')!r}"
                              + (", and predates the current gate chain" if callable_.get("predates_gates") else "")
                              + ")",
                              candidate_tree, offending=callable_.get("name"),
                              predates_gates=callable_.get("predates_gates"))
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
    try:
        # correction loop finding #2: without knowing what tree the artifact
        # is being consumed FOR, no binding check below can mean anything —
        # a missing expected_candidate_tree must never let a mismatch check
        # be silently skipped into a pass.
        expected_candidate_tree = _require_candidate_tree(expected_candidate_tree, "expected_candidate_tree")
    except _MalformedInput as exc:
        return _block("G_SHA_BOUND_MISSING", str(exc), None)
    if not isinstance(artifact, Mapping) or not artifact.get("candidate_tree"):
        return _block("G_SHA_BOUND_MISSING", "artifact has no candidate_tree field", expected_candidate_tree)
    try:
        artifact_tree = _require_candidate_tree(artifact.get("candidate_tree"), "artifact.candidate_tree")
        # An artefact is proof only if it says WHERE the raw evidence lives.
        _require_nonempty_string(artifact.get("proof_path"), "artifact.proof_path")
        if recomputed_candidate_tree is not None:
            recomputed_candidate_tree = _require_candidate_tree(
                recomputed_candidate_tree, "recomputed_candidate_tree")
    except _MalformedInput as exc:
        return _block("G_SHA_BOUND_PROOF_MALFORMED_INPUT", str(exc), expected_candidate_tree)
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
    # `canary_required` is a contract boolean: "false" (a truthy STRING) must
    # never be read as "no canary needed", and must never be read as True either.
    try:
        canary_required = _require_exact_bool(canary_required, "canary_required")
    except _MalformedInput as exc:
        return _block("G_CANARY_FIRST_MALFORMED_INPUT", str(exc), None)

    if not canary_required:
        return _pass("G_CANARY_FIRST_NOT_REQUIRED", "policy does not require a canary for this run",
                    candidate_tree if isinstance(candidate_tree, str) else None, skipped=True)
    try:
        # correction loop finding #2: without a real candidate_tree there is
        # nothing to bind a canary record to — never let a None==None
        # coincidence between an absent candidate_tree and an absent/stale
        # canary_record.candidate_tree read as "matched".
        candidate_tree = _require_candidate_tree(candidate_tree)
    except _MalformedInput as exc:
        return _block("G_CANARY_FIRST_MISSING",
                      f"candidate_tree is unusable ({exc}) — cannot verify any canary binding", None)
    if not isinstance(canary_record, Mapping):
        return _block("G_CANARY_FIRST_MISSING", "canary_required=true but no canary record exists for this candidate",
                      candidate_tree)
    try:
        record_tree = _require_candidate_tree(canary_record.get("candidate_tree"), "canary_record.candidate_tree")
        # A canary "pass" is only evidence if it says where the raw proof is.
        _require_nonempty_string(canary_record.get("proof_path"), "canary_record.proof_path")
        passed = _require_exact_bool(canary_record.get("passed"), "canary_record.passed")
    except _MalformedInput as exc:
        return _block("G_CANARY_FIRST_MALFORMED_INPUT", str(exc), candidate_tree)
    if record_tree != candidate_tree:
        return _block("G_CANARY_FIRST_STALE_CANDIDATE",
                      f"canary record is bound to {record_tree!r}, "
                      f"not the candidate being promoted {candidate_tree!r}",
                      candidate_tree, canary_candidate_tree=record_tree)
    if not passed:
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
    # Correction loop 3, finding #3: this was the only gate that never
    # validated its own candidate_tree (unlike gate_dbl_audit /
    # gate_sha_bound_proof). Without it, candidate_tree=None and a
    # required_test evidence record carrying candidate_tree=None matched each
    # other by coincidence (None == None) and the whole gate reported OK.
    try:
        candidate_tree = _require_candidate_tree(candidate_tree)
    except _MalformedInput as exc:
        return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                      f"candidate_tree is unusable ({exc}) — required_test evidence cannot be bound to, "
                      "or verified against, a candidate that has no identity",
                      None)
    # Validate the container shapes before any semantic scope logic runs, so a
    # malformed element (e.g. `changed_paths=[None]`) BLOCKs instead of raising
    # AttributeError out of the gate.
    try:
        changed_entries = _require_mapping_list(changed_paths if changed_paths is not None else [],
                                                "changed_paths", required_keys=("path", "action"))
        correction_entries = _require_mapping_list(corrections if corrections is not None else [], "corrections")
        results_map = _require_mapping(required_test_results if required_test_results is not None else {},
                                       "required_test_results")
        for test_id, evidence in results_map.items():
            _require_nonempty_string(test_id, "required_test_results key")
            _require_mapping(evidence, f"required_test_results[{test_id!r}]",
                             required_keys=("passed", "candidate_tree"))
            _require_exact_bool(evidence.get("passed"), f"required_test_results[{test_id!r}].passed")
    except _MalformedInput as exc:
        return _block("G_FROZEN_FINISH_LINE_MALFORMED_INPUT", str(exc), candidate_tree)

    if not isinstance(frozen_mission, Mapping) or frozen_mission.get("risk_tier") not in ("normal", "critical"):
        return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                      "frozen_mission is missing or has no valid risk_tier (D1) — cannot evaluate scope",
                      candidate_tree)

    # correction loop finding #2: every one of these frozen_mission fields is
    # itself a mandatory, contractually-required part of the artefact
    # (JOAO_C8_GATE_CONTRACTS.md G-FROZEN-FINISH-LINE) — missing/empty must
    # BLOCK, never be silently treated as "not applicable this run".
    for field in ("spec_sha", "roadmap_sha", "authority_instruction_hash"):
        value = frozen_mission.get(field)
        if not isinstance(value, str) or not value.strip():
            return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                          f"frozen_mission.{field} is missing/empty — a mission cannot be frozen without it",
                          candidate_tree, missing_field=field)
    if not frozen_mission.get("criterion_bindings"):
        return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                      "frozen_mission.criterion_bindings is missing/empty — no acceptance criterion could "
                      "ever be satisfied; a criterion_bindings-free mission can never map a changed path",
                      candidate_tree)

    try:
        forbidden_paths = _require_string_list(frozen_mission.get("forbidden_paths") or [],
                                               "frozen_mission.forbidden_paths")
        bindings_map = _require_mapping(frozen_mission.get("criterion_bindings"),
                                        "frozen_mission.criterion_bindings")
        bindings: dict[str, Any] = {}
        for ac_id, binding in bindings_map.items():
            _require_nonempty_string(ac_id, "criterion_bindings key")
            binding = _require_mapping(binding, f"criterion_bindings[{ac_id!r}]")
            _require_string_list(binding.get("allowed_paths") or [], f"criterion_bindings[{ac_id!r}].allowed_paths")
            _require_string_list(binding.get("allowed_actions") or [],
                                 f"criterion_bindings[{ac_id!r}].allowed_actions")
            if binding.get("required_tests") is not None:
                _require_string_list(binding.get("required_tests"),
                                     f"criterion_bindings[{ac_id!r}].required_tests")
            bindings[ac_id] = binding
    except _MalformedInput as exc:
        return _block("G_FROZEN_FINISH_LINE_MALFORMED_INPUT", str(exc), candidate_tree)

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

    for change in changed_entries:
        try:
            path = _require_nonempty_string(change.get("path"), "changed_paths[].path")
            action = _require_nonempty_string(change.get("action"), "changed_paths[].action")
        except _MalformedInput as exc:
            return _block("G_FROZEN_FINISH_LINE_MALFORMED_INPUT", str(exc), candidate_tree)
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

    for correction in correction_entries:
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
    results = results_map
    for ac_id in touched_acs:
        for test_id in bindings.get(ac_id, {}).get("required_tests") or []:
            evidence = results.get(test_id)
            if not isinstance(evidence, Mapping) or evidence.get("passed") is not True:
                return _block("G_FROZEN_FINISH_LINE_SCOPE_CREEP",
                              f"required_test {test_id!r} for {ac_id!r} has no PASS evidence "
                              "(missing or failed) — criterion_bindings is not satisfied by declaration alone",
                              candidate_tree, offending_ac=ac_id, offending_test=test_id)
            evidence_tree = evidence.get("candidate_tree")
            if not isinstance(evidence_tree, str) or evidence_tree.strip() != candidate_tree:
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
