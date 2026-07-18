#!/usr/bin/env python3
"""B-28 Phase 1 — import DEFECTS_LEDGER.md → machine-readable injectable lessons.

Parses ~/Claude-HQ/DEFECTS_LEDGER.md (D-001…D-042, across every markdown table) plus the
6 LOIS from JOAO_ANALYSE_SYSTEMIQUE into `memory/lessons.jsonl`, one lesson per line:

  {id, date, project, source_defect, tags[], severity(1-3),
   rule (imperative), applies_to[planner|builder|reviewer],
   trigger_contexts[], recurrences}

IDs are SYSTEM-attributed (L-001…) — never by a run (D-029). The D-xxx mapping is kept in
`source_defect`. Append-only (D-016 governance): a re-run adds only lessons whose
source_defect is not already present; existing lines are never rewritten. Deterministic,
stdlib only.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LESSONS = REPO / "memory" / "lessons.jsonl"
LEDGER = Path.home() / "Claude-HQ" / "DEFECTS_LEDGER.md"
ANALYSIS = Path.home() / "Claude-HQ" / "JOAO_ANALYSE_SYSTEMIQUE_POURQUOI_CA_LIVRE_PAS.md"

# cause-racine (root cause) → which prompt stages the lesson applies to.
CAUSE_TO_STAGES = {
    "PROMPT": ["planner"], "CARTE": ["planner"],
    "MOTEUR": ["builder"], "CODE": ["builder"],
    "REVIEW": ["reviewer"], "GATE": ["reviewer", "builder"],
    "UX": ["builder", "reviewer"], "CÂBLAGE": ["builder", "reviewer"], "CABLAGE": ["builder", "reviewer"],
    "ENVIRONNEMENT": ["builder"], "PROCESS": ["planner", "reviewer"],
}
# domain keywords → tags (deterministic).
TAG_RULES = {
    "ui": r"\b(ui|dashboard|bouton|button|clic|click|carte|interface)\b",
    "cablage": r"\b(câbl|cabl|wired|e2e|interaction)\b",
    "geo": r"\b(géo|geo|remote|office|bureau|hybrid|location|éligib|eligib)\b",
    "docs": r"\b(docx|cv\b|lettre|letter|master|template|render|rendu|visuel|design|mockup)\b",
    "pdf": r"\b(pdf|libreoffice|pages|convert)\b",
    "sourcing": r"\b(sourcing|slug|ats|greenhouse|ashby|lever|découverte|discovery|offre)\b",
    "git": r"\b(git|commit|push|branch|hash|sha)\b",
    "async": r"\b(async|thread|course|race|await|concurren)\b",
    "gate": r"\b(gate|test|vert|pass|non-régression|regression|couverture)\b",
    "placeholder": r"\b(placeholder|deviné|guess|vide|blank|linkedin)\b",
    "authority": r"\b(autorité|authority|master|référence|reference|validé|approuvé|approved)\b",
    "process": r"\b(process|gouvernance|numérotation|silence|approbation|ordre)\b",
    "salary": r"\b(salaire|salary)\b",
    "index": r"\b(index|offset|slicing|arithmétique)\b",
    "report": r"\b(rapport|report|non vérifié|all pass|déclar)\b",
}


# A few ledger entries live in tables with NO "leçon/règle" column (bug tables D-018..024,
# prose sections D-042/D-016) — the row's remedy is stated in the ledger's PROSE instead.
# These overrides transcribe that prose remedy verbatim into a crisp imperative. Sourced,
# not invented: each is the "Remède"/"règle permanente" line the ledger already carries.
RULE_OVERRIDES = {
    "D-016": {"rule": "Jamais une session à 100% de contexte : compacter, découper en sous-agents "
                      "et missions bornées (checkpoint CC-5).", "severity": 2},
    "D-018": {"rule": "Toute logique asynchrone (await, threads, ordre d'exécution) exige une "
                      "self-review adversariale dédiée aux courses temporelles — les tests ne les "
                      "attrapent pas ; review croisée indépendante obligatoire.", "severity": 2},
    "D-021": {"rule": "Un générateur d'artefact dérive TOUJOURS du master d'autorité par "
                      "duplicate-and-edit ; jamais recâbler sur l'ancien contenu ni reconstruire "
                      "la mise en page en code (contrat master = source unique).", "severity": 2},
    "D-042": {"rule": "Tout artefact servi à l'humain dérive du master d'autorité courant par "
                      "duplicate-and-edit (jamais reconstruit en code = récidive D-021 → alarme "
                      "rouge) ; la tour de contrôle est soumise aux mêmes règles, silence ≠ GO.",
              "severity": 3},
}


def _tags(text: str) -> list[str]:
    low = text.lower()
    out = [tag for tag, pat in TAG_RULES.items() if re.search(pat, low)]
    return out or ["general"]


def _stages(cause: str) -> list[str]:
    up = cause.upper()
    stages: list[str] = []
    for key, sts in CAUSE_TO_STAGES.items():
        if key in up:
            for s in sts:
                if s not in stages:
                    stages.append(s)
    return stages or ["planner", "builder", "reviewer"]


def _severity(row_text: str, status: str) -> int:
    t = row_text.lower()
    if "🔴🔴" in row_text or "méta" in t or "rupture" in t or "mensonge" in t:
        return 3
    if "🔴" in row_text or "grave" in t or "jamais" in t or "règle gravée" in t or "règle permanente" in t:
        return 3
    if "mineur" in t or ("fixé" in t and "règle" not in t):
        return 1
    return 2


def _clean_rule(rule: str) -> str:
    rule = re.sub(r"\s+", " ", rule).strip().strip("·").strip()
    # strip markdown bold and trailing test/status noise
    rule = rule.replace("**", "")
    return rule[:400]


def _strip_md(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.replace("**", "")).strip()


def parse_ledger(text: str) -> list[dict]:
    """Extract every D-xxx row from all markdown tables in the ledger."""
    out = []
    # match a table row that starts with a D-xxx id
    row_re = re.compile(r"^\|\s*(D-\d{3})\s*\|(.+)\|\s*$", re.M)
    for m in row_re.finditer(text):
        did = m.group(1)
        cells = [c.strip() for c in m.group(2).split("|")]
        # tables have variable shapes; find the cause + rule heuristically
        # common shape: produit | symptôme | cause racine | leçon/règle | (nonreg) | statut
        produit = cells[0] if cells else "JOÃO"
        symptom = cells[1] if len(cells) > 1 else ""

        def _is_cause_label(cell):
            head = _strip_md(cell).upper().lstrip("🔴 ").strip()
            return any(head.startswith(k) for k in CAUSE_TO_STAGES)

        def _is_status(cell):
            return bool(re.match(r"^(FIXÉ|OUVERT|CLOS|DOCUMENTÉ|RÈGLE|BLOQUÉ|LARGEMENT|RESTAURATION)",
                                 _strip_md(cell).upper()))
        # cause = the cell (index >= 2, past produit/symptom) that STARTS with a root-cause label
        cause, cidx = "", -1
        for i, c in enumerate(cells):
            if i >= 2 and _is_cause_label(c):
                cause, cidx = c, i
                break
        # rule = the next non-status, non-cause-label cell after the cause
        rule = ""
        for c in cells[cidx + 1:] if cidx >= 0 else cells[2:]:
            if c and not _is_status(c) and not _is_cause_label(c):
                rule = c
                break
        if not rule:  # fallback: longest non-status, non-cause cell, else the symptom
            rest = [c for c in cells[2:] if c and not _is_status(c) and not _is_cause_label(c)]
            rule = max(rest, key=len) if rest else symptom
        status = cells[-1] if cells else ""
        row_text = m.group(0)
        override = RULE_OVERRIDES.get(did, {})
        final_rule = override.get("rule") or _clean_rule(rule) or _clean_rule(symptom)
        severity = override.get("severity") or _severity(row_text, status)
        out.append({
            "source_defect": did, "project": _project(produit),
            "rule": final_rule,
            "tags": _tags(symptom + " " + final_rule + " " + cause),
            "severity": severity,
            "applies_to": _stages(cause),
            "trigger_contexts": _tags(symptom + " " + final_rule),
        })
    return out


def _project(produit: str) -> str:
    p = produit.lower()
    if "cv" in p:
        return "job-cv-auto"
    if "joão" in p or "joao" in p:
        return "joao"
    return "all"


def parse_laws(text: str) -> list[dict]:
    """The 6 LOIS from the systemic analysis → systemic lessons (severity 3, all stages)."""
    out = []
    law_re = re.compile(r"\*\*LOI\s+(\d+)\s+—\s+([^.*]+?)\.?\*\*\s+(.+?)(?=\n\*\*LOI|\Z)", re.S)
    for m in law_re.finditer(text):
        num, title, body = m.group(1), m.group(2).strip(), _strip_md(m.group(3))[:300]
        out.append({
            "source_defect": f"LOI-{num}", "project": "all",
            "rule": f"{title}. {body}"[:400],
            "tags": _tags(title + " " + body) + ["loi"],
            "severity": 3,
            "applies_to": ["planner", "builder", "reviewer"],
            "trigger_contexts": _tags(title + " " + body),
        })
    return out


def load_existing() -> dict:
    if not LESSONS.is_file():
        return {}
    out = {}
    for line in LESSONS.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                d = json.loads(line)
                out[d["source_defect"]] = d
            except (json.JSONDecodeError, KeyError):
                continue
    return out


def main():
    if not LEDGER.is_file():
        print(f"ledger not found: {LEDGER}"); return 2
    ledger_lessons = parse_ledger(LEDGER.read_text())
    law_lessons = parse_laws(ANALYSIS.read_text()) if ANALYSIS.is_file() else []
    candidates = law_lessons + ledger_lessons  # laws first → stable L-001.. for systemic

    existing = load_existing()
    # stable id assignment: keep existing ids, append new ones by next free number
    used_nums = sorted(int(d["id"].split("-")[1]) for d in existing.values() if d.get("id", "").startswith("L-"))
    next_num = (used_nums[-1] + 1) if used_nums else 1
    today = date(2026, 7, 18).isoformat()
    added = 0
    lines = [json.dumps(d, ensure_ascii=False, sort_keys=True) for d in existing.values()]
    seen = set(existing)
    for c in candidates:
        if c["source_defect"] in seen:
            continue
        seen.add(c["source_defect"])
        c = {"id": f"L-{next_num:03d}", "date": today, "recurrences": 0, **c}
        next_num += 1
        added += 1
        lines.append(json.dumps(c, ensure_ascii=False, sort_keys=True))
    LESSONS.write_text("\n".join(lines) + ("\n" if lines else ""))
    total = len(existing) + added
    print(f"imported {added} new lessons ({total} total) → {LESSONS}")
    print(f"  from ledger: {len(ledger_lessons)} · from 6 LOIS: {len(law_lessons)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
