"""Temporary reviewer topology (Boss decision, 2026-07-20 — "TEMPORARY
REVIEWER TOPOLOGY UNTIL 23 JULY 2026"): Codex is unavailable until Codex's
live availability probe succeeds, so this module selects, WITHOUT ever
requiring a live Codex dispatch, which reviewer family pairs with which
builder family.

Pure selection logic, never a gate: `JOAO_C8_GATE_CONTRACTS.md` fixes the set
of seven gates closed (`bubble/gates.py` — "No 8th gate"). This module only
picks the INPUTS `run_c8b_mission`/`gate_dbl_audit` are then called with; it
never replaces, duplicates, or re-implements `gate_dbl_audit`'s own
post-dispatch verdict counting.

NORMAL policy (`bubble/runtime.py` identity table, `JOAO_WORKER_INTEGRATION_
SPEC.md` §5): exactly one valid reviewer, family != builder family. Fixed,
not a search over available reviewers:

    GLM   (zai)       builder -> Claude (anthropic) reviewer
    Claude (anthropic) builder -> GLM (zai) reviewer

CRITICAL policy: exactly two reviewers, mutually distinct families, both !=
builder family. Until Codex's live availability probe succeeds, only ONE
non-builder family exists (the NORMAL partner) — never a fabricated second.
A critical mission for either family therefore BLOCKs with the explicit
reason `G_DBL_AUDIT_NO_SECOND_DISTINCT_FAMILY_AVAILABLE` while Codex stays
unavailable, and the exact same call automatically allows Codex the instant
`codex_available` reports True — no code change required on 2026-07-23
beyond that live probe succeeding.
"""
from __future__ import annotations

from typing import Any

REASON_NO_SECOND_DISTINCT_FAMILY = "G_DBL_AUDIT_NO_SECOND_DISTINCT_FAMILY_AVAILABLE"

# The only two builder families this temporary topology defines a NORMAL
# reviewer partner for; deliberately a small closed map, not a general
# same-family-exclusion search — a builder family this map has no entry for
# gets a fail-closed C8B_NO_NORMAL_TOPOLOGY_FOR_BUILDER_FAMILY refusal, never
# a silently-guessed partner.
_NORMAL_PARTNER_FAMILY = {"zai": "anthropic", "anthropic": "zai"}

CODEX_FAMILY = "openai"


def select_normal_reviewer_family(builder_family: str) -> dict[str, Any]:
    """Fixed NORMAL-tier reviewer partner for `builder_family`.

    Returns `{"ok": True, "reviewer_family": str}` or a fail-closed
    `{"ok": False, "reason_code": str, "reason": str}` when no topology is
    defined for this builder family (never a guessed/fabricated partner).
    """
    partner = _NORMAL_PARTNER_FAMILY.get(builder_family)
    if partner is None:
        return {
            "ok": False, "reason_code": "C8B_NO_NORMAL_TOPOLOGY_FOR_BUILDER_FAMILY",
            "reason": f"no NORMAL-tier reviewer topology is defined for builder family {builder_family!r} "
                      f"(defined: {sorted(_NORMAL_PARTNER_FAMILY)})",
        }
    return {"ok": True, "reviewer_family": partner}


def select_critical_reviewer_families(builder_family: str, *, codex_available: bool) -> dict[str, Any]:
    """The two mutually distinct, non-builder reviewer families CRITICAL
    needs, gated on whether Codex's live availability probe currently
    succeeds.

    Returns `{"ok": True, "reviewer_families": [family, "openai"]}` once
    Codex is available, or a fail-closed
    `{"ok": False, "reason_code": REASON_NO_SECOND_DISTINCT_FAMILY, ...}`
    while it is not — `codex_available` must come from a real live probe
    (e.g. `CodexCLIReviewer.available()`), never a caller-asserted flag, so
    this can never present an unavailable Codex as selectable.
    """
    if builder_family not in _NORMAL_PARTNER_FAMILY:
        return {
            "ok": False, "reason_code": "C8B_NO_CRITICAL_TOPOLOGY_FOR_BUILDER_FAMILY",
            "reason": f"no CRITICAL-tier reviewer topology is defined for builder family {builder_family!r} "
                      f"(defined: {sorted(_NORMAL_PARTNER_FAMILY)})",
        }
    if not codex_available:
        return {
            "ok": False, "reason_code": REASON_NO_SECOND_DISTINCT_FAMILY,
            "reason": f"critical tier for builder family {builder_family!r} requires two mutually distinct "
                      "reviewer families, both != builder; only openai (Codex) can supply the second one "
                      "and Codex is not currently live-available",
        }
    partner = _NORMAL_PARTNER_FAMILY[builder_family]
    return {"ok": True, "reviewer_families": [partner, CODEX_FAMILY]}
