# JOÃO C-8 GATES + WORKER INTEGRATION — ROADMAP (v2 — 3 lots, 7 gates conservés)

**Historique (Phase 0 — origine du document) :** planifie, n'implémente rien. Branche `feat/joao-c8-gates-spec`, base `10156a2` — point de départ historique v1.
**Autorité C-8 courante (planning) :** `acf9ef4e42e2f9b1333a1f5c0314895339f4fef0` (tip v4). C8-A implémenté/contre-audité sur `06911e457cee50e30934470beb69c78f9f696434` (`feat/joao-c8-a-v4`), correction en cours, non affecté par cet amendement.
**Cet amendement (v2.1, docs-only) :** branche `feat/joao-product-superiority-spec`, base `acf9ef4e42e2f9b1333a1f5c0314895339f4fef0`.
A0.2 gelé `cc796b5`, non modifié.
**v2 :** compresse les 7 milestones en **3 lots d'exécution** (recommandation GPT + Boss), sans supprimer un seul des 7 gates. Chaque lot garde : scope, fichiers, tests, tests adversariaux, exigence reviewer, critères d'acceptation, rollback, dépendances, STOP.
**v2.1 (docs-only, ce commit, branche/base ci-dessus) :** ajoute l'instrumentation Product Superiority & Efficiency à C8-B, le benchmark synthétique manuel vs JOÃO à C8-C, et la validation post-C8 sur missions réelles (`JOAO_C8_GATES_SPEC.md` §24, `JOAO_PRODUCT_VALIDATION_BACKLOG.md`). N'ajoute aucun 8ᵉ gate, ne modifie aucun critère technique C8-A.

