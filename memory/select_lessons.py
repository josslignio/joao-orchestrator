#!/usr/bin/env python3
"""B-28 Phase 2 — THE SELECTOR: deterministic top-K lesson retrieval (no LLM).

Given a mission context `{project, mission_type, tags, files_touched}`, return the most
relevant lessons from memory/lessons.jsonl, bounded to a token budget (default 800).

Scoring is purely arithmetic and reproducible — NO model call, NO clock, NO randomness:

    score = OVERLAP_W * (tag_overlap + trigger_overlap) * severity      # relevance × gravity
          + SEV_FLOOR  * severity                                       # gravity floor
          + PROJECT_W  * project_match                                  # same project / systemic
          + RECUR_W    * min(recurrences, 5)                            # a repeat offender ranks up
          + recency_rank                                               # newest lesson wins ties

Ties break on ascending lesson id (stable). Same input → same output, always (Gate 2).
When the mission carries NO usable tags, we fall back to the systemic severity-3 lessons
(the 6 LOIS + the meta-defects) so every mission is still armed against the worst failures.

Injected lesson text is UNTRUSTED advisory data (D-028 spirit): it may advise, never grant
permission or execute. This module only reads and ranks; it never evaluates lesson content.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_ledger import TAG_RULES  # noqa: E402  (reuse the exact stock tagger)

REPO = Path(__file__).resolve().parents[1]
LESSONS = REPO / "memory" / "lessons.jsonl"

# selector-side synonyms: english / free-text mission words → canonical tags.
SYNONYMS = {
    "visual": ["ui", "docs"], "render": ["ui", "docs"], "rendering": ["ui", "docs"],
    "docx": ["docs"], "letter": ["docs"], "resume": ["docs"], "pdf": ["pdf", "docs"],
    "threading": ["async"], "concurrency": ["async"], "race": ["async"],
    "button": ["ui", "cablage"], "wiring": ["cablage"], "e2e": ["cablage", "gate"],
    "scraping": ["sourcing"], "discovery": ["sourcing"], "salary": ["salary"],
}

OVERLAP_W = 1000
SEV_FLOOR = 60
PROJECT_W = 200
RECUR_W = 40


def load_lessons(path: Path = LESSONS) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _tags_from_text(text: str) -> set[str]:
    low = (text or "").lower()
    tags = {tag for tag, pat in TAG_RULES.items() if re.search(pat, low)}
    for word, syns in SYNONYMS.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            tags.update(syns)
    return tags


def query_tags(mission_type: str = "", tags=None, files_touched=None) -> set[str]:
    """Build the deterministic query tag set from the mission context."""
    q: set[str] = set(t.lower() for t in (tags or []))
    q |= _tags_from_text(mission_type or "")
    for f in files_touched or []:
        q |= _tags_from_text(Path(str(f)).name.replace("_", " ").replace("-", " ").replace("/", " "))
    q.discard("general")  # a non-signal placeholder, never a query intent
    return q


def _estimate_tokens(lesson: dict) -> int:
    # injected form is roughly "[L-nnn|sevN] rule" — approximate 1 token per whitespace word,
    # plus a small id/severity header. Conservative (rounds up) so 800 is a real ceiling.
    return len(lesson.get("rule", "").split()) + 4


def _score(lesson: dict, q: set[str], project: str, recency_rank: int) -> int:
    tag_overlap = len(set(lesson.get("tags", [])) & q)
    trig_overlap = len(set(lesson.get("trigger_contexts", [])) & q)
    sev = int(lesson.get("severity", 2))
    lp = lesson.get("project", "all")
    project_match = 1 if (lp == "all" or (project and lp == project)) else 0
    recur = min(int(lesson.get("recurrences", 0)), 5)
    return (OVERLAP_W * (tag_overlap + trig_overlap) * sev
            + SEV_FLOOR * sev
            + PROJECT_W * project_match
            + RECUR_W * recur
            + recency_rank)


def select_lessons(project: str = "", mission_type: str = "", tags=None,
                   files_touched=None, max_tokens: int = 800, max_k: int = 12,
                   lessons: list[dict] | None = None) -> list[dict]:
    """Return top-K lessons for the mission, bounded to `max_tokens`, fully deterministic."""
    pool = lessons if lessons is not None else load_lessons()
    if not pool:
        return []
    q = query_tags(mission_type, tags, files_touched)

    # recency rank: newest date gets the highest tiny bonus; ties by id keep it stable.
    by_recency = sorted(pool, key=lambda d: (d.get("date", ""), d.get("id", "")))
    recency_of = {d["id"]: i for i, d in enumerate(by_recency)}

    if not q:
        # no tags → the systemic severity-3 lessons only (LOIS + meta-defects).
        candidates = [d for d in pool if int(d.get("severity", 2)) == 3]
    else:
        scored = [(d, _score(d, q, project, recency_of[d["id"]])) for d in pool]
        # keep only lessons with real relevance (some tag/trigger overlap).
        relevant = [(d, s) for d, s in scored
                    if set(d.get("tags", [])) & q or set(d.get("trigger_contexts", [])) & q]
        relevant.sort(key=lambda ds: (-ds[1], ds[0]["id"]))
        candidates = [d for d, _ in relevant]
        if not candidates:  # nothing matched → systemic fallback
            candidates = [d for d in pool if int(d.get("severity", 2)) == 3]

    if not q:
        # deterministic fallback order: the 6 LOIS are the systemic BACKBONE and must always be
        # armed first (a newly-imported severity-3 defect must never evict them), then the other
        # severity-3 lessons by recency, then id.
        candidates.sort(key=lambda d: (0 if str(d.get("source_defect", "")).startswith("LOI-") else 1,
                                       -int(d.get("severity", 2)), -recency_of[d["id"]], d["id"]))

    selected, used = [], 0
    for d in candidates:
        cost = _estimate_tokens(d)
        if selected and used + cost > max_tokens:
            break
        selected.append(d)
        used += cost
        if len(selected) >= max_k:
            break
    return selected


def format_block(lessons: list[dict]) -> str:
    """Render the selected lessons as the injectable RÈGLES ACTIVES body (Phase 3 uses this)."""
    lines = [f"- [{d['id']} · {d['source_defect']} · sev{d['severity']}] {d['rule']}" for d in lessons]
    return "\n".join(lines)


if __name__ == "__main__":  # tiny CLI for manual inspection
    import argparse
    ap = argparse.ArgumentParser(description="select injectable lessons for a mission context")
    ap.add_argument("--project", default="")
    ap.add_argument("--mission", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--files", default="")
    ap.add_argument("--max-tokens", type=int, default=800)
    a = ap.parse_args()
    sel = select_lessons(a.project, a.mission,
                         [t for t in a.tags.split(",") if t],
                         [f for f in a.files.split(",") if f],
                         max_tokens=a.max_tokens)
    print(f"# {len(sel)} lessons · ~{sum(_estimate_tokens(d) for d in sel)} tokens")
    print(format_block(sel))
