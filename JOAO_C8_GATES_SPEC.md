# JOÃO C-8 GATES + WORKER INTEGRATION — SPEC (v2, post GPT counter-audit)

**Historique (Phase 0 — origine du document) :** SPEC + ROADMAP ONLY, branche `feat/joao-c8-gates-spec`, base `10156a2` — c'est le point de départ historique v1, plus l'état git courant.
**Autorité C-8 courante (planning) :** `acf9ef4e42e2f9b1333a1f5c0314895339f4fef0` (tip v4, GO C8-A inscrit). C8-A : candidat final `1f831619fccccf60ec8c996d58ae2cb34a838b01` (branche `feat/joao-c8-a-v4`), **prêt pour les audits indépendants finaux** — non affecté par cet amendement. Aucun PASS GPT n'est acquis à ce stade.
**Cet amendement (v2.1, docs-only) :** branche `feat/joao-product-superiority-spec`, base `acf9ef4e42e2f9b1333a1f5c0314895339f4fef0`. Ne touche ni C8-A, ni A0.2, ni aucun `.py`/test.
A0.2 gelé `cc796b55808b95210adf9d219ecd91a240b2676c` (tree `7ae68454…`, `PROVISIONALLY_CLOSED`, Codex `PENDING_2026-07-23`) — read-only, non modifié.
Compagnons : `JOAO_C8_GATE_CONTRACTS.md`, `JOAO_WORKER_INTEGRATION_SPEC.md`, `JOAO_C8_GATES_ROADMAP.md`, `JOAO_C8_OPEN_DECISIONS.md`, `JOAO_PRODUCT_VALIDATION_BACKLOG.md`.
**v2 :** corrige les 4 findings bloquants GPT + sépare validation-technique/approbation-Boss + inscrit D1–D7 + compresse la roadmap en 3 lots (7 gates conservés).
**v2.1 (docs-only, ce commit, branche/base ci-dessus) :** ajoute §24 *Product Superiority and Efficiency Validation* — C-8 techniquement vert ne vaut pas GO produit tant que §24 n'est pas franchi. N'est pas un 8ᵉ gate, ne modifie ni ne bloque C8-A.

## 1. Goals

1. Convertir les 7 leçons C-8 en gates **exécutables, fail-closed**, à reason codes stables, câblés dans le vrai pipeline (`bubble/gates.py` + entrypoints réels).
2. Doter JOÃO d'un `GLMReviewer(ReviewerAdapter)` — la moitié manquante prouvée manuellement cette session — **utilisable comme reviewer indépendant uniquement quand le builder n'est pas GLM** (voir §6 et le paradoxe résolu de G-DBL-AUDIT).
3. Un contrôleur d'orchestration qui déroule `Boss GO → SPEC/ROADMAP approuvée → build → gates → freeze → review → canary/promotion → approbation Boss`, sans séquencer `RunRuntime` à la main.
4. Faire de la discipline appliquée à la main cette session (indépendance reviewer, tests hermétiques, preuve via vrai entrypoint, liaison SHA, canary-avant-promotion, scope gelé) une propriété du **système**.
5. **Démontrer, par mesure et non par architecture, que construire via JOÃO produit un résultat meilleur** que le workflow manuel actuel (Claude Code build → revue manuelle Claude → copier-coller GPT → vérifications Git/pytest/SHA à la main) — voir §24, *Product Superiority and Efficiency Validation*. Un C-8 techniquement complet ne vaut GO produit que si §24 le confirme.

## 2. Non-goals

Pas de 8ᵉ gate. Pas de sécurité OS / nouvelle frontière d'isolation (D-046). Pas de réécriture de `RunRuntime`/`promotion.py`/`execution_backend.py` — le contrôleur appelle les primitives existantes. Pas de SOURCE-FRESH implémenté (c'est la 1ʳᵉ mission à faire passer par ce milestone, pas une partie de sa construction). Pas de changement de `memory/lessons.jsonl` (les leçons L-048..L-059/C-8 + D-055 différées restent hors candidat — D-055). Pas de réouverture d'A0.2. Pas de nouveau mécanisme d'auth « Boss GO » cryptographique (D5 : texte nommé, hashé et lié au run). §24 (*Product Superiority and Efficiency Validation*) n'est **pas un 8ᵉ gate** technique et ne bloque **pas** C8-A : c'est une validation produit, instrumentée en C8-B et mesurée en C8-C/post-C8 (voir §24 et `JOAO_C8_GATES_ROADMAP.md`).

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

