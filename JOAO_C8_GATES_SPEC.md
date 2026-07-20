# JOÃO C-8 GATES + WORKER INTEGRATION — SPEC (v2, post GPT counter-audit)

**Phase 0 — SPEC + ROADMAP ONLY.** Branche `feat/joao-c8-gates-spec`, base `10156a2`.
A0.2 gelé `cc796b55808b95210adf9d219ecd91a240b2676c` (tree `7ae68454…`, `PROVISIONALLY_CLOSED`, Codex `PENDING_2026-07-23`) — read-only, non modifié.
Compagnons : `JOAO_C8_GATE_CONTRACTS.md`, `JOAO_WORKER_INTEGRATION_SPEC.md`, `JOAO_C8_GATES_ROADMAP.md`, `JOAO_C8_OPEN_DECISIONS.md`.
**v2 :** corrige les 4 findings bloquants GPT + sépare validation-technique/approbation-Boss + inscrit D1–D7 + compresse la roadmap en 3 lots (7 gates conservés).

## 1. Goals

1. Convertir les 7 leçons C-8 en gates **exécutables, fail-closed**, à reason codes stables, câblés dans le vrai pipeline (`bubble/gates.py` + entrypoints réels).
2. Doter JOÃO d'un `GLMReviewer(ReviewerAdapter)` — la moitié manquante prouvée manuellement cette session — **utilisable comme reviewer indépendant uniquement quand le builder n'est pas GLM** (voir §6 et le paradoxe résolu de G-DBL-AUDIT).
3. Un contrôleur d'orchestration qui déroule `Boss GO → SPEC/ROADMAP approuvée → build → gates → freeze → review → canary/promotion → approbation Boss`, sans séquencer `RunRuntime` à la main.
4. Faire de la discipline appliquée à la main cette session (indépendance reviewer, tests hermétiques, preuve via vrai entrypoint, liaison SHA, canary-avant-promotion, scope gelé) une propriété du **système**.

## 2. Non-goals

Pas de 8ᵉ gate. Pas de sécurité OS / nouvelle frontière d'isolation (D-046). Pas de réécriture de `RunRuntime`/`promotion.py`/`execution_backend.py` — le contrôleur appelle les primitives existantes. Pas de SOURCE-FRESH implémenté (c'est la 1ʳᵉ mission à faire passer par ce milestone, pas une partie de sa construction). Pas de changement de `memory/lessons.jsonl` (les leçons L-048..L-059/C-8 + D-055 différées restent hors candidat — D-055). Pas de réouverture d'A0.2. Pas de nouveau mécanisme d'auth « Boss GO » cryptographique (D5 : texte nommé, hashé et lié au run).

## 3. Current-state architecture

