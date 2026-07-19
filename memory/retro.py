#!/usr/bin/env python3
"""B-28 Phase 4 — THE LOOP: end-of-mission retro, recurrence detection, project memory.

The mission is only finished when it has fed the brain. This module:

  1. renders the end-of-mission RETRO template (spec→result diff, root-cause taxonomy,
     candidate lessons WITHOUT id — the SYSTEM assigns the id, never the run: D-029);
  2. appends genuinely-new candidate lessons to memory/lessons.jsonl (stable L-nnn ids);
  3. DETECTS RECURRENCE — a candidate that matches a lesson already in the ledger means a
     defect the injection system failed to prevent → it does NOT create a new lesson;
     instead it bumps the lesson's recurrence count and raises a 🔴 alert
     ("récidive = échec du système d'injection", not a failure of the run);
  4. tracks the `runs_until_perfect` metric per project and refreshes PROJECT_MEMORY.md.

APPEND-ONLY INVARIANT (D-016): lessons.jsonl is never rewritten. Recurrence counts live in
a SEPARATE append-only overlay log (recurrences.jsonl); the effective count is the imported
baseline plus the overlay. Deterministic, stdlib only, no clock in the scoring path.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_ledger import LESSONS, _stages, _tags  # noqa: E402  (reuse the stock helpers)

MEM = Path(__file__).resolve().parent
RECURRENCES = MEM / "recurrences.jsonl"
METRICS = MEM / "run_metrics.jsonl"
PROJECTS = MEM.parent / "projects"

TAXONOMY = ["PROMPT/CARTE", "MOTEUR", "REVIEW", "GATE", "UX/CÂBLAGE", "PROCESS", "ENVIRONNEMENT"]


def set_state_dir(base, *, lessons_path=None) -> None:
    """Redirect the runtime-state files (recurrences, metrics, project memory) under `base`.

    The shared lessons.jsonl STOCK is normally left where it is (the brain's canon):
    only per-run/per-project runtime state moves, so a live runtime keeps its metrics with
    its own state_root and never writes into the committed brain during a run.

    A0.2 (§7 memory isolation): pass `lessons_path` to ALSO redirect the write
    target for `ingest_candidates`/`close_mission` — used by the test suite
    (`JOAO_MEMORY_DIR`-backed isolation, see tests/conftest.py) so a run
    executed under test can never append into the real, committed
    `memory/lessons.jsonl`, deterministic or not.
    """
    global RECURRENCES, METRICS, PROJECTS, LESSONS
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    RECURRENCES = base / "recurrences.jsonl"
    METRICS = base / "run_metrics.jsonl"
    PROJECTS = base / "projects"
    if lessons_path is not None:
        LESSONS = Path(lessons_path)

_STOP = set("le la les de des du un une et ou à a au aux en dans par pour sur avec sans "
            "que qui est sont doit tout toute jamais pas ne plus se son sa ses ce cette".split())


# ─────────────────────────── retro template ───────────────────────────
RETRO_TEMPLATE = """# RÉTRO — {mission_title}
_projet : {project} · run : {run_id}_

## 1. SPEC → RÉSULTAT (l'écart)
- Attendu (spec) : {spec}
- Obtenu (résultat) : {result}
- Écart : <décrire chaque écart, ou « aucun »>

## 2. CAUSE RACINE (taxonomie — cocher + expliquer)
{taxonomy_block}

## 3. LEÇONS CANDIDATES (SANS id — le système attribue L-nnn)
<pour chaque défaut : règle impérative courte · applies_to[planner|builder|reviewer] · tags · sévérité 1-3>
- rule: ... | applies_to: ... | tags: ... | severity: ...

## 4. RÉCIDIVES
<le détecteur remplit : 🔴 si une leçon candidate existe déjà au ledger>

## 5. MÉTRIQUE
- runs_until_perfect ({project}) : {rup}
"""


def render_retro_template(project: str, run_id: str, mission_title: str,
                          spec: str = "", result: str = "") -> str:
    taxonomy_block = "\n".join(f"- [ ] {c}" for c in TAXONOMY)
    return RETRO_TEMPLATE.format(
        mission_title=mission_title, project=project, run_id=run_id,
        spec=spec or "<...>", result=result or "<...>",
        taxonomy_block=taxonomy_block, rup=runs_until_perfect(project))


# ─────────────────────────── recurrence fingerprint ───────────────────────────
def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-zàâçéèêëîïôûùüÿœ0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOP}


def _similar(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def load_lessons() -> list[dict]:
    if not LESSONS.is_file():
        return []
    return [json.loads(x) for x in LESSONS.read_text().splitlines() if x.strip()]


def detect_recurrence(candidate: dict, lessons: list[dict] | None = None,
                      threshold: float = 0.6) -> dict | None:
    """Return the existing lesson a candidate recurs, or None. Deterministic (id tie-break)."""
    pool = lessons if lessons is not None else load_lessons()
    crule, ctags = candidate.get("rule", ""), set(candidate.get("tags", []))
    csrc = candidate.get("source_defect")
    best, best_score = None, 0.0
    for d in sorted(pool, key=lambda x: x.get("id", "")):
        if csrc and d.get("source_defect") == csrc:
            return d
        sim = _similar(crule, d.get("rule", ""))
        tag_overlap = bool(ctags & set(d.get("tags", []))) or not ctags
        if sim >= threshold and tag_overlap and sim > best_score:
            best, best_score = d, sim
    return best


# ─────────────────────────── recurrence overlay (append-only) ───────────────────────────
def record_recurrence(lesson_id: str, *, project: str, run_id: str, candidate_rule: str,
                      at: str = "") -> dict:
    """Append a recurrence event (never rewrites lessons.jsonl) and return the 🔴 alert."""
    event = {"lesson_id": lesson_id, "project": project, "run_id": run_id,
             "candidate_rule": candidate_rule, "at": at}
    with RECURRENCES.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    count = recurrence_counts().get(lesson_id, 0)
    return {
        "level": "🔴",
        "lesson_id": lesson_id,
        "recurrences": count,
        "message": (f"🔴 RÉCIDIVE de {lesson_id} — une leçon DÉJÀ au ledger a été re-violée : "
                    f"c'est un échec du SYSTÈME D'INJECTION, pas du run. "
                    f"(récidives cumulées : {count})"),
    }


def recurrence_counts() -> dict[str, int]:
    if not RECURRENCES.is_file():
        return {}
    counts: dict[str, int] = {}
    for line in RECURRENCES.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                counts[json.loads(line)["lesson_id"]] = counts.get(json.loads(line)["lesson_id"], 0) + 1
            except (json.JSONDecodeError, KeyError):
                continue
    return counts


def effective_recurrences(lesson: dict) -> int:
    """Baseline (imported) recurrences plus every overlay event."""
    return int(lesson.get("recurrences", 0)) + recurrence_counts().get(lesson.get("id", ""), 0)


# ─────────────────────────── append new lessons (system-assigned ids) ───────────────────────────
def _next_id(lessons: list[dict]) -> int:
    used = [int(d["id"].split("-")[1]) for d in lessons if d.get("id", "").startswith("L-")]
    return (max(used) + 1) if used else 1


def ingest_candidates(candidates: list[dict], *, project: str, run_id: str,
                      date: str = "", at: str = "") -> dict:
    """Recurrence-check then append genuinely-new candidates. Returns a structured result.

    New candidates → appended with a fresh L-nnn (system-attributed, never by the run: D-029).
    Recurring candidates → NO new lesson; a recurrence event + 🔴 alert instead.
    """
    lessons = load_lessons()
    next_num = _next_id(lessons)
    added, alerts = [], []
    new_lines = []
    for c in candidates:
        hit = detect_recurrence(c, lessons)
        if hit is not None:
            alerts.append(record_recurrence(hit["id"], project=project, run_id=run_id,
                                            candidate_rule=c.get("rule", ""), at=at))
            continue
        rule = (c.get("rule") or "").strip()
        if not rule:
            continue
        lesson = {
            "id": f"L-{next_num:03d}", "date": date,
            "project": project or c.get("project", "all"),
            "source_defect": c.get("source_defect") or f"RETRO-{run_id}-{next_num:03d}",
            "rule": rule,
            "tags": c.get("tags") or _tags(rule),
            "severity": int(c.get("severity", 2)),
            "applies_to": c.get("applies_to") or _stages(c.get("root_cause", "")),
            "trigger_contexts": c.get("trigger_contexts") or (c.get("tags") or _tags(rule)),
            "recurrences": 0,
        }
        lessons.append(lesson)  # so subsequent candidates recurrence-check against it too
        new_lines.append(json.dumps(lesson, ensure_ascii=False, sort_keys=True))
        added.append(lesson)
        next_num += 1
    if new_lines:  # append-only: only ever adds lines
        with LESSONS.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(new_lines) + "\n")
    return {"added": added, "recurrence_alerts": alerts}


# ─────────────────────────── runs_until_perfect metric ───────────────────────────
def record_run_metric(project: str, run_id: str, *, perfect: bool, recurrences: int = 0,
                      at: str = "") -> None:
    event = {"project": project, "run_id": run_id, "perfect": bool(perfect),
             "recurrences": recurrences, "at": at}
    with METRICS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _project_runs(project: str) -> list[dict]:
    if not METRICS.is_file():
        return []
    out: list[dict] = []
    seen: dict[str, int] = {}  # dedupe by run_id, last occurrence wins
    for line in METRICS.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                d = json.loads(line)
                if d.get("project") != project:
                    continue
                rid = d.get("run_id", "")
                if rid in seen:
                    out[seen[rid]] = d
                else:
                    seen[rid] = len(out)
                    out.append(d)
            except json.JSONDecodeError:
                continue
    return out


def runs_until_perfect(project: str) -> int:
    """Consecutive non-perfect runs up to and including the latest (0 = latest was perfect)."""
    runs = _project_runs(project)
    streak = 0
    for d in reversed(runs):
        if d.get("perfect"):
            break
        streak += 1
    return streak


# ─────────────────────────── PROJECT_MEMORY.md ───────────────────────────
@dataclass
class RetroOutcome:
    project: str
    run_id: str
    added: list[dict] = field(default_factory=list)
    recurrence_alerts: list[dict] = field(default_factory=list)
    runs_until_perfect: int = 0


def update_project_memory(project: str, *, run_id: str = "", added: list[dict] | None = None,
                          alerts: list[dict] | None = None) -> Path:
    """Refresh projects/<project>/PROJECT_MEMORY.md with the latest retro state."""
    folder = PROJECTS / project
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "PROJECT_MEMORY.md"
    rup = runs_until_perfect(project)
    lines = [f"# PROJECT_MEMORY — {project}", ""]
    lines.append(f"- runs_until_perfect : **{rup}**")
    lines.append(f"- dernier run : {run_id or '<n/a>'}")
    if alerts:
        lines.append("")
        lines.append("## 🔴 RÉCIDIVES (échecs du système d'injection)")
        lines += [f"- {a['message']}" for a in alerts]
    if added:
        lines.append("")
        lines.append("## Leçons ajoutées par la dernière rétro")
        lines += [f"- [{l['id']}] {l['rule']}" for l in added]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def close_mission(project: str, run_id: str, candidates: list[dict], *,
                  perfect: bool = True, date: str = "", at: str = "") -> RetroOutcome:
    """End-to-end retro: ingest candidates, detect recurrence, record metric, refresh memory."""
    result = ingest_candidates(candidates, project=project, run_id=run_id, date=date, at=at)
    had_recurrence = bool(result["recurrence_alerts"])
    record_run_metric(project, run_id, perfect=perfect and not had_recurrence,
                      recurrences=len(result["recurrence_alerts"]), at=at)
    update_project_memory(project, run_id=run_id, added=result["added"],
                          alerts=result["recurrence_alerts"])
    return RetroOutcome(project=project, run_id=run_id, added=result["added"],
                        recurrence_alerts=result["recurrence_alerts"],
                        runs_until_perfect=runs_until_perfect(project))
