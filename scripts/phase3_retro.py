#!/usr/bin/env python3
"""PHASE 3 — global proof-of-life retro via the CERVEAU's own memory/retro.py.

Runs the L7 retro through the system built in the CERVEAU run: renders the spec→result diff,
proposes candidate lessons WITHOUT ids (the SYSTEM assigns ids, never the run — D-029), runs
the recurrence detector against the committed ledger, and updates runs_until_perfect. Nothing
is auto-graven into the committed brain: candidates are SUBMITTED (D-029), metrics land in a
state dir, not in memory/lessons.jsonl.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "memory"))
import retro  # noqa: E402

RUN_ID = "BIG-RUN-LA-SUITE"
PROJECT = "joao"
ACCEPT = Path("~/.local/share/joao/acceptance").expanduser()
OUT = ACCEPT / f"bigrun-suite-retro-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
OUT.mkdir(parents=True, exist_ok=True)


def latest(prefix: str):
    dirs = sorted(ACCEPT.glob(prefix + "*"))
    return dirs[-1] if dirs else None


# candidate lessons distilled from THIS run — SANS id (the system assigns L-nnn), submitted to Boss
CANDIDATES = [
    {"rule": "Tout câblage subprocess best-of-N restaure le worktree dans un try/finally ET coupe "
             "le process-group au timeout ; le WIP non suivi pré-existant est snapshotté puis restauré "
             "(jamais wipe silencieux).",
     "applies_to": ["builder"], "tags": ["async", "subprocess", "cascade", "worktree"], "severity": 3,
     "root_cause": "MOTEUR"},
    {"rule": "Un routeur d'intention tranche par heuristique déterministe d'abord ; l'ambigu affiche "
             "2 boutons (💬/🛠️), jamais un run à l'aveugle sur un doute.",
     "applies_to": ["planner"], "tags": ["chat", "routing", "ux"], "severity": 2, "root_cause": "PROMPT/CARTE"},
    {"rule": "Toute réponse chat sur un document s'appuie sur le TEXTE RÉELLEMENT EXTRAIT (pypdf/lecture) "
             "et le cite ; extraction impossible = dit honnêtement, jamais un chiffre/contenu inventé.",
     "applies_to": ["builder", "reviewer"], "tags": ["chat", "honnêteté", "pdf", "extraction"], "severity": 3,
     "root_cause": "PROMPT/CARTE"},
]


def main():
    retro.set_state_dir(OUT / "brain-state")  # never touch the committed brain during the run
    g1 = latest("bigrun-suite-gate1-")
    g2 = latest("bigrun-suite-gate2-")
    g1s = json.loads((g1 / "GATE1_SUMMARY.json").read_text()) if g1 else {}
    g2s = json.loads((g2 / "GATE2_SUMMARY.json").read_text()) if g2 else {}

    spec = ("Fermer L9.3 (cascade branchée au réel) + livrer V2.2 Chat Era (routeur d'intention, "
            "cerveau chat via CLIs, extraction déterministe, anti-mensonge), + fold-ins audit.")
    result = (f"GATE1 best-of-3 GLM réel ok={g1s.get('demo_b_best_of_3_real_glm', {}).get('ok')}, "
              f"GATE2 greeting intent={g2s.get('demo1_greeting', {}).get('intent')} / "
              f"pdf grounded={g2s.get('demo2_pdf_grounded', {}).get('answer_contains_4242')}.")

    # honest perfection call: the subprocess wiring needed ONE self-review-driven fix pass
    # (P1-A/P2-A/P2-C caught by adversarial review) → not a 0-loop-perfect run.
    perfect = False
    retro.record_run_metric(PROJECT, RUN_ID, perfect=perfect, at=datetime.now(timezone.utc).isoformat())

    # recurrence detection against the committed ledger (read-only)
    recurrence_report = []
    for c in CANDIDATES:
        hit = retro.detect_recurrence(c)
        recurrence_report.append({"candidate": c["rule"][:70],
                                  "recurs_existing": (hit or {}).get("id"),
                                  "existing_rule": (hit or {}).get("rule", "")[:80]})

    template = retro.render_retro_template(PROJECT, RUN_ID, "BIG RUN LA SUITE", spec=spec, result=result)
    rup = retro.runs_until_perfect(PROJECT)

    retro_md = [template, "", "## LEÇONS CANDIDATES (SANS id — soumises au Boss, D-029)"]
    for c in CANDIDATES:
        retro_md.append(f"- rule: {c['rule']}\n  applies_to: {c['applies_to']} | tags: {c['tags']} | severity: {c['severity']}")
    retro_md.append("\n## DÉTECTEUR DE RÉCIDIVE (contre le ledger committé)")
    for r in recurrence_report:
        mark = f"⚠️ proche de {r['recurs_existing']}" if r["recurs_existing"] else "✅ nouvelle (aucune récidive)"
        retro_md.append(f"- {mark} — {r['candidate']}…")
    retro_md.append(f"\n## MÉTRIQUE\n- runs_until_perfect({PROJECT}) = **{rup}** (perfect={perfect} : "
                    f"1 passe de correction issue de la self-review adversariale — honnête, pas 0-loop).")

    (OUT / "RETRO_LA_SUITE.md").write_text("\n".join(retro_md), encoding="utf-8")
    out = {"retro_dir": str(OUT), "runs_until_perfect": rup, "perfect": perfect,
           "candidates_without_id": len(CANDIDATES), "recurrence_report": recurrence_report,
           "gate1_dir": str(g1) if g1 else None, "gate2_dir": str(g2) if g2 else None}
    (OUT / "retro-summary.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