Détail source-vérifié : `JOAO_WORKER_INTEGRATION_SPEC.md §1`. Résumé : `RunRuntime` (`bubble/runtime.py`) = machine à états réelle et testée (`RunStatus`/`NEXT`), exercée E2E par `scripts/a0_toy_mission_e2e.py`. Builders `SandboxBuilder`, `GLMBuilder` (réel) ; reviewers `CodexEvidenceReviewer` (import de preuve, défaut), `CodexCLIReviewer` (Codex live) — tous dispatchés via un seul `ExecutionBackend.execute()` (A0.2 §12.2). **Manque :** un `GLMReviewer`, et tout code qui séquence une mission de bout en bout. **Fait vérifié (finding GPT #4) :** le budget de correction réel du runtime est `"max_corrections": 1` (`runtime.py:593`, `plan.json`) — PAS 2. Le `max_repair_loops: 2` de `.joao/context_policy.yaml` est un knob de quota distinct ; il ne définit pas le budget de correction d'un run.

## 4. Target architecture

Additif seulement (G-FROZEN-FINISH-LINE + G-NO-STALE-ENTRYPOINT) :
1. `bubble/gates.py` — les 7 gates comme fonctions pures fail-closed (détail `JOAO_C8_GATE_CONTRACTS.md`).
2. `frozen_mission.json` écrit par `RunRuntime.start()` (spec_sha, roadmap_sha, forbidden_paths, risk_tier, canary_required, **`criterion_bindings`** = mapping AC→{allowed_paths, required_tests, allowed_actions}, GATE_CONTRACTS) — l'artefact qui rend **G-FROZEN-FINISH-LINE mécanique** et lie réellement chaque changement à un critère gelé (findings GPT #2 + v2 #3).
3. `GLMReviewer(ReviewerAdapter)` — nouvelle classe `bubble/runtime.py` (détail WORKER §2.1).
4. Un contrôleur d'orchestration fin (`bubble/orchestrator.py`, nom TBD) qui séquence les appels `RunRuntime` existants et **produit l'éligibilité mécanique** sans jamais fabriquer l'approbation humaine (§13).

**Précédent mémoire (D-055) :** les 7 leçons C-8 vivent déjà dans `memory/lessons.jsonl` via `639ec01` sur `feat/joao-a0-integrite` — la branche que ce travail **ne** prend PAS pour base (il part de `feat/joao-a0-integrite-clean`, qui exclut ce commit). Le contenu des leçons (les 7 noms + claims) est fourni verbatim dans l'instruction et restitué dans `JOAO_C8_GATE_CONTRACTS.md` ; convertir en gates ne requiert pas de relire ce commit.

## 5. Trust boundaries

Inchangées A0/A0.2 : la frontière de confiance est le compte OS qui exécute JOÃO, pas une frontière process/container (D-046). C-8 ajoute une frontière **de politique** (pas d'isolation technique) : le `risk_tier` (§6) qui décide combien de vérification indépendante un run exige. Builder = non fiable (sortie/hash/identité re-vérifiés, jamais d'auto-approbation). Reviewer = indépendant, provider distinct du builder. Boss = seule autorité d'approbation humaine (jamais fabriquée par le contrôleur, §13).

## 6. Risk tiers  *(D1 : obligatoire)*

- **`normal`** : borné, non sensible. Requiert **1 reviewer indépendant** (provider ≠ builder). Builder GLM → reviewer **Codex**.
- **`critical`** : sensible sécurité/release, ou escaladé par chemin protégé (D2). Requiert **2 reviewers de `provider_family` DISTINCTES, toutes deux ≠ famille du builder** (finding GPT v3 : Codex et GPT sont tous deux famille `openai` → NE comptent PAS comme 2), + canary si requis. Builder GLM (`zai`) → **Codex (`openai`) + une 2ᵉ famille ≠ openai (Claude/`anthropic`)**. Tant qu'un reviewer de famille distincte de openai (`ClaudeCLIReviewer`) n'existe pas → build GLM critical **bloqué** (`G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE`). `GPTFormalEvidenceReviewer` (`provider="openai-gpt"`, identité contrôleur, jamais via `CodexEvidenceReviewer`) reste valide en `normal` mais ne satisfait pas la distinction de familles en `critical`.

`risk_tier` est un champ de mission gelé au `start()`. **Absent → BLOCK** (D1, jamais un défaut silencieux). Escalade auto = déterministe par chemins protégés uniquement (D2), jamais heuristique.

## 7. Gate inputs/outputs/reason codes

Détail par gate : `JOAO_C8_GATE_CONTRACTS.md`. Forme commune `{ok, decision, reason_code, reason, candidate_tree, …}`, fail-closed.

## 8. Entrypoint réel par gate

| Gate | Entrypoint réel |
|---|---|
| G-DBL-AUDIT | `bubble/gates.py` appelé à `REVIEWING→promotion_ready` + re-check dans `RunRuntime.promote()` |
| G-HERMETIC | fixture autouse `tests/conftest.py` + auditeur d'ouvertures de fichier (détection+échec) |
| G-AUTH-IO | `scripts/audit_test_entrypoints.py` (auditeur statique de suite), pre-close/CI |
| G-NO-STALE-ENTRYPOINT | `scripts/audit_entrypoints.py` (audit AST), pre-close/CI |
| G-SHA-BOUND-PROOF | chemin d'écriture ET de consommation de preuve dans `RunRuntime` (les 2 bouts) |
| G-CANARY-FIRST | `RunRuntime.promote()` / `promotion._verify_acceptance_for_promotion` |
| G-FROZEN-FINISH-LINE | `RunRuntime.start()` écrit `frozen_mission.json` ; `bubble/gates.py` compare l'état git réel avant freeze/review |

## 9. Provider identity model

`(provider, model)` = constantes de classe de l'adaptateur, **toujours écrasées** dans la preuve acceptée côté contrôleur (`reviewer_contract.validate_reviewer_verdict`, durci A0.2 correctif 5). G-DBL-AUDIT compare ces identités contrôleur pour builder≠reviewer et distinction de providers.

## 10–11. Launch contracts

Builder GLM/ZCode : WORKER §4 (réel). Reviewer Codex : WORKER §5 (réel, `CodexCLIReviewer`). Reviewer GLM : WORKER §2.1 (nouveau, `GLMReviewer`, **seulement si builder≠GLM**). 3ᵉ provider critique : WORKER §5.

## 12. Modèle de preuve SHA/tree

WORKER §6, généralisé par G-SHA-BOUND-PROOF à tout artefact (canary, logs de gate), pas seulement les verdicts reviewer. `recompute_candidate_tree` pré/post chaque étape sensible.

## 13. Machine à états + séparation validation/approbation  *(finding GPT #5)*

États `RunRuntime` inchangés : `ready → building → needs_approval → accepted → promoted` (+ `blocked`/`paused`). C-8 ajoute des **checks** aux transitions, **aucun nouvel état**. Ordre unifié (finding GPT v3 #3) :

```
reviews → canary → promotion-readiness.json → approbation Boss → promote()
```
- `promotion-readiness.json` = **ARTEFACT mécanique** lié au tree (gates verts + verdicts de familles distinctes + canary vert), écrit dans le `run_dir` — **PAS un nouvel état caché de `RunStatus`**.
- `approval-record.json` = **autorisation humaine** (`approved_by=human`, texte d'autorité hashé — D5), jamais fabriquée par le contrôleur.
- `promote()` exige `promotion-readiness.json` valide (lié au tree) ET `approval-record.json` humain réel.

## 14. Correction-loop maximum  *(finding GPT #4)*

**`max_corrections = 1`** — valeur réelle du runtime (`runtime.py:593`, `plan.json`), pas 2. Une tentative initiale + une correction ciblée, puis arrêt → retour Boss. `retry()` incrémente `corrections_used` ; à épuisement → `NEEDS_APPROVAL`. (Le `max_repair_loops: 2` de `context_policy.yaml` est un knob de quota distinct, non le budget de correction.)

## 15. Boss approval points  *(D6)*

Exactement : (a) GO initial nommant la SPEC/ROADMAP approuvée (hashée, D5) ; (b) escalade de tier en cours de run (déterministe par chemin, D2) ; (c) promotion. Autonomie autorisée **entre les gates pour runs synthétiques/normal** ; promotion et effets externes restent Boss-controlled (D6). Aucun arrêt Boss aux étapes mécaniques intermédiaires.

## 16. Failure & recovery

Tout gate `ok=false` → `blocked`, jamais de downgrade silencieux. Récupération = **nouveau run** (nouveau `candidate_tree`), jamais un resume/patch d'un candidat gelé bloqué. Désaccord reviewer critique → BLOCK + notification Boss, pas de tie-break auto (D4).

## 17. Immutable candidate / tag / rollback

Réutilise `promotion.py` (promote/rollback atomique, prouvé par `a0_toy_mission_e2e.py`) + la convention de tag annoté de cette session (pointant un commit exact, créé après GO reviewer). Candidats superseded archivés/révoqués, jamais auto-supprimés (D7).

## 18. Test strategy (rouge→vert adversarial)

Un test rouge→vert par reason code de `JOAO_C8_GATE_CONTRACTS.md`, chacun via le vrai entrypoint du gate (G-AUTH-IO appliqué au développement de C-8 lui-même), + un test positif du cas conforme. Tous hermétiques (G-HERMETIC, borné aux **racines de données sensibles/mutables** — pas aux sources immuables du repo/Python/deps que pytest doit lire ; GATE_CONTRACTS) — la suite par défaut atteint **`EXIT=0`** après le lot hermétique (D-044 déterminisé par fixture, non masqué). Détail par lot : `JOAO_C8_GATES_ROADMAP.md`.

## 19. Acceptance criteria (milestone C-8)

`SEVEN_GATES_WIRED=7` aux entrypoints §8 · `EACH_GATE_REDGREEN=true` · `G_FROZEN_FINISH_LINE_MECHANICAL=true` (frozen_mission.json + comparaison git) · `DEFAULT_SUITE_EXIT_0=true` (D-044 hermétique) · `MAX_CORRECTIONS=1` · `GLM_NEVER_REVIEWS_GLM_BUILT_CANDIDATE=true` · `CRITICAL_HAS_2_DISTINCT_NON_BUILDER_PROVIDERS=true` · `MECHANICAL_ELIGIBILITY_SEPARATE_FROM_BOSS_APPROVAL=true` · `NO_EIGHTH_GATE=true` · `NO_OS_SECURITY_RECREATED=true` · `ZERO_PAID_API_COST=true`.

## 20. Migration depuis A0.2

Additive : A0.2 est la **fondation** (gel candidat, contrat reviewer strict, scope gelé, dispatch unique, promotion vérifiée), pas remplacée. C8-A ajoute gates + hermeticity + frozen_mission autour du moteur ; C8-B ajoute `GLMReviewer` + orchestration ; C8-C ajoute SHA/canary/promotion + E2E + readiness. Démarre après clôture Codex d'A0.2 (2026-07-23) et merge — **sauf C8-A**, qui peut démarrer sans attendre Codex (voir roadmap). Aucune régression des tests A0.2 tolérée.

## 21. Compatibilité SOURCE-FRESH

C-8 s'arrête à un readiness gate (lot C8-C) prouvant que le cycle piloté par gates porte une mission produit synthétique E2E. Premier run réel SOURCE-FRESH = **`critical` + `canary_required`** exceptionnellement, → `normal` après 3 runs propres (décision produit, `JOAO_C8_OPEN_DECISIONS.md`). C-8 ne construit PAS SOURCE-FRESH.

## 22. Cost policy

**Zéro coût API payant incrémental.** Gates = code local (stdlib + git). Builder = abonnement GLM/ZCode (`joao-glm`, forfait Z.AI). Reviewer = abonnement Codex, + GPT-formel (intérim) pour le 3ᵉ provider critique. `max_cloud_workers: 1`, `no_parallel_cloud_subagents`. Aucun nouveau provider métré.

## 23. No OS-security boundary recreation (D-046)

Critère d'acceptation dur : tout milestone proposant un sandbox/ACL/isolation maison est hors scope, rejeté. L'isolation reste `ExecutionBackend`.
