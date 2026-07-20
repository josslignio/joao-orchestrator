# JOÃO PRODUCT VALIDATION BACKLOG — PV-01…PV-09

**Statut :** docs-only, ajouté au plan C-8 le 2026-07-20 (Boss instruction : mesurer si JOÃO bat réellement le workflow manuel, pas seulement s'il ajoute des contrôles). Compagnon de `JOAO_C8_GATES_SPEC.md` §24 (*Product Superiority and Efficiency Validation*) et `JOAO_C8_GATES_ROADMAP.md`. **N'est pas le backlog produit général** (`~/Claude-HQ/IDEAS_BACKLOG.md` reste l'inbox d'idées cross-projets, jamais un lieu pour des items scopés/datés comme ceux-ci) — ce fichier est le backlog dédié, seul et unique, de la validation produit JOÃO.

Aucun item ci-dessous ne modifie, ne bloque ni ne retarde C8-A. Aucun n'est un 8ᵉ gate.

| ID | Titre | Priorité | Dépendance | Lot d'implémentation | Statut | Critère de complétion |
|---|---|---|---|---|---|---|
| **PV-01** | Schéma de benchmark et modèle d'événement (`benchmark-run-manifest.json`, `benchmark-events.jsonl`) | P1 | C8-A clos | C8-B | planned | Schéma écrit, déterministe, append-only où requis, lié au `candidate_tree` ; couvre `workflow_mode` `manual_same_stack`\|`manual_current_workflow`\|`joao` |
| **PV-02** | Instrumentation usage/tokens/quota par provider | P1 | Instrumentation C8-B (PV-01) | C8-B | planned | Capture `input/output/cache_read/cache_write tokens`, appels, retries par provider/étape ; règle `unknown` jamais `0` appliquée et testée |
| **PV-03** | Instrumentation temps humain actif / actions manuelles | P1 | C8-B (PV-01) | C8-B | planned | `human_active_seconds`, `manual_action_count`, `context_switch_count` capturés ; attente machine explicitement exclue du temps actif |
| **PV-04** | Harnais baseline manuelle — même stack (BASELINE A) | P1 | Instrumentation C8-B (PV-01 à PV-03) | C8-B | planned | Permet de rejouer un run manuel avec mêmes builder/reviewers/SHA/SPEC que JOÃO, instrumenté avec le même schéma |
| **PV-05** | Capture baseline — workflow manuel actuel (BASELINE B) | **P1** (relevé de P2 — §24.2/24.6 : `JOAO_PROVEN` est impossible tant que BASELINE B n'a jamais été mesurée, elle n'est donc pas secondaire) | C8-B (PV-01 à PV-03) | C8-B | planned | Capture Claude Code build → revue manuelle Claude → copié-collé GPT → vérifications Git/pytest/SHA manuelles, avec le même schéma d'événements que JOÃO ; si non praticable sur le synthétique C8-C, mesurée au plus tard sur la 1ʳᵉ mission réelle (jamais omise) |
| **PV-06** | Benchmark synthétique aveugle manuel vs JOÃO | P1 | E2E C8-C vert | C8-C | planned | `JOAO_BENCHMARK_SCORECARD.json` + `JOAO_BENCHMARK_REPORT.md` produits ; évaluation qualité finale aveugle ; seuils §24.6 (premier benchmark) évalués explicitement PASS/FAIL, échantillon=1 documenté comme provisoire |
| **PV-07** | Cohorte des 3 premières missions Job Radar (Competitor/OSS, SOURCE-FRESH, CONTENT) | P1 | Gel candidat C-8 + double PASS Codex (23) + merge | Post-C8 | planned | Données manuel/JOÃO comparables capturées par mission (sous-missions appariées si le doublon complet gaspillerait du temps) ; seuils §24.6 (« après les 3 premières missions ») évalués |
| **PV-08** | Décision produit à 10 missions réelles | P1 | 10 missions réelles instrumentées (PV-07 + suite) | Post-C8 | planned | Décision formelle rendue : `JOAO_PROVEN` \| `JOAO_HYBRID_RECOMMENDED` \| `JOAO_SIMPLIFY` \| `INSUFFICIENT_EVIDENCE`, seuils §24.6 appliqués sans exception |
| **PV-09** | Simplification / fallback hybride | P2 (conditionnel) | Déclenché par la condition STOP §24.7 (après 3 missions réelles) | Post-C8 (si déclenché) | planned | Si déclenché : investigation de l'origine du surcoût, retrait/fusion des étapes à faible valeur, ou passage à un workflow hybride documenté — jamais une nouvelle couche d'architecture pour faire passer le benchmark |

## Règles transverses (s'appliquent à tous les items ci-dessus)

- Aucune valeur de token/usage non mesurée n'est jamais estimée ou mise à `0` : champ `unknown`, systématiquement.
- Aucun item PV n'autorise à réduire la portée de revue/preuve d'un run JOÃO pour améliorer artificiellement son score de benchmark (`JOAO_C8_GATES_SPEC.md` §24.8, anti-gaming).
- Chaque comparaison manuel/JOÃO utilise le protocole juste §24.3 (même SHA, même SPEC, mêmes critères, ordre anti-contamination, évaluation aveugle quand praticable).
- Un échec de sécurité dur (corruption candidat/preuve, promotion au mauvais SHA, P0/P1 échappé imputable à l'orchestration) bloque toujours `JOAO_PROVEN`, indépendamment de la vitesse.

## Traçabilité

Source d'autorité des seuils et définitions : `JOAO_C8_GATES_SPEC.md` §24. Intégration roadmap : `JOAO_C8_GATES_ROADMAP.md` (lots C8-B, C8-C, section « Validation produit post-C8 »). Politique adoptée (non un item ouvert) : `JOAO_C8_OPEN_DECISIONS.md`, section *Product Superiority and Efficiency Validation*.