Builder GLM/ZCode : WORKER §4 (réel). Reviewer Codex : WORKER §5 (réel, `CodexCLIReviewer`). Reviewer GLM : WORKER §2.1 (nouveau, `GLMReviewer`, **seulement si builder≠GLM**). 2ᵉ `provider_family` distincte en critical : WORKER §5.

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

Ces critères prouvent que C-8 fonctionne **techniquement**. Ils ne prouvent pas que C-8 vaut la peine d'être utilisé : cette seconde preuve est §24 (*Product Superiority and Efficiency Validation*), instrumentée en C8-B, mesurée en C8-C puis sur les missions réelles Job Radar — un critère produit, pas un 8ᵉ gate technique.

## 20. Migration depuis A0.2

Additive : A0.2 est la **fondation** (gel candidat, contrat reviewer strict, scope gelé, dispatch unique, promotion vérifiée), pas remplacée. C8-A ajoute gates + hermeticity + frozen_mission autour du moteur ; C8-B ajoute `GLMReviewer` + orchestration ; C8-C ajoute SHA/canary/promotion + E2E + readiness. Démarre après clôture Codex d'A0.2 (2026-07-23) et merge — **sauf C8-A**, qui peut démarrer sans attendre Codex (voir roadmap). Aucune régression des tests A0.2 tolérée.

## 21. Compatibilité SOURCE-FRESH

C-8 s'arrête à un readiness gate (lot C8-C) prouvant que le cycle piloté par gates porte une mission produit synthétique E2E. Premier run réel SOURCE-FRESH = **`critical` + `canary_required`** exceptionnellement, → `normal` après 3 runs propres (décision produit, `JOAO_C8_OPEN_DECISIONS.md`). C-8 ne construit PAS SOURCE-FRESH. SOURCE-FRESH, Competitor/OSS et CONTENT sont aussi les **3 premières missions de la cohorte de validation produit** §24 — leur readiness technique (ce paragraphe) et leur mesure d'efficacité (§24) sont deux preuves distinctes, l'une ne substitue pas l'autre.

## 22. Cost policy

**Zéro coût API payant incrémental.** Gates = code local (stdlib + git). Builder = abonnement GLM/ZCode (`joao-glm`, forfait Z.AI). Reviewer = abonnement Codex (`openai`). **GPT-formel est lui aussi de famille `openai`** : il ne peut donc PAS servir de 2ᵉ famille distincte à côté de Codex pour un run `critical` buildé par GLM (`zai`) — cette 2ᵉ famille doit être `anthropic` (`ClaudeCLIReviewer`, cible), sinon le run est bloqué fail-closed. GPT-formel reste utilisable en tier `normal` ou comme avis supplémentaire. `max_cloud_workers: 1`, `no_parallel_cloud_subagents`. Aucun nouveau provider métré.

