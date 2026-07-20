# JOÃO C-8 GATES + WORKER INTEGRATION — ROADMAP (v2 — 3 lots, 7 gates conservés)

Phase 0 — planifie, n'implémente rien. Branche `feat/joao-c8-gates-spec`, base `10156a2`. A0.2 gelé `cc796b5`, non modifié.
**v2 :** compresse les 7 milestones en **3 lots d'exécution** (recommandation GPT + Boss), sans supprimer un seul des 7 gates. Chaque lot garde : scope, fichiers, tests, tests adversariaux, exigence reviewer, critères d'acceptation, rollback, dépendances, STOP.

**Modèle reviewer des lots C-8 eux-mêmes (important) :** les lots C-8 sont **construits par une session builder (Claude/humain-piloté), PAS par `GLMBuilder`**. Donc `GLMReviewer`/GLM est un reviewer **indépendant valide** ici (builder ≠ GLM), et **GLM + GPT = 2 providers distincts** satisfont le tier critique **avant** le 23. Codex fait la revue finale le 23. Le paradoxe G-DBL-AUDIT (GLM ne peut pas revoir un candidat GLM-buildé) ne s'applique **pas** au développement de C-8, seulement aux futures missions GLM-buildées.

**Séquence Boss :** NOW = correctif docs + contre-audit GPT + GO BUILD. AVANT LE 23 = C8-A → C8-B → C8-C (GLM + GPT), candidat C-8 gelé sur un SHA exact. LE 23 = Codex audite A0.2 `cc796b5` ET le SHA final C-8. APRÈS PASS = merge A0.2, merge C-8, puis Competitor/OSS → SOURCE-FRESH.

---

## LOT C8-A — contrats + herméticité + scope gelé  *(peut démarrer SANS attendre Codex)*

**Scope :** (1) les 7 gates comme fonctions pures fail-closed dans `bubble/gates.py`, forme `{ok, decision, reason_code, reason, candidate_tree}` de `JOAO_C8_GATE_CONTRACTS.md` — logique correcte en isolation, pas encore câblée dans `approve()`/`promote()`. (2) **G-HERMETIC réel** : fixture autouse étendant `_isolated_joao_memory_dir` à TOUT root externe + auditeur d'ouvertures de fichier qui FAIL une évasion ; **déterminise D-044** (ledger externe + specs voisines remplacés par fixture gelée sous `tmp_path`, aucun skip/xfail) → suite par défaut **`EXIT=0`**. (3) **G-FROZEN-FINISH-LINE mécanique** : `frozen_mission.json` (spec_sha, roadmap_sha, acceptance_criterion_ids, allowed_write_paths, forbidden_paths, risk_tier, canary_required) écrit par `start()` + comparaison état-git→critères gelés. (4) stubs `scripts/audit_entrypoints.py` et `scripts/audit_test_entrypoints.py` (logique complète en C8-B).

