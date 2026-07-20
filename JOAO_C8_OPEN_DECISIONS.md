# JOÃO C-8 — DÉCISIONS BOSS (v2 — D1–D7 tranchées)

Phase 0. En v1 c'étaient 7 questions ouvertes. Le contre-audit GPT a recommandé des réponses ; le Boss les **adopte** (instruction du 2026-07-20, action #6 « inscrire les décisions D1–D7 »). Elles sont donc **tranchées** ci-dessous et re-répercutées dans les autres documents. Elles restent révisables par le Boss, mais ne bloquent plus le GO BUILD de C8-A.

| # | Décision (tranchée) | Répercussion |
|---|---|---|
| **D1** | `risk_tier` **obligatoire** : une mission sans `risk_tier` explicite → **BLOCK** (fail-closed, jamais un défaut `normal` silencieux). | G-DBL-AUDIT, SPEC §6, WORKER §3, contrôleur `start()`. |
| **D2** | Escalade automatique **déterministe par chemins protégés** (ex. toucher `bubble/promotion.py`, `reviewer_contract.py`, `execution_backend.py`, chemins credential-adjacents → `critical`), **jamais** une heuristique de « criticité ». Reste dans D-046 : c'est une règle path→tier fixe, pas un jugement de sécurité. | G-FROZEN-FINISH-LINE (`frozen_mission.json.forbidden_paths`/protected), G-DBL-AUDIT. |
| **D3** | Canary **automatisé accepté** s'il est **synthétique, exact-SHA, sans effet externe réel**. Pas d'obligation d'observation humaine temps réel pour un canary synthétique borné. | G-CANARY-FIRST, WORKER §2.2/§9. |
| **D4** | Désaccord reviewer (un ACCEPT, un BLOCK/P1) sur un run critique → **BLOCK + notification Boss**, **aucun tie-break automatique**, aucun 3ᵉ reviewer auto-convoqué pour trancher. | G-DBL-AUDIT, contrôleur. |
| **D5** | « Boss GO = texte d'instruction nommé » = **MVP accepté**, mais désormais **hashé et lié au run** (le SHA du texte d'autorité est enregistré dans le run, comme `frozen_mission.json.spec_sha`). Pas de schéma de signature cryptographique (hors scope, zéro coût). | WORKER §3, `frozen_mission.json`. |
| **D6** | **Autonomie autorisée entre les gates** pour les runs **synthétiques/normal** ; **promotion et effets externes restent Boss-controlled**. (Cohérent avec `JOAO_COURSE_CORRECTION_20260719_1.md` déc. 6 : autonomie nocturne différée jusqu'après mesure.) | SPEC §15, WORKER §9, séparation validation/approbation. |
| **D7** | Candidats/tags superseded **archivés/révoqués, jamais supprimés automatiquement** par JOÃO. (La suppression du tag `d305f57` cette session fut une action Boss-revue manuelle — elle le reste.) | G-SHA-BOUND-PROOF (traçabilité), politique de rétention. |

## Décision produit associée — tiering de SOURCE-FRESH (première vraie mission)

Le **premier** run réel `SOURCE-FRESH` est classé **exceptionnellement `critical` + `canary_required`** (double reviewer de providers distincts + canary synthétique avant tout effet). Après **3 exécutions propres**, il pourra redescendre en `normal`. Répercuté dans la ROADMAP (lot C8-C, readiness).

## Ce qui restait ouvert et ne l'est plus

Les 7 items sont tranchés. Aucune décision Boss résiduelle ne bloque C8-A. Deux points **backlog** (non bloquants) issus du contre-audit, à traiter dans le lot où ils deviennent pertinents, pas avant :
- le **3ᵉ provider reviewer automatique** (`ClaudeCLIReviewer` ou autre) pour remplacer l'étape GPT-formelle du tier critique — devient pertinent en C8-B/C8-C ;
- la fixture gelée exacte qui déterminise le test D-044 (contenu de la fixture ledger/specs) — détail d'implémentation de C8-A/M-hermétique.
