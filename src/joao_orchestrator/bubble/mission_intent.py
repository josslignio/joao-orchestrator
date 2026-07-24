"""Chat message -> structured mission intent resolution (Boss directive,
2026-07-20/21, corrected twice before/during implementation):

- "Termine le prochain lot <project>" must resolve to a real, honest next
  action — never a fabricated mission, never a stall.
- Project identification is a DETERMINISTIC, controller-owned alias table —
  never one-token overlap (that collided "Job Radar" with "Weekly Trading
  Radar" on the shared word "radar", and could never resolve "JobRadar" or
  "Twitter Bot" at all).
- Concrete product identifiers/aliases live ONLY in each project's own
  `project_profiles/<id>/profile.json` (an `"aliases"` list) — loaded here at
  runtime, never hardcoded in this module. This keeps
  `src/joao_orchestrator` product-agnostic
  (`tests/test_package_boundaries.py::test_no_concrete_product_identifiers_
  in_canonical_package`) exactly as it already was before this alias
  redesign — the fix for one-token collisions is a better MATCHING
  algorithm, not an exception to that boundary rule.

Verified before writing this module (Explore-agent research + direct repo
checks): neither of the two currently registered projects has a real
lot/task-level roadmap anywhere in this repo. Their `projects/<id>/
PROJECT_SPEC.md` files either show an unsigned authority state, or (for one
of them) no PROJECT_SPEC.md exists at all — Phase 0 was never even started.
So the correct resolution today is NOT "run the next lot" — it is "route to
the existing Phase-0 kickoff mechanism" (`bubble/kickoff.py`, `bubble/
api.py`'s `kickoff_state`/`kickoff_action`), which already knows how to
present exactly what needs Boss sign-off. Concrete project identifiers are
never named in this module's source — see `project_profiles/*/profile.json`
for the real, current alias data.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import kickoff as kickoff_mod
from . import project_registry as project_registry_mod

_LOT_INTENT_RE = re.compile(
    r"\b(termine|lance|d[ée]marre|continue|next|run|start)\b.{0,20}\b(lot|prochain lot|next lot)\b", re.I)


def _normalize(text: str) -> str:
    """Rules (Boss directive): case, repeated whitespace, hyphens,
    underscores and harmless punctuation are all ignored for matching."""
    text = text.lower()
    text = re.sub(r"[-_]+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _significant_words(phrase: str) -> set[str]:
    return set(_normalize(phrase).split())


def load_project_aliases(profiles_root: Path) -> dict[str, list[str]]:
    """Load the deterministic, controller-owned alias table from each
    project's own `profile.json` (`"aliases"` list) — data, never hardcoded
    identifiers in this module. A project with no `aliases` field falls
    back to just its own `project_id` (still a real, non-generic phrase)."""
    aliases: dict[str, list[str]] = {}
    profiles_root = Path(profiles_root)
    if not profiles_root.is_dir():
        return aliases
    for entry in sorted(profiles_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
            continue
        profile_path = entry / "profile.json"
        if not profile_path.is_file():
            continue
        try:
            data = json.loads(profile_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        project_id = data.get("project_id") or entry.name
        registered = data.get("aliases")
        aliases[project_id] = list(registered) if isinstance(registered, list) and registered else [project_id]
    return aliases


def _matched_project_aliases(normalized_message: str, alias_table: dict[str, list[str]]) -> dict[str, list[str]]:
    """Full-phrase matches only — returns {project_id: [matched aliases]}
    for every project with AT LEAST ONE complete alias phrase present in the
    message. Never a partial/single-token match (rule 3)."""
    matches: dict[str, list[str]] = {}
    for project_id, aliases in alias_table.items():
        hits = [alias for alias in aliases if _normalize(alias) in normalized_message]
        if hits:
            matches[project_id] = hits
    return matches


def _generic_overlap_projects(normalized_message: str, alias_table: dict[str, list[str]]) -> list[str]:
    """Projects whose alias VOCABULARY (any individual word from any of
    their registered alias phrases) shares a word with the message, even
    without a full-phrase match — used only to distinguish a genuinely
    unknown project (PROJECT_NOT_FOUND) from a generic/ambiguous hint like
    bare "radar" (CLARIFICATION_REQUIRED), never to auto-select a project."""
    message_words = set(normalized_message.split())
    hits = []
    for project_id, aliases in alias_table.items():
        vocab: set[str] = set()
        for alias in aliases:
            vocab |= _significant_words(alias)
        if message_words & vocab:
            hits.append(project_id)
    return hits


def resolve_project_alias(text: str, *, profiles_root: Path) -> dict[str, Any]:
    """Deterministic project identification (Boss directive, 2026-07-21).

    Returns `{"ok": True, "project_id": str, "matched_alias": str}` when
    EXACTLY ONE canonical project has a full alias-phrase match (the
    longest matching alias is reported, per rule 2) — or a fail-closed
    `{"ok": False, "reason_code": "PROJECT_NOT_FOUND"|"AMBIGUOUS_PROJECT",
    ...}` otherwise. Never a model-generated project identity (rule 7) —
    this is a pure, deterministic function of the alias table loaded from
    `project_profiles/`.
    """
    alias_table = load_project_aliases(profiles_root)
    normalized = _normalize(text)
    matches = _matched_project_aliases(normalized, alias_table)

    if len(matches) == 1:
        project_id, aliases = next(iter(matches.items()))
        longest = max(aliases, key=len)  # rule 2: prefer the longest specific alias
        return {"ok": True, "project_id": project_id, "matched_alias": longest}

    if len(matches) > 1:
        # Cross-project collision — never silently select (rule 5).
        return {"ok": False, "reason_code": "AMBIGUOUS_PROJECT",
                "reason": "message matches full aliases for more than one project",
                "candidates": sorted(matches)}

    # No full-phrase match anywhere. Distinguish a genuinely unknown project
    # from a generic/ambiguous hint (e.g. bare "radar") that overlaps SOME
    # registered vocabulary without ever auto-selecting one (rule 3/4).
    generic_hits = _generic_overlap_projects(normalized, alias_table)
    if generic_hits:
        return {"ok": False, "reason_code": "AMBIGUOUS_PROJECT",
                "reason": "message shares only generic/partial vocabulary with registered "
                          "project aliases — never resolved from a single token match",
                "candidates": sorted(generic_hits)}
    return {"ok": False, "reason_code": "PROJECT_NOT_FOUND",
            "reason": "no registered project alias matched this message"}


_REQUIRED_LOT_FIELDS = ("source_file", "item", "test_command", "allowed_paths")


def resolve_next_lot(project_id: str, *, profiles_root: Path) -> dict[str, Any] | None:
    """Read a project's own DECLARED next-lot data — never invented here.

    A project's `profile.json` may carry a `"next_lot"` object naming the
    source file/section, the next eligible item, its dependencies, risk
    tier, allowed write paths and test command (section 4/5 of the Boss
    directive: "derive worktree/allowed_paths/test_command/critical flag
    automatically from authority documents/registry/roadmap/topology/
    policy" — this IS that data source, read from the project's own
    registry entry, never hardcoded product data in this module).

    Returns the `next_lot` dict unchanged when present and structurally
    valid (all of `_REQUIRED_LOT_FIELDS` present, `allowed_paths` a
    non-empty list) — `None` otherwise (no field, malformed, or profile
    missing). Verified as of this writing: neither of the two currently
    registered production projects declares one, so this is unreachable for
    them today — never fabricated just to make a code path reachable.
    """
    profiles_root = Path(profiles_root)
    profile_path = profiles_root / project_id / "profile.json"
    if not profile_path.is_file():
        return None
    try:
        data = json.loads(profile_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    lot = data.get("next_lot")
    if not isinstance(lot, dict):
        return None
    if any(field not in lot for field in _REQUIRED_LOT_FIELDS):
        return None
    if not isinstance(lot.get("allowed_paths"), list) or not lot["allowed_paths"]:
        return None
    return lot


def resolve_chat_intent(text: str, *, projects_root: Path, profiles_root: Path | None = None) -> dict[str, Any]:
    """Resolve a natural-language chat message into a real, honest,
    normalized next action. Never fabricates a mission from data that
    doesn't exist.

    `action_type` is one of:
      NOT_A_MISSION_INTENT   — doesn't read as a "run/continue next lot" instruction
      PROJECT_NOT_FOUND      — no registered project alias matched at all
      CLARIFICATION_REQUIRED — ambiguous project match, OR Phase 0 is signed
                               but no lot/task roadmap data source exists
      PHASE0_KICKOFF_REQUIRED   — no PROJECT_SPEC.md exists yet for this project
      PHASE0_SIGNATURE_REQUIRED — PROJECT_SPEC.md exists but is not signed
      NEXT_ROADMAP_LOT_READY    — Phase 0 signed AND the project's own
                                   profile.json declares a real `"next_lot"`
                                   (unreachable for the two production
                                   projects today — verified neither
                                   declares one)
    Never returns an ok=true response with a constructed mission.
    """
    if not _LOT_INTENT_RE.search(text):
        return {"ok": False, "action_type": "NOT_A_MISSION_INTENT",
                "reason": "message does not look like a 'run/continue the next lot' instruction"}

    if profiles_root is None:
        # Never an ad-hoc sibling-directory guess (Boss directive, 2026-07-21)
        # — the centralized, auditable ProjectRegistry resolution order.
        profiles_root = project_registry_mod.ProjectRegistry().resolve_profiles_root().path
    alias_result = resolve_project_alias(text, profiles_root=profiles_root)
    if not alias_result["ok"]:
        action_type = "PROJECT_NOT_FOUND" if alias_result["reason_code"] == "PROJECT_NOT_FOUND" \
            else "CLARIFICATION_REQUIRED"
        return {"ok": False, "action_type": action_type, **{k: v for k, v in alias_result.items() if k != "ok"}}

    project_id = alias_result["project_id"]
    spec_path = kickoff_mod.spec_path(projects_root, project_id)
    spec_exists = spec_path.is_file()
    signed = kickoff_mod.spec_is_signed(projects_root, project_id)

    if not signed:
        action_type = "PHASE0_KICKOFF_REQUIRED" if not spec_exists else "PHASE0_SIGNATURE_REQUIRED"
        reason = ("Phase 0 has not been started for this project (no PROJECT_SPEC.md yet)"
                  if not spec_exists else
                  "Phase 0 authority sign-off is not yet complete for this project (SIGNÉ: EN ATTENTE)")
        return {"ok": False, "action_type": action_type, "project_id": project_id,
                "matched_alias": alias_result["matched_alias"],
                "spec_exists": spec_exists, "signed": False, "reason": reason}

    lot = resolve_next_lot(project_id, profiles_root=profiles_root)
    if lot is not None:
        return {"ok": False, "action_type": "NEXT_ROADMAP_LOT_READY", "project_id": project_id,
               "matched_alias": alias_result["matched_alias"], "signed": True, "lot": lot}

    # Signed but this project declares no lot/task roadmap data — never
    # fabricated (unreachable for the two registered projects today).
    return {"ok": False, "action_type": "CLARIFICATION_REQUIRED", "project_id": project_id,
           "matched_alias": alias_result["matched_alias"], "signed": True,
           "reason": "Phase 0 is signed, but no lot/task-level roadmap exists yet for this "
                     "project — defining one is a separate product decision, not something "
                     "this resolver can invent"}