**Fichiers/modules attendus :** neuf `bubble/gates.py` ; `RunRuntime.start()` (ajout écriture `frozen_mission.json` — additif) ; `tests/conftest.py` (étendre la fixture d'isolation) ; neuf `tests/test_c8_gates.py`, `tests/test_g_hermetic_self_check.py`, `tests/test_frozen_mission.py` ; neuf `scripts/audit_entrypoints.py` + `scripts/audit_test_entrypoints.py` (stubs). **Aucun** `.py` d'A0.2 modifié en assertion.

**Tests :** unitaires par gate (dicts d'entrée construits à la main) ; auto-check hermeticité ; frozen_mission map/BLOCK.

**Tests adversariaux :** un rouge→vert par reason code (`G_DBL_AUDIT_SAME_PROVIDER`, `G_HERMETIC_EXTERNAL_LEDGER_DEPENDENCY`, `G_FROZEN_FINISH_LINE_SCOPE_CREEP`, `G_SHA_BOUND_MISMATCH`, …) ; un test plantant une lecture de vrai chemin → doit être flaggé avant retrait ; un candidat écrivant hors `allowed_write_paths` → `SCOPE_CREEP`.

**Exigence reviewer :** **critical** (change l'infra de test + ajoute frozen_mission). Builder = session ≠ GLM → **GLM (`GLMReviewer` ou joao-glm read-only) + GPT**, 2 providers distincts. Codex final le 23.

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

**Scope :** (1) `GLMReviewer(ReviewerAdapter)` (WORKER §2.1) + **`GPTFormalEvidenceReviewer`** (`provider="openai-gpt"`, WORKER §5 — le 3ᵉ provider distinct pour les runs critical GLM-buildés), tous deux à l'allowlist du garde AST single-dispatch. (2) logique réelle de `scripts/audit_entrypoints.py` (G-NO-STALE) et `audit_test_entrypoints.py` (G-AUTH-IO). (2b) **preuve dynamique G-AUTH-IO** : spy/monkeypatch/trace d'appel asserttant que l'entrypoint réel est **invoqué** (pas seulement référencé) — l'audit statique seul est insuffisant (une référence morte le satisfait). (3) **contrôleur d'orchestration** (`bubble/orchestrator.py`) qui séquence `start→run_once→freeze→review(s)→promotion_ready` et **produit l'éligibilité mécanique sans fabriquer l'approbation humaine**. (4) G-DBL-AUDIT câblé : sélection reviewer par tier, builder≠reviewer, providers distincts, **preuve multi-review** liée au tree.

**Corrections pré-C8-B inscrites (finding GPT v3) — implémentées DANS ce lot, condition du GO C8-B :**
- **#1 `provider_family` :** chaque builder/reviewer porte `provider_family` (`zai`/`openai`/`anthropic`) ; tier critical exige **2 familles distinctes** ≠ famille builder. Codex+GPT = openai = insuffisant → un build GLM critical est **bloqué** (`G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE`) tant que `ClaudeCLIReviewer` (`anthropic`) n'existe pas. (Prévoir `ClaudeCLIReviewer` dans ce lot pour débloquer, OU accepter le blocage fail-closed du critical GLM.)
- **#2 import de preuve sécurisé :** `*-review-import.json` provient d'un **inbox contrôleur hors chemins builder**, avec **nonce + candidate_tree exact + ingestion unique** ; rejet sinon (empêche un builder de préfabriquer une fausse preuve). Tests adversariaux : import sans nonce / au mauvais tree / rejoué / dans une zone écriture-builder → refusé.
- **#3 ordre unifié + artefact :** `reviews → canary → promotion-readiness.json → approbation Boss → promote`. `promotion-readiness.json` = artefact mécanique lié au tree, **pas** un nouvel état `RunStatus`.

**Fichiers/modules attendus :** `bubble/runtime.py` (+`GLMReviewer`) ; `tests/test_a0_2_corrections.py` allowlist `_GUARDED_METHODS` (+`"GLMReviewer": {"review_stage"}` — **ajout** à un dict, jamais une assertion A0.2 modifiée ; diff revu contre G-FROZEN-FINISH-LINE) ; neuf `bubble/orchestrator.py`, `tests/test_c8_glm_reviewer.py`, `tests/test_c8_orchestration.py`, `tests/test_c8_dbl_audit_wiring.py` ; `scripts/audit_entrypoints.py`/`audit_test_entrypoints.py` (logique réelle).

**Tests :** `GLMReviewer` lie `candidate_tree` avant/après comme `CodexCLIReviewer` (via faux `joao-glm`) + un test `live_canary` manuel réel ; orchestration produit `promotion_ready` sans écrire d'`approval-record` humain ; G-DBL-AUDIT : normal 1-reviewer, critical 2-providers-distincts.

**Tests adversariaux :** candidat trafiqué en cours de review (fail-closed) ; dispatch GLM manquant → `block` ; run critical avec 2 slots remplis par GLM → `G_DBL_AUDIT_SAME_PROVIDER` ; **run GLM-buildé dont on tente GLM comme reviewer → `G_DBL_AUDIT_BUILDER_SELF_REVIEW`** (le paradoxe, testé) ; contrôleur tentant de fabriquer `approved_by=human` → refusé.

**Exigence reviewer :** **critical** — ajoute des capacités de dispatch/orchestration. Builder = session ≠ GLM → **GLM + GPT** (2 distincts). Codex final le 23.

**Critères d'acceptation :** `GLMReviewer` passe les formes tamper/fail-closed de `CodexCLIReviewer` ; `audit_entrypoints.py` = zéro dispatch non listé ; un dispatch GLM-reviewer réel (non mocké) réussit sur un candidat jouet, tracé comme `GLM_RUN_EVIDENCE.json` ; l'orchestrateur n'écrit jamais d'approbation humaine ; tests A0.2 existants inchangés et verts.

**Rollback :** retrait de `GLMReviewer` + son entrée allowlist + `orchestrator.py` ; `audit_*` revient aux stubs C8-A ; aucun défaut `RunRuntime` changé (aucun reviewer auto-sélectionné hors orchestrateur).

**Dépendances :** C8-A (formes de gate, fixture hermétique, frozen_mission).

**STOP :** si le contrat CLI réel de `joao-glm` a dérivé de WORKER §2.1 → STOP, re-vérifier contre le binaire live avant d'écrire `GLMReviewer` ; si câbler G-DBL-AUDIT exige de MODIFIER (pas seulement d'AJOUTER) un test A0.2 existant → STOP, adjudication Boss.

---

## LOT C8-C — SHA/canary/promotion + E2E  *(+ readiness SOURCE-FRESH)*

**Scope :** (1) G-SHA-BOUND-PROOF général (recompute-and-compare à l'écriture ET la consommation de preuve) + G-CANARY-FIRST dans `promotion._verify_acceptance_for_promotion`. (2) **E2E synthétique complet** traversant toute la chaîne assemblée (Boss GO → orchestrateur → build → gates → freeze → 2 reviewers critical → canary synthétique exact-SHA sans effet externe (D3) → promotion exigeant `promotion_ready` ET approbation humaine). (3) **rapport de readiness SOURCE-FRESH** (document seulement, pas de code SOURCE-FRESH).

**Fichiers/modules attendus :** `bubble/promotion.py` (préconditions additives dans la chaîne de vérif) ; `bubble/gates.py` (G-SHA-BOUND, G-CANARY complétés) ; neuf `tests/test_c8_canary_promotion.py`, `scripts/c8_e2e_synthetic_mission.py` (étend `a0_toy_mission_e2e.py`), `JOAO_C8_SOURCE_FRESH_READINESS_REPORT.md`.

**Tests :** promote refuse sans canary (`G_CANARY_FIRST_MISSING`), sur canary stale (`G_CANARY_FIRST_STALE_CANDIDATE`), réussit avec canary vert exact ; artefact au tree mismatché refusé (`G_SHA_BOUND_MISMATCH`) aux 2 bouts.

**Tests adversariaux :** canary du candidat A présenté à la promotion du candidat B → refusé (l'erreur cross-candidate rattrapée 3× à la main cette session) ; E2E répété avec un gate mal configuré par run (2ᵉ reviewer manquant en critical, canary manquant, candidat trafiqué) → halte au bon gate/reason code ; tentative de promotion avec `promotion_ready` mais sans approbation humaine → refusée.

**Exigence reviewer :** **critical** (chemin de promotion). Builder = session ≠ GLM → **GLM + GPT**. Codex final le 23 sur le SHA C-8 gelé.

**Critères d'acceptation :** happy-path E2E complet (build → 2 reviewers GO → canary pass → promotion) avec « SAME HASH TRACED END TO END: True » comme `a0_toy_mission_e2e.py`, étendu aux 2 verdicts + canary ; chaque run adversarial halte au bon gate ; workspace live + `memory/lessons.jsonl` byte-identiques avant/après ; le rapport readiness répond, preuve à l'appui : SOURCE-FRESH lançable via le contrôleur ? (premier run = `critical`+`canary_required`).

**Rollback :** suppression du script E2E + du rapport ; préconditions `promotion.py` = checks additifs révertables ; aucun changement de modèle de données candidat.

**Dépendances :** C8-A + C8-B (assemble tout).

**STOP :** si le happy-path ne peut atteindre « SAME HASH TRACED END TO END: True » que `a0_toy_mission_e2e.py` atteint déjà → STOP (régression inacceptable) ; si répondre à la readiness SOURCE-FRESH exige d'écrire du code SOURCE-FRESH → STOP immédiat (implémentation interdite sous ce nom).

---

## Gel du candidat C-8 (avant le 23)

Après C8-A→C8-C verts (GLM + GPT sur chaque lot), **geler le candidat C-8 sur un SHA/tree exact** + tag annoté provisoire, comme A0.2. Le 23 : Codex audite A0.2 `cc796b5` ET le SHA final C-8. Après double PASS Codex : merge A0.2, merge C-8, puis Competitor/OSS → SOURCE-FRESH. **Aucun merge/push/promotion avant le PASS Codex.**
