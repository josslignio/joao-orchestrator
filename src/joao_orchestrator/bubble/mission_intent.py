"""Chat message -> structured mission intent resolution (Boss directive,
2026-07-21, corrected before implementation): "Termine le prochain lot
<project>" must resolve to a real, honest next action — never a fabricated
mission JSON, and never a stall.

Fully product-agnostic (package-boundary rule, `tests/test_package_boundaries.
py::test_no_concrete_product_identifiers_in_canonical_package`): this module
never hardcodes a project name/alias — it discovers candidate projects by
token overlap against the REAL `project_profiles/<id>/` directory names on
disk, at call time. Concrete product identifiers live only in
`project_profiles/`, never in `src/joao_orchestrator`.

Verified before writing this module (Explore agent + direct repo checks, for
the two projects named in the run card): neither has a real lot/task-level
roadmap anywhere in this repo. One has no `projects/<id>/PROJECT_SPEC.md` at
all (Phase 0 never even started); the other shows `SIGNÉ : ❌ EN ATTENTE`. So
the correct resolution today is NOT "run the next lot" (no lot data exists to
run) — it is "route to the existing Phase-0 kickoff mechanism"
(`bubble/kickoff.py`, `bubble/api.py`'s `kickoff_state`/`kickoff_action`),
which already knows how to present exactly what needs Boss sign-off. This
module discovers the project alias and returns that real state; it never
invents a lot, and never silently stalls the chat with a dead end.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import kickoff as kickoff_mod

# Words that carry no project-identifying signal — stripped from BOTH the
# chat message and candidate directory names before token-overlap matching.
# A closed, language-mechanical list (verbs/articles the intent grammar
# uses), never a product name.
_STOP_WORDS = {
    "termine", "termin", "lance", "lancer", "demarre", "démarre", "demarrer", "continue",
    "continuer", "next", "run", "start", "le", "la", "les", "prochain", "prochaine", "lot",
    "the", "de", "du", "des", "un", "une",
}

_LOT_INTENT_RE = re.compile(
    r"\b(termine|lance|d[ée]marre|continue|next|run|start)\b.{0,20}\b(lot|prochain lot|next lot)\b", re.I)


def _tokenize(text: str) -> set[str]:
    return {tok for tok in re.findall(r"[a-z0-9]+", text.lower()) if tok not in _STOP_WORDS}


def default_profiles_root(projects_root: Path) -> Path:
    """`project_profiles/` is a sibling of `projects/` at the repo root in
    every deployment this module has been run against — never hardcoded
    beyond that generic relationship."""
    return Path(projects_root).parent / "project_profiles"


def _find_project_by_token_overlap(text: str, profiles_root: Path) -> tuple[str | None, list[str]]:
    """Returns `(project_id, all_candidates)` — `project_id` is set only when
    EXACTLY ONE profile directory's name shares a token with the message;
    zero or multiple matches both come back as `(None, candidates)` (never a
    guessed pick among ties)."""
    message_tokens = _tokenize(text)
    if not profiles_root.is_dir():
        return None, []
    candidates = []
    for entry in sorted(profiles_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
            continue
        name_tokens = _tokenize(entry.name.replace("-", " ").replace("_", " "))
        if message_tokens & name_tokens:
            candidates.append(entry.name)
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def resolve_chat_intent(text: str, *, projects_root: Path, profiles_root: Path | None = None) -> dict[str, Any]:
    """Resolve a natural-language chat message into a real, honest next
    action. Never fabricates a mission from data that doesn't exist.

    Returns one of:
      `{"ok": False, "kind": "not_a_mission_intent", "reason": str}`
        — the message doesn't read as a "run/continue the next lot" instruction.
      `{"ok": False, "kind": "ambiguous", "reason": str, "candidates": [...]}`
        — zero or multiple `project_profiles/` directories matched (or none
        exist yet) — the caller must ask the Boss to disambiguate, never guess.
      `{"ok": False, "kind": "phase0_incomplete", "project_id": str,
        "spec_exists": bool, "signed": bool,
        "action": "open_kickoff", "reason": str}`
        — the project is recognized but Phase 0 sign-off is not complete
        (including the case where no PROJECT_SPEC.md exists at all yet).
      `{"ok": False, "kind": "no_lot_structure", "project_id": str, "reason": str}`
        — Phase 0 IS signed but no lot/task roadmap data source is wired in
        to produce a mission from (never fabricated).
    Never returns an ok=true response with a constructed mission — no
    lot/task-level roadmap data source is wired into this resolver yet for
    any project.
    """
    if not _LOT_INTENT_RE.search(text):
        return {"ok": False, "kind": "not_a_mission_intent",
                "reason": "message does not look like a 'run/continue the next lot' instruction"}

    profiles_root = profiles_root or default_profiles_root(projects_root)
    project_id, candidates = _find_project_by_token_overlap(text, profiles_root)
    if project_id is None:
        return {"ok": False, "kind": "ambiguous",
                "reason": "no single registered project matched this message" if not candidates
                          else "message matched more than one registered project — ambiguous",
                "candidates": candidates}

    spec_path = kickoff_mod.spec_path(projects_root, project_id)
    spec_exists = spec_path.is_file()
    signed = kickoff_mod.spec_is_signed(projects_root, project_id)
    if not signed:
        reason = ("Phase 0 has not been started for this project (no PROJECT_SPEC.md yet)"
                  if not spec_exists else
                  "Phase 0 authority sign-off is not yet complete for this project (SIGNÉ: EN ATTENTE)")
        return {"ok": False, "kind": "phase0_incomplete", "project_id": project_id,
                "spec_exists": spec_exists, "signed": False, "action": "open_kickoff", "reason": reason}

    # Signed but this module has no lot/task roadmap data source wired in —
    # never fabricated.
    return {"ok": False, "kind": "no_lot_structure", "project_id": project_id,
           "reason": "Phase 0 is signed, but no lot/task-level roadmap exists yet for this "
                     "project — defining one is a separate product decision, not something "
                     "this resolver can invent"}
