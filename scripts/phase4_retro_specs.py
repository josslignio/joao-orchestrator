#!/usr/bin/env python3
"""B-36 — retroactive Phase 0: generate PROJECT_SPEC + PROJECT_MEMORY for the live products.

Distils the existing knowledge (graven authorities, brand decisions, the ledger) into a
signable spec for each product. The spec is born UNSIGNED (`SIGNÉ : ❌ EN ATTENTE`) — the Boss
signs it in the UI (Kickoff.sign), NEVER auto-signed (LOI 1 / silence ≠ GO). A pre-mortem of
the 10 known failure modes is pulled from the Phase-2 selector.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "memory"))
import select_lessons  # noqa: E402

PROJECTS = REPO / "projects"
SIGNED_PENDING = "SIGNÉ : ❌ EN ATTENTE"

SPECS = {
    "joao": {
        "produit": "JOÃO — orchestrateur local qui transforme une intention en code livré, review indépendante et validation humaine, à coût minimal (cascade GLM→best-of-N→Claude) et sans jamais mentir.",
        "probleme": "Faire livrer des IA de code de façon FIABLE (spec nette + exécution réelle + review croisée + mémoire des erreurs), là où un LLM seul se trompe.",
        "autorites": "UI bubble (rendu validé Boss), rapports de run (section NON VÉRIFIÉ obligatoire).",
        "regles": "Fail-closed partout ; le modèle réellement utilisé est TOUJOURS affiché ; jamais un PASS inventé ; Phase 0 obligatoire avant toute mission projet.",
        "interdits": "Commit sur une baseline gelée ; run 22 min sur une salutation ; réponse chat inventée sur un doc illisible ; builder = reviewer sur le chemin critique.",
        "build": "GLM (volume) build, Codex/Claude review adversariale, cascade pour le coût. Missions bornées.",
        "hors_scope": "Recherche web dans le chat (non branchée), génération d'images (budget), débat multi-IA.",
    },
    "job-cv-auto": {
        "produit": "CV bot — génère CV + lettre de motivation dérivés d'un master validé, et assiste la candidature.",
        "probleme": "Postuler vite et bien sans re-fabriquer la mise en page à chaque offre.",
        "autorites": "CV master = assets/cv-master/Jocelyn_Grosjean_CV_master.docx (sha256 831a9215…, JC-M0, validé Boss). Cover letter master = CoverLetter_master_FINAL_O1v2.docx (sha256 c034573a…, validé Boss).",
        "regles": "Tout document servi DÉRIVE du master d'autorité par duplicate-and-edit (jamais reconstruit en code) ; le dashboard BLOQUE le téléchargement si le hash de dérivation ne remonte pas au master courant.",
        "interdits": "Servir un CV non dérivé du master validé (D-042) ; traiter le silence comme un GO design (D-038) ; DOCX→PDF via Pages (aplatit — LibreOffice obligatoire).",
        "build": "Sourcing multi-ATS, resolver découverte→ATS, autofill ; revue humaine du formulaire = filet.",
        "hors_scope": "Package zip en bouton ; premium navy (RETIRED).",
    },
    "weekly-trading-radar": {
        "produit": "Radar de trading hebdomadaire — synthèse de signaux de marché.",
        "probleme": "Produire une revue hebdo reproductible sans travail manuel.",
        "autorites": "Source immuable : git@github.com:josslignio/weekly-trading-radar.git@c8d8390c8f76 (non réécrite).",
        "regles": "Données sensibles jamais commitées ; sorties reproductibles ; append-only sur les historiques.",
        "interdits": "Réécrire l'historique source ; inventer un signal non calculé.",
        "build": "Pipeline data → indicateurs → rapport ; environnement à figer.",
        "hors_scope": "Exécution d'ordres réels ; conseil financier.",
    },
}


def premortem(text: str):
    sel = select_lessons.select_lessons(project="", mission_type=text, max_tokens=800,
                                         max_k=10, lessons=select_lessons.load_lessons())
    return "\n".join(f"{i}. [{d['source_defect']}] {d['rule']}  → parade dans la spec ?"
                     for i, d in enumerate(sel, 1)) or "_(sélecteur indisponible)_"


def spec_md(name: str, s: dict) -> str:
    pm = premortem(" ".join([s["produit"], s["probleme"], s["interdits"]]))
    return (
        f"# PROJECT_SPEC — {name} · v1.0 (Phase 0 rétroactive) · 2026-07-18\n{SIGNED_PENDING}\n\n"
        f"## 1. PRODUIT\n{s['produit']}\n\n### Problème tué\n{s['probleme']}\n\n"
        f"## 2. AUTORITÉS (à valider sur rendu)\n{s['autorites']}\n\n"
        f"## 3. RÈGLES BINAIRES\n{s['regles']}\n\n"
        f"## 4. INTERDITS ABSOLUS\n{s['interdits']}\n\n"
        f"## 5. BUILD\n{s['build']}\n\n"
        f"## 6. HORS-SCOPE V1\n{s['hors_scope']}\n\n"
        f"## 7. PRE-MORTEM (10 échecs connus → parade)\n{pm}\n\n"
        f"## AVENANTS\n_(aucun — spec rétroactive, à signer par le Boss dans l'UI)_\n")


def memory_md(name: str, s: dict) -> str:
    return (f"# PROJECT_MEMORY — {name}\n_Injecté en entête de chaque mission. Mis à jour à chaque rétro._\n\n"
            f"## Autorités\n{s['autorites']}\n\n## Interdits absolus\n{s['interdits']}\n")


def main():
    written = []
    for name, s in SPECS.items():
        folder = PROJECTS / name; folder.mkdir(parents=True, exist_ok=True)
        (folder / "PROJECT_SPEC.md").write_text(spec_md(name, s), encoding="utf-8")
        (folder / "PROJECT_MEMORY.md").write_text(memory_md(name, s), encoding="utf-8")
        written.append(name)
        print(f"generated projects/{name}/PROJECT_SPEC.md (UNSIGNED) + PROJECT_MEMORY.md")
    print(f"\n{len(written)} specs générées, NON signées (le Boss signe dans l'UI). Projets: {written}")


if __name__ == "__main__":
    main()