**Modèle reviewer des lots C-8 eux-mêmes (important) :** les lots C-8 sont **construits par une session builder pilotée par un humain, PAS par `GLMBuilder`**. Identité formelle de ce builder (remplace le terme flou « builder = session ») :
```
builder_provider   = anthropic
builder_family     = anthropic
builder_model      = <modèle Claude réel de la session qui construit le lot>
builder_session_id = <id ou hash de session>
human_piloted      = true
```
Donc `GLMReviewer`/GLM (`provider_family=zai`) est un reviewer **indépendant valide** ici (famille ≠ `anthropic`), et **GLM (`zai`) + GPT (`openai`) = 2 `provider_family` mutuellement distinctes, toutes deux ≠ `anthropic`** satisfont le tier critical **avant** le 23 (§6 SPEC — ce cas est différent de « Codex+GPT pour un build GLM », voir la correction #1 ci-dessous, où le builder est `zai` et Codex/GPT sont tous deux `openai`). Codex fait la revue finale le 23. Le paradoxe G-DBL-AUDIT (GLM ne peut pas revoir un candidat GLM-buildé) ne s'applique **pas** au développement de C-8 (builder = `anthropic`, jamais `zai` ici), seulement aux futures missions GLM-buildées.

**Séquence Boss :** NOW = correctif docs + contre-audit GPT + GO BUILD. AVANT LE 23 = C8-A → C8-B → C8-C (GLM + GPT), candidat C-8 gelé sur un SHA exact. LE 23 = Codex audite A0.2 `cc796b5` ET le SHA final C-8. APRÈS PASS = merge A0.2, merge C-8, puis Competitor/OSS → SOURCE-FRESH.

---

## LOT C8-A — contrats + herméticité + scope gelé  *(peut démarrer SANS attendre Codex)*

**Scope :** (1) les 7 gates comme fonctions pures fail-closed dans `bubble/gates.py`, forme `{ok, decision, reason_code, reason, candidate_tree}` de `JOAO_C8_GATE_CONTRACTS.md` — logique correcte en isolation, pas encore câblée dans `approve()`/`promote()`. (2) **G-HERMETIC réel** : fixture autouse étendant `_isolated_joao_memory_dir` à TOUT root externe + auditeur d'ouvertures de fichier qui FAIL une évasion ; **déterminise D-044** (ledger externe + specs voisines remplacés par fixture gelée sous `tmp_path`, aucun skip/xfail) → suite par défaut **`EXIT=0`**. (3) **G-FROZEN-FINISH-LINE mécanique** : `frozen_mission.json` (spec_sha, roadmap_sha, acceptance_criterion_ids, allowed_write_paths, forbidden_paths, risk_tier, canary_required) écrit par `start()` + comparaison état-git→critères gelés. (4) stubs `scripts/audit_entrypoints.py` et `scripts/audit_test_entrypoints.py` (logique complète en C8-B).

**Fichiers/modules attendus :** neuf `bubble/gates.py` ; `RunRuntime.start()` (ajout écriture `frozen_mission.json` — additif) ; `tests/conftest.py` (étendre la fixture d'isolation) ; neuf `tests/test_c8_gates.py`, `tests/test_g_hermetic_self_check.py`, `tests/test_frozen_mission.py` ; neuf `scripts/audit_entrypoints.py` + `scripts/audit_test_entrypoints.py` (stubs). **Aucun** `.py` d'A0.2 modifié en assertion.

**Tests :** unitaires par gate (dicts d'entrée construits à la main) ; auto-check hermeticité ; frozen_mission map/BLOCK.

**Tests adversariaux :** un rouge→vert par reason code (`G_DBL_AUDIT_SAME_PROVIDER`, `G_HERMETIC_EXTERNAL_LEDGER_DEPENDENCY`, `G_FROZEN_FINISH_LINE_SCOPE_CREEP`, `G_SHA_BOUND_MISMATCH`, …) ; un test plantant une lecture de vrai chemin → doit être flaggé avant retrait ; un candidat écrivant hors `allowed_write_paths` → `SCOPE_CREEP`.

**Exigence reviewer :** **critical** (change l'infra de test + ajoute frozen_mission). Builder = `provider_family=anthropic`/`human_piloted=true` (≠ `zai`) → **GLM (`GLMReviewer` ou joao-glm read-only) + GPT**, 2 providers distincts. Codex final le 23.

**Définition de la fixture D-044 (précisée — finding GPT v2 #2) :** la fixture gelée sous `tmp_path` fournit (a) un `DEFECTS_LEDGER` synthétique au **contenu figé** (un jeu connu de defects, incluant un D-044 de démonstration) et (b) une copie figée des specs `cv_bot`. Le test réécrit `build_traceability` pour lire ces racines injectées, **s'exécute toujours** (aucun skip/xfail) et **affirme le mapping attendu déterministe** sur la fixture (0 UNMAPPED pour le jeu figé mappé). Le **défaut produit D-044** (le vrai trou de traçabilité sur le ledger réel) reste **ouvert au ledger/backlog produit** — seule la **dépendance de la suite** au contenu vivant de `~/Claude-HQ`/repo voisin disparaît.

**Critères d'acceptation :** 7 fonctions de gate conformes au contrat ; chaque reason code a un rouge→vert vert ; **`pytest` par défaut = `EXIT=0`** (D-044 déterminisé par fixture, non masqué) ; `frozen_mission.json` (avec `criterion_bindings`) écrit et comparé ; **chaque `required_test` d'un critère produit un PASS lié au `candidate_tree`, sinon BLOCK** (finding GPT v3 — un `criterion_bindings` sans preuve de test verte liée au tree est refusé) ; **intégrité A0.2 (formulation corrigée, finding GPT v2 #4)** :
- branche/tag/candidat A0.2 (`feat/joao-a0-integrite-clean`, `cc796b5`, son tag, evidence, tests) **inchangés** ;
- diff C8-A **borné à la liste de fichiers autorisée** (§ Fichiers ci-dessus) ;
- les fichiers existants (`runtime.py`, `tests/conftest.py`) modifiés **uniquement en hunks additifs bornés** — pas de réécriture ;
- **aucune assertion A0.2 existante modifiée** (les tests A0.2 restent verts, inchangés).

**Rollback :** suppression pure de fichiers neufs + revert de l'ajout `conftest.py`/`start()` (additif) ; aucun état runtime/promotion à défaire.

**Dépendances :** aucune (premier lot). Ne dépend PAS de Codex → démarre après GO BUILD.

**STOP :** si rendre G-HERMETIC strict exigeait de skip/xfail/supprimer le test D-044 au lieu de le déterminiser par fixture → STOP (ce serait « modifier D-044 », interdit) ; si un gate ne peut être une fonction pure de ses inputs → STOP, pas de redéfinition silencieuse du contrat.

---

## LOT C8-B — reviewers + orchestration automatique

**Scope :** (1) `GLMReviewer(ReviewerAdapter)` (WORKER §2.1) + **`GPTFormalEvidenceReviewer`** (`provider="openai-gpt"`, `provider_family="openai"`, WORKER §5), tous deux à l'allowlist du garde AST single-dispatch. **Rôle exact de `GPTFormalEvidenceReviewer` (corrige une contradiction interne v2 — ne PAS l'implémenter comme « le 3ᵉ provider distinct des runs critical ») :**
```
GPTFormalEvidenceReviewer appartient à provider_family=openai (même famille que Codex).
Utilisable sur des runs normal, ou comme preuve additionnelle non comptée pour la
distinction de familles — jamais comme 2ᵉ famille distincte à côté de Codex.

Pour un build GLM (zai) critical, les 2 reviewers doivent être de familles mutuellement
distinctes ET ≠ zai : Codex (openai) + GPT (openai) NE suffisent PAS (même famille,
G_DBL_AUDIT_SAME_FAMILY, SPEC §6). Il faut Codex (openai) + un reviewer provider_family
distinct (ex. ClaudeCLIReviewer, anthropic) — sinon BLOCK fail-closed
(G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE), jamais GPT substitué en 2ᵉ famille.
``` (2) logique réelle de `scripts/audit_entrypoints.py` (G-NO-STALE) et `audit_test_entrypoints.py` (G-AUTH-IO). (2b) **preuve dynamique G-AUTH-IO** : spy/monkeypatch/trace d'appel asserttant que l'entrypoint réel est **invoqué** (pas seulement référencé) — l'audit statique seul est insuffisant (une référence morte le satisfait). (3) **contrôleur d'orchestration** (`bubble/orchestrator.py`) qui séquence `start→run_once→freeze→review(s)→promotion_ready` et **produit l'éligibilité mécanique sans fabriquer l'approbation humaine**. (4) G-DBL-AUDIT câblé : sélection reviewer par tier, builder≠reviewer, providers distincts, **preuve multi-review** liée au tree.

**Corrections pré-C8-B inscrites (finding GPT v3) — implémentées DANS ce lot, condition du GO C8-B :**
- **#1 `provider_family` :** chaque builder/reviewer porte `provider_family` (`zai`/`openai`/`anthropic`) ; tier critical exige **2 familles distinctes** ≠ famille builder. Codex+GPT = openai = insuffisant → un build GLM critical est **bloqué** (`G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE`) tant que `ClaudeCLIReviewer` (`anthropic`) n'existe pas. (Prévoir `ClaudeCLIReviewer` dans ce lot pour débloquer, OU accepter le blocage fail-closed du critical GLM.)
- **#2 import de preuve sécurisé :** `*-review-import.json` provient d'un **inbox contrôleur hors chemins builder**, avec **nonce + candidate_tree exact + ingestion unique** ; rejet sinon (empêche un builder de préfabriquer une fausse preuve). Tests adversariaux : import sans nonce / au mauvais tree / rejoué / dans une zone écriture-builder → refusé.
- **#3 ordre unifié + artefact :** `reviews → canary → promotion-readiness.json → approbation Boss → promote`. `promotion-readiness.json` = artefact mécanique lié au tree, **pas** un nouvel état `RunStatus`.

**Fichiers/modules attendus :** `bubble/runtime.py` (+`GLMReviewer`) ; `tests/test_a0_2_corrections.py` allowlist `_GUARDED_METHODS` (+`"GLMReviewer": {"review_stage"}` — **ajout** à un dict, jamais une assertion A0.2 modifiée ; diff revu contre G-FROZEN-FINISH-LINE) ; neuf `bubble/orchestrator.py`, `tests/test_c8_glm_reviewer.py`, `tests/test_c8_orchestration.py`, `tests/test_c8_dbl_audit_wiring.py` ; `scripts/audit_entrypoints.py`/`audit_test_entrypoints.py` (logique réelle).

**Tests :** `GLMReviewer` lie `candidate_tree` avant/après comme `CodexCLIReviewer` (via faux `joao-glm`) + un test `live_canary` manuel réel ; orchestration produit `promotion_ready` sans écrire d'`approval-record` humain ; G-DBL-AUDIT : normal 1-reviewer, critical 2-providers-distincts.

**Tests adversariaux :** candidat trafiqué en cours de review (fail-closed) ; dispatch GLM manquant → `block` ; run critical avec 2 slots remplis par GLM → `G_DBL_AUDIT_SAME_PROVIDER` ; **run GLM-buildé dont on tente GLM comme reviewer → `G_DBL_AUDIT_BUILDER_SELF_REVIEW`** (le paradoxe, testé) ; contrôleur tentant de fabriquer `approved_by=human` → refusé.

**Exigence reviewer :** **critical** — ajoute des capacités de dispatch/orchestration. Builder = `provider_family=anthropic`/`human_piloted=true` (≠ `zai`) → **GLM + GPT** (2 distincts). Codex final le 23.

**Critères d'acceptation :** `GLMReviewer` passe les formes tamper/fail-closed de `CodexCLIReviewer` ; `audit_entrypoints.py` = zéro dispatch non listé ; un dispatch GLM-reviewer réel (non mocké) réussit sur un candidat jouet, tracé comme `GLM_RUN_EVIDENCE.json` ; l'orchestrateur n'écrit jamais d'approbation humaine ; tests A0.2 existants inchangés et verts.

**Rollback :** retrait de `GLMReviewer` + son entrée allowlist + `orchestrator.py` ; `audit_*` revient aux stubs C8-A ; aucun défaut `RunRuntime` changé (aucun reviewer auto-sélectionné hors orchestrateur).

**Dépendances :** C8-A (formes de gate, fixture hermétique, frozen_mission).

**STOP :** si le contrat CLI réel de `joao-glm` a dérivé de WORKER §2.1 → STOP, re-vérifier contre le binaire live avant d'écrire `GLMReviewer` ; si câbler G-DBL-AUDIT exige de MODIFIER (pas seulement d'AJOUTER) un test A0.2 existant → STOP, adjudication Boss.

**Instrumentation Product Superiority & Efficiency (PV — `JOAO_C8_GATES_SPEC.md` §24, condition de GO C8-B) :** ce n'est pas un gate technique de plus, mais l'instrumentation est requise pour que C8-C puisse mesurer §24.

- **Scope :** événements timestampés (GO, build démarré, premier candidat, tests ciblés/complets verts, candidat gelé, revues démarrées/terminées, canary, promotion-readiness, approbation Boss, acceptation finale) ; schéma d'événement action-humaine (prompts, commandes terminal, uploads/downloads, changements de contexte, vérifications manuelles, décisions Boss) ; capture usage/tokens par provider quand exposé, `unknown` explicite sinon (jamais `0`) ; compteurs retries/corrections ; `benchmark-run-manifest.json` (identifie base_sha/spec_sha/roadmap_sha/risk_tier/workflow_mode/ordre randomisé) ; `benchmark-events.jsonl` append-only ; champ `workflow_mode` (`manual_same_stack`|`manual_current_workflow`|`joao`) porté par chaque run, y compris manuel.

  **Champs obligatoires par événement et liaison candidat (avant/après freeze — corrige l'exigence "candidate_tree sur chaque événement", impossible avant que le candidat existe) :**
  ```
  Toujours obligatoire   : benchmark_id, run_id, mission_id, workflow_mode,
                            base_sha, spec_sha, roadmap_sha, sequence_number

  Avant freeze            : candidate_tree = null, candidate_commit = null

  Au freeze               : événement CANDIDATE_BOUND avec candidate_tree exact
                            et candidate_commit exact

  Après freeze            : candidate_tree obligatoire sur chaque événement/
                            preuve sensible (jamais null après CANDIDATE_BOUND)
  ```
  **Chaînage tamper-evident** (rend le JSONL append-only vérifiable, pas seulement nommé ainsi) :
  ```
  previous_event_hash, event_hash   — chaque événement inclut le hash du précédent ;
  une suppression ou modification d'un événement casse la chaîne, détectable.
  ```
- **Fichiers/modules attendus :** module d'instrumentation neuf (nom TBD, ex. `bubble/benchmark_events.py`) branché en observateur sur les mêmes entrypoints réels que les gates §8 — jamais un chemin dupliqué (G-NO-STALE-ENTRYPOINT s'applique) ; tests `tests/test_c8_benchmark_instrumentation.py`.
- **Tests :** déterminisme du **calcul**, pas du comportement du modèle (deux exécutions d'un agent peuvent légitimement différer en nombre d'appels/retries/actions tout en produisant un résultat correct) :
  ```
  - mêmes entrées événementielles → même sérialisation (déterminisme d'encodage) ;
  - même event log → mêmes métriques dérivées (déterminisme de calcul) ;
  - sequence_number strictement croissant, jamais réutilisé ;
  - mêmes règles de calcul appliquées identiquement à un run manuel et à un run JOÃO ;
  - AUCUNE obligation que deux exécutions LLM produisent la même séquence d'événements.
  ```
  Append-only vérifié via `previous_event_hash`/`event_hash` (jamais de réécriture d'un événement déjà écrit, chaîne rompue = détectée) ; `candidate_tree` null avant `CANDIDATE_BOUND`, obligatoire après (ci-dessus) ; capture usage manquante → `unknown`, jamais `0`/estimé ; instrumentation active sur un run manuel comme sur un run JOÃO (même schéma des deux côtés, sinon la comparaison §24 est invalide).
- **Tests adversariaux :** provider ne renvoyant aucune donnée d'usage → champ `unknown`, run non bloqué ; tentative d'écraser un événement déjà append (`event_hash`/`previous_event_hash` incohérents) → détectée et refusée ; instrumentation désactivée/absente sur un run critical → n'empêche pas le run (l'instrumentation n'est jamais un gate fail-closed), mais est marquée `EVIDENCE_COMPLETENESS<100%` dans le manifest ; événement sensible présenté avec `candidate_tree=null` après `CANDIDATE_BOUND` → rejeté.
- **Critères d'acceptation additionnels à C8-B :** instrumentation déterministe (au sens calcul, ci-dessus) ; append-only vérifiable par chaîne de hash ; n'altère pas matériellement les sorties du builder ; `candidate_tree` correctement null/obligatoire selon l'étape (ci-dessus) ; instrumente aussi bien un run manuel qu'un run JOÃO ; ne rapporte jamais une valeur de token inventée ; ne loggue aucun secret/credential (`SECRETS_LOGGED=0`) ; overhead borné et mesuré, pas seulement affirmé — seuils exacts (`INSTRUMENTATION_WALL_CLOCK_OVERHEAD<=5%`, `INSTRUMENTATION_FAILURE_MUST_NOT_BREAK_PRODUCT_RUN=true`, `RAW_EVENT_LOG_SIZE_REPORTED=true`) : `JOAO_C8_GATES_SPEC.md` §24.5.

**Rollback (instrumentation) :** retrait pur du module d'observation + de ses hooks (additifs) ; aucun état runtime/promotion à défaire ; C8-B reste fonctionnel sans (juste non mesurable par §24).

---

## LOT C8-C — SHA/canary/promotion + E2E  *(+ readiness SOURCE-FRESH)*

**Scope :** (1) G-SHA-BOUND-PROOF général (recompute-and-compare à l'écriture ET la consommation de preuve) + G-CANARY-FIRST dans `promotion._verify_acceptance_for_promotion`. (2) **E2E synthétique complet** traversant toute la chaîne assemblée (Boss GO → orchestrateur → build → gates → freeze → 2 reviewers critical → canary synthétique exact-SHA sans effet externe (D3) → promotion exigeant `promotion_ready` ET approbation humaine). (3) **rapport de readiness SOURCE-FRESH** (document seulement, pas de code SOURCE-FRESH).

**Fichiers/modules attendus :** `bubble/promotion.py` (préconditions additives dans la chaîne de vérif) ; `bubble/gates.py` (G-SHA-BOUND, G-CANARY complétés) ; neuf `tests/test_c8_canary_promotion.py`, `scripts/c8_e2e_synthetic_mission.py` (étend `a0_toy_mission_e2e.py`), `JOAO_C8_SOURCE_FRESH_READINESS_REPORT.md`.

**Tests :** promote refuse sans canary (`G_CANARY_FIRST_MISSING`), sur canary stale (`G_CANARY_FIRST_STALE_CANDIDATE`), réussit avec canary vert exact ; artefact au tree mismatché refusé (`G_SHA_BOUND_MISMATCH`) aux 2 bouts.

**Tests adversariaux :** canary du candidat A présenté à la promotion du candidat B → refusé (l'erreur cross-candidate rattrapée 3× à la main cette session) ; E2E répété avec un gate mal configuré par run (2ᵉ reviewer manquant en critical, canary manquant, candidat trafiqué) → halte au bon gate/reason code ; tentative de promotion avec `promotion_ready` mais sans approbation humaine → refusée.

**Exigence reviewer :** **critical** (chemin de promotion). Builder = `provider_family=anthropic`/`human_piloted=true` (≠ `zai`) → **GLM + GPT**. Codex final le 23 sur le SHA C-8 gelé.

**Critères d'acceptation :** happy-path E2E complet (build → 2 reviewers GO → canary pass → promotion) avec « SAME HASH TRACED END TO END: True » comme `a0_toy_mission_e2e.py`, étendu aux 2 verdicts + canary ; chaque run adversarial halte au bon gate ; workspace live + `memory/lessons.jsonl` byte-identiques avant/après ; le rapport readiness répond, preuve à l'appui : SOURCE-FRESH lançable via le contrôleur ? (premier run = `critical`+`canary_required`).

**Rollback :** suppression du script E2E + du rapport ; préconditions `promotion.py` = checks additifs révertables ; aucun changement de modèle de données candidat.

**Dépendances :** C8-A + C8-B (assemble tout).

**STOP :** si le happy-path ne peut atteindre « SAME HASH TRACED END TO END: True » que `a0_toy_mission_e2e.py` atteint déjà → STOP (régression inacceptable) ; si répondre à la readiness SOURCE-FRESH exige d'écrire du code SOURCE-FRESH → STOP immédiat (implémentation interdite sous ce nom).

**Benchmark synthétique manuel vs JOÃO (PV — `JOAO_C8_GATES_SPEC.md` §24, condition de clôture C8-C) :** le E2E technique ci-dessus prouve que la chaîne tourne ; il ne prouve pas que la chaîne vaut mieux que le manuel. C8-C n'est pas considéré produit-complet sur le seul E2E technique vert.

- **Scope :** **BASELINE A obligatoire** (§24.2, orchestration pure même stack) ; **BASELINE B obligatoire si praticable** sur la mission synthétique (§24.2, workflow manuel actuel) — si non praticable sur ce synthétique, **différée explicitement** à la 1ʳᵉ mission réelle (jamais silencieusement omise, jamais traitée comme optionnelle) ; évaluation qualité finale aveugle (le reviewer final ne sait pas quel candidat vient de JOÃO) ; mesure temps total, temps humain actif, actions manuelles, tokens des deux bras.
- **Fichiers/modules attendus :** `JOAO_BENCHMARK_SCORECARD.json`, `JOAO_BENCHMARK_REPORT.md` (§24.5) produits par ce lot ; `scripts/c8_benchmark_synthetic_ab.py` (orchestre les deux bras sur le même E2E synthétique, réutilise `c8_e2e_synthetic_mission.py`).
- **Tests :** les deux bras utilisent le même SHA de base, la même SPEC, les mêmes critères d'acceptation (§24.3) ; le scorecard applique mécaniquement les seuils §24.6 (premier benchmark synthétique) et calcule un verdict sans intervention manuelle.
- **Critères d'acceptation additionnels à C8-C :** scorecard + rapport produits et liés au candidat/tree ; seuils §24.6 (synthétique) évalués, résultat explicite (PASS/FAIL par seuil, jamais un résumé qui masque un seuil raté) ; toute valeur token/usage non mesurable = `unknown`, jamais estimée ; limite d'un seul benchmark synthétique documentée explicitement comme provisoire (échantillon = 1, pas de confiance statistique revendiquée — §24.8) ; **verdict rendu explicitement `C8_TECHNICAL_PASS`/`PRODUCT_VALIDATION_PROVISIONAL` si BASELINE B n'a pas pu être mesurée ici, jamais `JOAO_PROVEN`** (§24.6).
- **STOP (benchmark) :** si produire le scorecard exige de fabriquer une métrique non mesurée → STOP, champ `unknown`, jamais une valeur inventée ; si le seul moyen d'atteindre les seuils §24.6 est de réduire la portée de la revue/preuve du bras JOÃO en dessous de celle du bras manuel → STOP, ce serait gagner le benchmark en dégradant la qualité, interdit par §24.1.

---

## Gel du candidat C-8 (avant le 23)

Après C8-A→C8-C verts (GLM + GPT sur chaque lot), **geler le candidat C-8 sur un SHA/tree exact** + tag annoté provisoire, comme A0.2. Le 23 : Codex audite A0.2 `cc796b5` ET le SHA final C-8. Après double PASS Codex : merge A0.2, merge C-8, puis Competitor/OSS → SOURCE-FRESH. **Aucun merge/push/promotion avant le PASS Codex.**

---

## Validation produit post-C8 — missions réelles Job Radar  *(PV — `JOAO_C8_GATES_SPEC.md` §24)*

Le C-8 technique (gates verts, E2E synthétique, un seul benchmark synthétique) n'est qu'une preuve de faisabilité. La preuve produit se fait sur des missions réelles.

**Cohorte des 3 premières missions :** Competitor/OSS Intelligence, SOURCE-FRESH, CONTENT (§21 — ce sont aussi les 3 premières missions produit prévues, pas des missions dédiées au benchmark). Pour chacune, capture de données manuel/JOÃO comparables quand possible. Ne pas construire deux fois toute la feature en entier si cela gaspille un temps excessif : approches autorisées — sous-missions bornées appariées, tranches d'implémentation équivalentes, un module sous contrôle manuel et un module apparié sous contrôle JOÃO, replay de l'orchestration sur une tâche d'implémentation gelée, mesure « shadow » quand un doublon de build serait un vrai gaspillage. La comparaison reste juste et déclarée (§24.3) ; aucune mission comparée silencieusement à une autre de difficulté matériellement différente.

Seuils après ces 3 missions : §24.6 (« Après les 3 premières missions Job Radar »).

**Après 10 missions réelles :** décision produit formelle rendue selon §24.6 : `JOAO_PROVEN` | `JOAO_HYBRID_RECOMMENDED` | `JOAO_SIMPLIFY` | `INSUFFICIENT_EVIDENCE`.

**Condition d'arrêt (STOP produit, distincte des STOP techniques par lot) :** deux gâchettes, §24.7. `EFFICIENCY_STOP` — évaluée après les 3 premières missions réelles, expression booléenne exacte en §24.7 ; déclenche une investigation/simplification de JOÃO ou un retour à un workflow hybride, jamais l'ajout d'une nouvelle couche d'architecture dans le seul but de faire passer le benchmark. `HARD_SAFETY_STOP` — évaluée en continu dès la première mission, bloque immédiatement sans attendre 3 missions (corruption candidat/preuve, promotion au mauvais SHA, P0/P1 échappé imputable à l'orchestration, métrique fabriquée).

**Dépendances :** C8-A + C8-B + C8-C (instrumentation + benchmark synthétique) + gel du candidat C-8 + double PASS Codex (23) + merge. Ne démarre pas avant.

**Backlog détaillé (PV-01…PV-09), priorités et dépendances :** `JOAO_PRODUCT_VALIDATION_BACKLOG.md`.