**« Zéro coût API payant incrémental » ≠ « usage efficace de tokens/quota ».** Le premier est vrai par construction (abonnements forfaitaires, jamais de facturation à l'appel) et ne dit rien du second : un run orchestré par JOÃO peut consommer davantage de tokens/quota qu'un build manuel équivalent (plus de reviewers, plus de preuve, plus de relectures) sans que cela coûte un centime de plus — et pourtant représenter un usage moins efficace du quota forfaitaire, donc un coût réel en capacité de travail disponible. `ZERO_PAID_API_COST=true` (§19) reste un critère technique valide ; il ne clôt PAS la question d'efficacité tokens/quota, qui est mesurée et seuillée séparément en §24 (`TOKEN_OVERHEAD_NORMAL`, `TOKEN_OVERHEAD_CRITICAL`, tokens/coût par candidat accepté).

## 23. No OS-security boundary recreation (D-046)

Critère d'acceptation dur : tout milestone proposant un sandbox/ACL/isolation maison est hors scope, rejeté. L'isolation reste `ExecutionBackend`.

## 24. Product Superiority and Efficiency Validation

**Ceci n'est pas un 8ᵉ gate technique.** C'est une validation **produit** : la preuve, par mesure, que construire via JOÃO bat le workflow manuel actuel — pas seulement qu'il ajoute des contrôles. Un C-8 techniquement complet (§19) qui ne franchit pas §24 n'est pas un GO produit ; JOÃO reste alors une couche de contrôle plus lourde, plus lente et/ou plus coûteuse que l'existant, ce qui est un échec produit même si tous les gates sont verts.

### 24.1 Question produit primaire

« Construire via JOÃO fait-il réellement mieux travailler que Claude Code + revue manuelle Claude + copier-coller GPT ? » La réponse vient de mesures, jamais d'une affirmation architecturale, d'une intuition, ou du seul self-report d'un agent. JOÃO n'est prouvé que s'il produit un **meilleur workflow total**, du Boss GO au candidat accepté :

```
Boss GO → implémentation → tests verts → revues indépendantes complètes
→ preuves complètes → candidat exact identifié → prêt pour approbation Boss
```

**Point de fin unique du benchmark (`BENCHMARK_END`) :**
```
BENCHMARK_END = promotion_readiness_emitted
              (candidat exact, tests verts, revues terminées,
               preuves complètes, prêt à être présenté au Boss)
```
Le temps d'attente avant que le Boss clique le GO final ne pénalise ni JOÃO ni le manuel — il est mesuré séparément :
```
Métrique principale (comparée entre bras)   : Boss GO → BENCHMARK_END (promotion-readiness)
Métrique secondaire (informative seulement) : BENCHMARK_END → décision Boss (time_to_boss_decision)
```
« Candidat accepté » ailleurs dans ce document désigne toujours ce même point (`promotion_readiness_emitted`), jamais l'acceptation Boss finale — un seul point de fin, jamais deux définitions concurrentes.

N'optimiser QUE le temps du premier brouillon, la vitesse brute du modèle, le nombre de gates, le nombre de tests ou le volume de preuve généré est un anti-pattern explicitement rejeté par ce document.

Comparaison minimale exigée à chaque niveau de preuve (§24.4) :

```
JOÃO doit produire un résultat au moins aussi bon
+ avec moins de travail humain
+ plus rapidement jusqu'au candidat accepté
+ sans explosion des tokens
+ avec moins de risques et de reprises
```

### 24.2 Deux baselines obligatoires (ne jamais les fusionner)

**BASELINE A — orchestration pure, même stack.** Isole ce que JOÃO lui-même apporte. Même builder, mêmes reviewers, même SHA de départ, même SPEC, même budget de correction, même environnement. Mode manuel : humain lance le builder, lance les tests, assemble les preuves, vérifie les SHA, lance les reviewers, séquence chaque étape à la main. Mode JOÃO : mêmes composants, séquencés par JOÃO, gates appliqués, preuves liées au candidat, arrêt uniquement aux points d'approbation Boss définis (§15).

**BASELINE B — ton vrai workflow actuel.** La vraie question produit. Manuel : Claude Code build → revue manuelle Claude → copié-collé vers GPT → vérifications Git/status/diff/SHA à la main → pytest à la main → assemblage manuel de preuve → prompts de correction manuels. JOÃO : mission scopée automatiquement → tests + preuve → freeze du candidat → orchestration des revues indépendantes → canary si requis → promotion-readiness → approbation Boss.

Les deux comparaisons doivent exister au cours de la validation. Ne jamais les fusionner en un seul chiffre. **Exigibilité dans le temps (concilie ce paragraphe et `JOAO_C8_GATES_ROADMAP.md` C8-C) :** BASELINE A est obligatoire dès le premier benchmark synthétique (C8-C). BASELINE B est obligatoire sur ce même benchmark **si praticable** ; si elle ne l'est pas sur un scénario synthétique, elle est **explicitement différée** à la première mission réelle (§24.6, « après les 3 premières missions »), jamais silencieusement omise. `JOAO_PROVEN` (§24.6, palier 10 missions) est **impossible** tant que BASELINE B n'a jamais été mesurée au moins une fois — voir §24.6 pour les verdicts intermédiaires que C8-C peut rendre en son absence.

### 24.3 Protocole expérimental juste

Chaque comparaison appariée utilise : même SHA de départ propre ; même SPEC/ROADMAP approuvée ; mêmes critères d'acceptation ; mêmes `allowed_paths`/`forbidden_paths` ; même `risk_tier` ; même budget de correction ; même environnement de test ; même limite de temps ; accès provider équivalent ; même rubrique de qualité finale ; worktree neuf ; session modèle neuve sans sortie copiée de l'autre bras ; aucune réutilisation des découvertes d'implémentation du premier run dans le second.

Anti-contamination d'ordre — deux méthodes autorisées :
1. missions appariées de difficulté équivalente, assignation manuel/JOÃO randomisée ; ou
2. missions identiques depuis des snapshots remis à zéro, sessions fraîches isolées, ordre d'exécution randomisé.

Évaluation finale **aveugle quand c'est praticable** : le reviewer final ne doit pas savoir si le candidat vient de JOÃO ou du workflow manuel ; noms de fichiers, en-têtes de rapport et métadonnées exposés au scoreur ne doivent pas révéler le workflow quand cela peut être évité ; même rubrique pour les deux candidats. Toute déviation inévitable au protocole est documentée explicitement — jamais de comparaison silencieuse entre missions de difficulté matériellement différente.

### 24.4 Métriques instrumentées

**A. Qualité et justesse.** Critères d'acceptation réussis/total ; score d'acceptation pondéré ; tests ciblés/complets passés/échoués ; régressions ; scope leaks ; preuve incorrecte ou manquante ; tentative de preuve à mauvais SHA/cross-candidate ; nombre et sévérité des findings reviewer ; P0/P1/P2 trouvés avant acceptation ; P0/P1/P2 découverts après acceptation (fenêtre d'observation ci-dessous) ; nombre de boucles de correction (règle de comptage ci-dessous) ; score qualité du reviewer final aveugle ; statut accepté/rejeté final ; rollback nécessaire ou non.

**Score qualité et marge de non-infériorité (définition exacte, remplace la « tolérance documentée ») :**
```
QUALITY_SCORE = score aveugle sur 100 (rubrique unique, même grille pour les deux bras)
NON_INFERIORITY_MARGIN = 2 points maximum

QUALITY_NOT_WORSE_THAN_MANUAL=true  ssi TOUTES ces conditions :
  - QUALITY_SCORE(JOÃO) >= QUALITY_SCORE(manuel) - NON_INFERIORITY_MARGIN
  - aucun P0/P1 supplémentaire (avant ou après acceptation)
  - couverture de test non inférieure
  - aucun défaut d'intégrité (preuve/SHA/candidat)
```

**Fenêtre d'observation des défauts échappés** (sans elle, un candidat qui vient d'être accepté paraît automatiquement sans défaut échappé) :
```
ESCAPED_DEFECT_OBSERVATION_WINDOW = 7 jours après acceptation, OU jusqu'à la mission
                                     suivante sur le même périmètre — la première
                                     échéance atteinte. Un défaut P0/P1 découvert
                                     dans cette fenêtre compte comme "échappé",
                                     pas seulement un défaut découvert avant BENCHMARK_END.
```

**Comptage des corrections et redémarrages** (empêche de contourner `max_corrections=1` en relançant plusieurs runs neufs plutôt qu'une correction) :
```
Tout redémarrage causé par l'échec du même mission_id (nouveau run_id, nouveau
candidate_tree, mais mission_id identique) compte dans total_correction_attempts —
qu'il s'agisse d'un retry() interne ou d'un nouveau run lancé manuellement après échec.
```

**B. Rapidité.** Timestamps monotoniques : GO reçu ; build démarré ; premier candidat produit ; tests ciblés verts ; tests complets verts ; candidat gelé ; premier reviewer démarré/terminé ; reviewer final terminé ; canary terminé ; **`promotion_readiness_emitted` = `BENCHMARK_END`** (§24.1) ; approbation Boss demandée ; acceptation finale. Calculs : `time_to_first_candidate`, `time_to_green_tests`, `time_to_reviews_complete`, `time_to_promotion_ready` (= métrique principale, Boss GO → `BENCHMARK_END`), `wall_clock_seconds` (total, informatif), `time_spent_recovering_from_errors`, `time_to_boss_decision` (secondaire, `BENCHMARK_END` → décision Boss, jamais utilisé pour comparer JOÃO au manuel). Jamais seulement « temps d'écriture du code ».

**C. Effort humain.** `human_active_seconds` (jamais le temps d'attente machine) ; `manual_action_count` (prompts collés, commandes terminal, uploads/downloads, fichiers déplacés à la main, changements de fenêtre/contexte, vérifications SHA/preuve/tests à la main, lancements reviewer manuels, questions de clarification, décisions Boss, instructions de correction) ; `context_switch_count` ; `prompts_per_accepted_candidate` ; `commands_per_accepted_candidate` ; `Boss_interruptions_before_final_approval`. Central : même si le temps machine total reste similaire, JOÃO n'est utile que s'il libère fortement le temps humain.

**Comment `human_active_seconds` est capturé (définition exacte, requise pour que la métrique soit autre chose qu'une estimation) :**
```
Événements de session : human_session_start, human_session_pause,
                         human_session_resume, human_session_end

Règles :
  - action purement automatique (JOÃO/le modèle agit seul)         → 0 seconde humaine
  - attente du modèle/build/tests                                   → exclue
  - lecture, décision, rédaction de prompt, commande tapée          → incluse
  - segment de temps auto-déclaré (pas d'événement automatique)     → reliability=low
  - segment capturé automatiquement (start/pause/resume/end réels)  → reliability=high
```
**Confidentialité par défaut** — un événement d'action humaine ne loggue jamais le contenu complet d'un prompt/commande :
```
action_type          (obligatoire)
duration              (obligatoire)
content_length         (obligatoire)
content_hash            (optionnel)
raw_content              INTERDIT par défaut (opt-in explicite seulement, jamais pour un run réel)
```

**D. Tokens, quota et coût.** Par provider et par étape, quand exposé : `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, appels d'outils, appels modèle, retries, longueur de contexte, unités de quota, coût monétaire déclaré. Calculs : `tokens_per_accepted_candidate`, `tokens_per_acceptance_criterion_passed`, `tokens_per_real_defect_detected`, `token_overhead_vs_manual`, `cost_per_accepted_candidate`, `retries_per_candidate`. Règles strictes : jamais de valeur de token inventée ; jamais d'estimation quand le provider n'expose pas l'usage exact — champ `unknown`, jamais `0` ; distinguer usage abonnement/quota de coût monétaire mesuré ; distinguer consommation builder/reviewer/orchestration ; effets de cache enregistrés séparément ; ne jamais confondre « coût API additionnel nul » (§22) avec « usage efficace » — voir §22.

**E. Fiabilité et opérabilité.** Complétude de preuve (%) ; échecs de liaison SHA ; détections de candidat périmé ; violations d'herméticité ; entrées obligatoires manquantes ; dispatches reviewer échoués ; tentatives de rejeu de preuve ; runs bloqués ; faux blocages ; rollbacks réussis ; étapes de récupération opérateur ; incidents nécessitant une réparation manuelle du repo.

### 24.5 Artefacts de mesure (spécifiés ici, implémentés en C8-B/C8-C — pas dans ce docs-only)

- `benchmark-run-manifest.json` : `benchmark_id`, `mission_id`, `workflow_mode` (`manual_same_stack`|`manual_current_workflow`|`joao`), `base_sha`, `spec_sha`, `roadmap_sha`, `risk_tier`, critères d'acceptation, budget de correction, identités provider/modèle, empreinte d'environnement, ordre randomisé, timestamps début/fin.
- `benchmark-events.jsonl` : flux append-only, timestamps pour actions humaines, appels outil/modèle, étapes de test, changements de candidat, production de preuve, revues, corrections, acceptation. **Schéma d'événement normatif — voir §24.5.1.**

#### 24.5.1 Schéma d'événement et chaîne d'intégrité (normatif)

**Champs obligatoires sur CHAQUE événement**, y compris ceux émis avant tout gel de candidat :

```
benchmark_id        mission_id        workflow_mode
run_id              base_sha          spec_sha         roadmap_sha
sequence_number     # entier strictement croissant, sans trou, par benchmark_id
event_type          # énuméré, stable
timestamp_monotonic # horloge monotone (jamais une horloge murale ajustable)
timestamp_utc       # horloge murale, informative uniquement, jamais ordonnante
```

**Liaison au candidat — deux régimes explicitement distincts :**

```
AVANT le gel du candidat :
    candidate_tree   = null      # AUTORISÉ, et la seule valeur correcte
    candidate_commit = null      # AUTORISÉ, et la seule valeur correcte
    -> un événement pré-gel n'invente JAMAIS une identité de candidat

AU gel du candidat :
    émettre l'événement  event_type = CANDIDATE_BOUND
    portant le candidate_commit et le candidate_tree exacts

APRÈS CANDIDATE_BOUND :
    tout événement sensible et toute preuve EXIGENT le candidate_commit ET le
    candidate_tree exacts ; absents ou différents => l'artefact est refusé
    (même règle que G-SHA-BOUND-PROOF, aucun assouplissement pour le benchmark)
```

**Intégrité de la chaîne d'événements (append-only vérifiable) :**

```
previous_event_hash # hash de l'événement précédent (null pour sequence_number 0)
event_hash          # hash de la sérialisation canonique du présent événement,
                    # previous_event_hash inclus dans le calcul
```
Vérification : rejouer le fichier recalcule chaque `event_hash` et confirme que chaque `previous_event_hash` correspond au précédent. Une rupture de chaîne, un `sequence_number` dupliqué/manquant, ou une réécriture rétroactive invalident le benchmark — jamais un avertissement silencieux. L'append-only est une propriété **vérifiée**, pas seulement déclarée.

**Déterminisme exigé (et sa limite honnête) :** la sérialisation canonique, la dérivation des métriques, les règles d'ordonnancement des événements et le calcul du score sont **déterministes** — mêmes événements en entrée ⇒ mêmes métriques et même score, à l'octet près. En revanche il n'est **jamais** exigé que deux exécutions LLM produisent des séquences d'événements comportementales identiques : c'est le pipeline de mesure qui est déterministe, pas le modèle mesuré.
- `JOAO_BENCHMARK_SCORECARD.json` : comparaison machine-lisible manuel vs JOÃO.
- `JOAO_BENCHMARK_REPORT.md` : conclusions lisibles, limites, recommandation.
- `JOAO_PRODUCT_VALIDATION_HISTORY.jsonl` : historique append-only à travers les missions réelles.

Chaque artefact de benchmark est lié à : le commit de base exact ; le commit/tree candidat exact ; l'autorité SPEC/ROADMAP exacte ; le mode de benchmark exact.

**Seuil de surcoût de l'instrumentation elle-même** (l'instrumentation ne doit jamais devenir la cause principale du ralentissement qu'elle mesure) :
```
INSTRUMENTATION_WALL_CLOCK_OVERHEAD <= 5%
INSTRUMENTATION_FAILURE_MUST_NOT_BREAK_PRODUCT_RUN=true
RAW_EVENT_LOG_SIZE_REPORTED=true
SECRETS_LOGGED=0
```
Implémentation et critères d'acceptation associés : `JOAO_C8_GATES_ROADMAP.md`, lot C8-B.

### 24.6 Seuils d'acceptation adoptés

**Premier benchmark synthétique (C8-C) :**
```
QUALITY_NOT_WORSE_THAN_MANUAL=true
P0_P1_ESCAPED=0
EVIDENCE_COMPLETENESS=100%
SHA_BINDING_FAILURES=0
HUMAN_ACTIVE_TIME_REDUCTION>=50%
MANUAL_ACTION_REDUCTION>=70%
TOKEN_OVERHEAD_NORMAL<=10%
TOKEN_OVERHEAD_CRITICAL<=25%
```
Un run `critical` peut légitimement consommer davantage de tokens (plus de contrôle) — cette dépense doit éviter des reprises ou améliorer clairement la qualité, jamais être un coût sans contrepartie. Les seuils tokens ne s'appliquent que si l'usage est réellement mesuré ; `unknown` ne vaut jamais `0` et empêche de revendiquer une victoire d'efficacité tokens sans invalider automatiquement le candidat technique.

**Verdicts que C8-C peut rendre (distincts de `JOAO_PROVEN`) :** si BASELINE A passe les seuils ci-dessus mais que BASELINE B n'a pas pu être mesurée sur le synthétique (§24.2), C8-C rend au maximum :
```
C8_TECHNICAL_PASS=true              (E2E + gates + BASELINE A conformes)
PRODUCT_VALIDATION_PROVISIONAL=true  (BASELINE B différée à la 1ʳᵉ mission réelle)
```
jamais `JOAO_PROVEN`, qui exige BASELINE B mesurée au moins une fois (§24.2).

**Après les 3 premières missions Job Radar** (Competitor/OSS, SOURCE-FRESH, CONTENT — §21) :
```
QUALITY_MEDIAN >= MANUAL_BASELINE
HUMAN_ACTIVE_TIME_REDUCTION >= 50%
TOTAL_WALL_CLOCK_REDUCTION >= 20%
MANUAL_ACTION_REDUCTION >= 70%
COST_PER_ACCEPTED_CANDIDATE <= MANUAL_BASELINE
P0_P1_ESCAPED = 0
EVIDENCE_CORRUPTION = 0
```

**Après 10 missions réelles**, JOÃO est `JOAO_PROVEN` seulement si :
```
>= 80% des missions meilleures ou égales en qualité
HUMAN_ACTIVE_TIME_MEDIAN_REDUCTION >= 60%
TOTAL_WALL_CLOCK_MEDIAN_REDUCTION >= 25%
TOKENS_PER_ACCEPTED_CANDIDATE <= MANUAL_BASELINE (où mesurable)
CORRUPTION_PREUVE_OU_CANDIDAT = 0
MAX_UNE_CORRECTION_CIBLEE_PAR_MISSION
```
Décision formelle rendue à ce jalon : `JOAO_PROVEN` | `JOAO_HYBRID_RECOMMENDED` | `JOAO_SIMPLIFY` | `INSUFFICIENT_EVIDENCE`.

### 24.7 Condition d'arrêt (STOP / simplification)

Deux conditions distinctes, jamais fusionnées, chacune avec sa propre gâchette :

**`EFFICIENCY_STOP`** — évaluée après 3 missions réelles, expression booléenne exacte (remplace tout « ET/OU » ambigu) :
```
EFFICIENCY_STOP =
  ( WALL_CLOCK_PENALTY > 25%  OR  TOKEN_OVERHEAD > 30% )
  AND HUMAN_ACTIVE_TIME_REDUCTION < 40%
  AND QUALITY_GAIN <= 0
```
Si `EFFICIENCY_STOP=true` : pas de nouvelle couche d'architecture. Réponse obligatoire : investiguer l'origine du surcoût ; retirer/fusionner les étapes à faible valeur ; simplifier prompts/preuve ; réduire les revues redondantes ; envisager un workflow hybride ; ne jamais ajouter une couche d'architecture supplémentaire dans le seul but de faire passer le benchmark. Empêche de continuer à construire JOÃO simplement parce que du temps y a déjà été investi.

**`HARD_SAFETY_STOP`** — évaluée en continu, bloque **immédiatement**, sans attendre l'échéance des 3 missions :
```
HARD_SAFETY_STOP =
  candidate_corruption
  OR evidence_corruption
  OR wrong_sha_promotion
  OR escaped_P0_P1_caused_by_orchestration
  OR fabricated_metric
```
`HARD_SAFETY_STOP=true` prime toujours sur l'efficacité et bloque `JOAO_PROVEN` même si le workflow est rapide — il n'attend pas 3 missions comme `EFFICIENCY_STOP`, il arrête dès qu'il est détecté.

### 24.8 Anti-gaming

Interdit explicitement : comparer des difficultés de tâche différentes sans le déclarer ; exclure les runs JOÃO échoués des résultats ; compter l'attente machine comme travail humain actif ; compter un usage en cache comme usage nul ; traiter une valeur de token `unknown` comme `0` ; ne mesurer que l'étape la plus rapide ; arrêter le chronomètre avant que revues/preuves soient complètes ; donner à JOÃO plus de boucles de correction qu'au mode manuel ; donner à un workflow un accès préalable aux résultats de l'autre ; laisser le builder noter sa propre sortie ; changer les critères d'acceptation après avoir vu les résultats ; rapporter des pourcentages d'amélioration sans les valeurs brutes ; revendiquer une confiance statistique à partir d'une seule mission. Chaque run tenté, y compris échecs et candidats abandonnés, est enregistré.

### 24.9 Où cela entre dans le plan C-8 (détail complet : `JOAO_C8_GATES_ROADMAP.md`)

```
C8-A     → inchangé, aucune instrumentation, aucun code de validation produit.
C8-B     → ajoute l'instrumentation (temps, tokens, appels, retries, actions humaines).
C8-C     → exécute le benchmark synthétique manuel vs JOÃO, produit scorecard + rapport.
Missions Job Radar (Competitor/OSS, SOURCE-FRESH, CONTENT)
         → 3 missions comparatives appariées, premier cohort réel.
Après 10 missions
         → décision formelle : JOAO_PROVEN / JOAO_HYBRID_RECOMMENDED / JOAO_SIMPLIFY / INSUFFICIENT_EVIDENCE.
```

Backlog détaillé (PV-01…PV-09), dépendances et statut : `JOAO_PRODUCT_VALIDATION_BACKLOG.md`. Politique adoptée (non rouverte, ne bloque pas C8-A) : `JOAO_C8_OPEN_DECISIONS.md`.
