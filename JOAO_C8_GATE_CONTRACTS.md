# JOÃO C-8 GATE CONTRACTS (v2 — post GPT counter-audit `CHANGES_REQUIRED`)

Phase 0 — spec only, no implementation. Planning branch `feat/joao-c8-gates-spec`, base
`10156a2` (tip A0.2 propre). A0.2 gelé sur `cc796b55808b95210adf9d219ecd91a240b2676c`
(tree `7ae68454…`, `PROVISIONALLY_CLOSED`, Codex `PENDING_2026-07-23`) — **non modifié**
par ce document. **Exactement 7 gates, pas de 8ᵉ.** D-046 : JOÃO = control plane, PAS sécurité OS.

**v2 corrige les 4 findings bloquants de GPT** (paradoxe GLM builder/reviewer ; G-FROZEN-FINISH-LINE rendu mécanique ; G-HERMETIC rendu réellement fail-closed avec suite EXIT=0 ; boucle de correction = 1) + la séparation validation-technique/approbation-Boss + les décisions D1–D7 (voir `JOAO_C8_OPEN_DECISIONS.md`).

Convention : chaque gate rend `{"ok": bool, "decision": "pass"|"block", "reason_code": str, "reason": str, "candidate_tree": sha|null, ...champs}` — même vocabulaire que les adaptateurs existants (`execution_backend.py` `PREFLIGHT_UNAVAILABLE`, `reviewer_contract.py` `"decision": "block"`). Fail-closed : doute/entrée manquante/exception → BLOCK. Reason codes = chaînes stables contractuelles.

---

## G-DBL-AUDIT — deux auditeurs indépendants, à niveau de risque  *(v2: paradoxe GLM résolu)*

**Claim :** aucune éligibilité technique à la promotion sans le nombre requis de verdicts d'auditeurs **de providers distincts, tous distincts du provider builder**, liés au `candidate_tree` gelé. Le builder ne compte JAMAIS comme reviewer.

**Le paradoxe corrigé (finding GPT #1).** Le builder par défaut est GLM (`GLMBuilder.provider = "zai-coding-plan"`). Un futur `GLMReviewer.provider = "zai-coding-plan"` **partage** ce provider → un run construit par GLM ne peut pas compter GLM comme reviewer indépendant (`G_DBL_AUDIT_BUILDER_SELF_REVIEW`). Donc la cible « critical = GLM + Codex » était **impossible** quand le builder est GLM. Règle v2 :

```
normal   + builder=GLM  → 1 reviewer indépendant de famille ≠ builder : Codex (CodexCLIReviewer)
critical + builder=GLM  → reviewer #1 : Codex          (provider_family = openai)
                         → reviewer #2 : famille DISTINCTE, ≠ openai ≠ builder → Claude/Anthropic
                         → si aucun 3ᵉ reviewer d'une famille distincte n'existe encore → BLOCK (fail-closed)
```

**`provider_family` (correction pré-C8-B #1 — finding GPT v3) :** Codex et GPT ne sont PAS deux providers indépendants — ce sont deux outils **OpenAI** (même `provider_family`). Chaque builder/reviewer porte donc un `provider_family` (`glm`→`zai`, `codex`→`openai`, `gpt`→`openai`, `claude`→`anthropic`). Le tier `critical` exige **2 familles distinctes**, toutes ≠ famille du builder — pas seulement 2 `provider` distincts. Pour un build GLM critique : Codex (openai) + Claude (anthropic). Codex + GPT = **1 seule famille** → refusé. Tant qu'un reviewer d'une famille distincte de openai (p. ex. `ClaudeCLIReviewer`) n'existe pas, un build GLM **critique** est **bloqué** (fail-closed), jamais accepté avec 2 verdicts OpenAI.

Chemins zéro-coût pour le 3ᵉ provider (décision d'implémentation, pas de nouveau provider payant) :
- **Intérim (dès C8-B) :** un **contre-audit GPT formel** lié au SHA, importé via un adaptateur à **identité distincte fixée par le contrôleur** — `GPTFormalEvidenceReviewer` (`provider="openai-gpt"`), **jamais** `CodexEvidenceReviewer` (qui écraserait l'identité en `provider="codex"` → un 2ᵉ verdict Codex, pas un 3ᵉ provider ; voir WORKER §5). C'est le 2ᵉ provider réellement distinct pour un run critical GLM-buildé, jusqu'à ce qu'un 3ᵉ adaptateur automatique existe.
- **Cible (backlog) :** ajouter un `ClaudeCLIReviewer` (ou autre 3ᵉ provider) comme 2ᵉ reviewer automatique.
- `GLMReviewer` reste utile comme reviewer indépendant **uniquement** quand le builder n'est PAS GLM (ex. `SandboxBuilder`, ou un futur builder d'un autre provider).

**Inputs :** `run.risk_tier` (`normal`|`critical` — D1 : absence → BLOCK) ; les verdicts du stage final, chacun avec son `reviewer.provider` **calculé par le contrôleur** (`reviewer_contract.py`, jamais l'identité auto-déclarée) ; `run.builder_provider`.
**Output / reason codes :**
- `ok=true` ssi : `normal` → exactement 1 verdict ACCEPT d'un provider ≠ builder ; `critical` → 2 verdicts ACCEPT de 2 providers **distincts entre eux ET distincts du builder**.
- `G_DBL_AUDIT_INSUFFICIENT_REVIEWERS` — moins de verdicts ACCEPT que requis pour le tier.
- `G_DBL_AUDIT_SAME_PROVIDER` — deux verdicts partagent un `reviewer.provider`.
- `G_DBL_AUDIT_SAME_FAMILY` — tier critical : deux verdicts partagent un `provider_family` (ex. Codex+GPT = openai).
- `G_DBL_AUDIT_BUILDER_SELF_REVIEW` — un reviewer a `provider == run.builder_provider` (ou `provider_family == builder_family`).
- `G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE` — tier critical mais aucun 2ᵉ reviewer d'une famille distincte n'existe → BLOCK fail-closed.
- `G_DBL_AUDIT_TREE_MISMATCH` — un verdict est lié à un tree ≠ `candidate_tree` gelé.

**Real entrypoint :** la **vérification** vit dans `bubble/gates.py` et est appelée par le contrôleur au passage `REVIEWING → promotion_ready` **et** re-vérifiée dans `RunRuntime.promote()` (comme `promotion._verify_acceptance_for_promotion` garde déjà `promote()`). Elle ne fabrique jamais l'approbation humaine (voir « Séparation » en fin de doc).

**Red→green :** rouge : run `critical`, builder GLM, reviewers GLM+Codex → doit `G_DBL_AUDIT_BUILDER_SELF_REVIEW` (GLM = builder). rouge : 2 reviewers GLM → `G_DBL_AUDIT_SAME_PROVIDER`. vert : Codex + 3ᵉ provider (GPT formel ou ClaudeCLIReviewer), tous deux ACCEPT sur le tree gelé → pass. Source : L-049/L-056 (D-045/D-052), « deux auditeurs non négociables » (D-051).

---

## G-HERMETIC — la suite par défaut ne touche AUCUN chemin réel/externe  *(v2: réellement fail-closed, suite EXIT=0)*

**Claim :** `pytest` par défaut ne dépend d'aucune **racine de données sensible/mutable** hors `tmp_path`/root injecté. La portée du gate est **explicitement bornée** à ces racines (finding GPT v2 #2 : le gate ne peut PAS interdire toute ouverture de fichier — pytest doit forcément lire le code du repo, Python/stdlib et les dépendances installées) :

```
COUVERT (doit résoudre sous tmp_path / root injecté) :
  memory/                         # vraie mémoire lessons.jsonl
  ledgers externes                # ~/Claude-HQ/DEFECTS_LEDGER.md
  repos frères                    # ~/job-opportunity-radar*
  workspaces utilisateur          # workspace live d'un run
  credentials / config utilisateur
  artefacts runtime               # run_dir, state, evidence d'un vrai run
EXPLICITEMENT AUTORISÉ (immuable, hors gate) :
  sources du repo sous test, Python/stdlib, packages installés, .pytest_cache
```

`ok=true` **uniquement** si aucune ouverture ne résout vers une **racine de données couverte non injectée**.

**Contradiction corrigée (finding GPT #3).** La v1 prévoyait de **garder** la dépendance externe de `test_real_repo_produces_zero_unmapped_with_cv_bot_specs` (D-044) en produisant un simple rapport non bloquant, laissant la suite à `329 passed / 1 failed`. Ce n'est ni fail-closed ni hermétique. **v2 :** on ne masque PAS D-044 — on rend son test **hermétique** :

```
ledger externe réel (~/Claude-HQ/DEFECTS_LEDGER.md)  → remplacé par une fixture gelée sous tmp_path
specs du repo voisin (~/job-opportunity-radar/...)   → copiées/injectées sous tmp_path
le test s'exécute toujours (aucun skip / aucun xfail) → résultat déterministe
```

Après M2 (lot C8-A), la suite par défaut = **`EXIT=0`**. Le **défaut** D-044 (le vrai trou de traçabilité produit) reste **ouvert** dans le ledger/backlog produit — mais la **suite** ne dépend plus du contenu courant de `~/Claude-HQ` ni d'un repo voisin. Le test devient une assertion déterministe sur une fixture connue, plus une lecture d'état vivant.

**Inputs :** la valeur résolue de chaque root surchargable en début de session (`JOAO_MEMORY_DIR`, déjà A0.2 correctif 7 ; + équivalents ledger/specs que C8-A ajoute) ; le journal des chemins réellement ouverts pendant un run de test.
**Output / reason codes :**
- `G_HERMETIC_REAL_MEMORY_TOUCHED` — lecture/écriture hors de la copie isolée `JOAO_MEMORY_DIR`.
- `G_HERMETIC_EXTERNAL_LEDGER_DEPENDENCY` — un test dépend d'un fichier hors repo et hors `tmp_path` (cas D-044 : `scripts/build_traceability.py` `DEFAULT_LEDGER = Path.home()/"Claude-HQ"/"DEFECTS_LEDGER.md"` — à injecter via fixture).
- `G_HERMETIC_UNINJECTED_ROOT` — un module a résolu une constante de root vers un chemin réel non injecté.

**Real entrypoint :** fixture autouse de session dans `tests/conftest.py` (étend `_isolated_joao_memory_dir` à TOUT root externe) + un auditeur d'ouvertures de fichier qui FAIL un test qui s'échappe. C'est un gate de **détection et d'échec**, pas seulement de fourniture d'une copie isolée.

**Red→green :** rouge : un test lisant le vrai `~/Claude-HQ/DEFECTS_LEDGER.md` passe → `G_HERMETIC_EXTERNAL_LEDGER_DEPENDENCY`. vert : le même test lit une fixture gelée sous `tmp_path`, déterministe, suite `EXIT=0`. Source : L-051 (D-047), D-054, D-044.

---

## G-AUTH-IO — le contrôle est exercé via le VRAI point d'entrée protégé

**Claim :** la suite de tests de chaque gate/correctif appelle le **vrai point d'entrée de production** (`Adapter.review()`/`.build()`, une méthode `RunRuntime`, une frontière CLI subprocess) — jamais seulement un helper interne (`validate_reviewer_verdict()` appelé en direct sans prouver qu'il est câblé à `CodexEvidenceReviewer.review()` en production).

**Inputs :** pour chaque fichier de test de gate, la liste statique des call-sites dont dépendent ses assertions.
**Output / reason codes :**
- `G_AUTH_IO_HELPER_ONLY_COVERAGE` — un gate a des tests d'attaque mais aucun ne traverse le vrai entrypoint (le défaut d'origine du correctif A0.2 #5).
- `G_AUTH_IO_ENTRYPOINT_UNREACHABLE` — l'entrypoint réel nommé ne peut être construit/appelé depuis un test (doc de contrat périmée / entrypoint refactoré).

**Real entrypoint :** un **auditeur statique de suite de tests** (`scripts/audit_test_entrypoints.py`, nouveau) qui mappe chaque fichier de test de gate aux symboles d'entrypoint qu'il doit référencer ; exécuté en pre-close/CI (la cible auditée EST la suite de tests).

**Renforcement dynamique (à ajouter en C8-B — finding GPT v2, « à renforcer avant C8-B ») :** l'audit statique de **présence de symbole** est **nécessaire mais insuffisant** — une référence morte (un import/appel jamais exécuté) pourrait le satisfaire sans que l'entrypoint réel tourne. C8-B ajoute une **preuve dynamique** : un spy/monkeypatch/trace d'appel posé sur l'entrypoint réel (`GLMReviewer.review_stage`, `CodexEvidenceReviewer.review`, etc.) qui **assert que l'entrypoint a bien été invoqué** pendant le test du gate — pas seulement référencé. `G_AUTH_IO_HELPER_ONLY_COVERAGE` doit donc être prouvé par un compteur d'invocation réel, pas par une analyse statique seule. Source : C-8 §D, D-051 (contrôle→actif).

---

## G-NO-STALE-ENTRYPOINT — inventaire exhaustif, aucun entrypoint legacy/dupliqué/non-protégé

**Claim :** tout chemin capable de déclencher build/review/promotion/lecture d'artefact est énuméré exactement une fois, protégé par le même jeu de gates que son frère canonique ; aucun 2ᵉ chemin ancien/oublié n'atteint le même effet ungated.

**Inputs :** un inventaire AST (même technique que `test_a02_single_dispatch_point_ast_guard_no_direct_subprocess_in_adapters`) de tout callable atteignant (a) `ExecutionBackend.execute()`, (b) `RunRuntime.promote()`/`.approve()`, (c) une lecture de `PKG_ROOT`/artefact ; croisé contre une allowlist des entrypoints canoniques.
**Output / reason codes :**
- `G_NO_STALE_UNLISTED_DISPATCH` — un nouveau callable atteint le dispatch/promotion/lecture sans être dans l'allowlist (forme permanente et généralisée du garde single-dispatch d'A0.2, étendu « adapters seulement » → « tout le repo »).
- `G_NO_STALE_DUPLICATE_PATH` — deux callables atteignent un effet équivalent, l'un non canonique.
- `G_NO_STALE_LEGACY_UNPROTECTED` — un entrypoint inventorié prédate un gate et n'appelle pas la chaîne de gates courante.

**Real entrypoint :** `scripts/audit_entrypoints.py` (nouveau) — audit AST statique, étape requise pre-close/CI, son exit code gate la clôture du lot (comme `git diff --check` gate les clôtures de candidat de cette session). Source : L-057 (D-053 / R-1, PID 57084).

---

## G-SHA-BOUND-PROOF — toute preuve est liée au commit/tree exact  *(méta-gate)*

**Claim :** aucun record (test, attaque, canary, promotion, log de gate-run) n'est accepté comme preuve pour un candidat sans son `candidate_tree` exact (et `candidate_commit` où une promotion a eu lieu). Hérite de RI-3/RI-4/A0-1 (`recompute_candidate_tree`, déjà utilisé par `promotion.py` et les 2 reviewers), **étendu à CHAQUE artefact** que C-8 introduit (canary, logs de gate).

**Inputs :** chaque artefact de preuve + le `candidate_tree`/`candidate_commit` que l'état `RunRuntime` détient.
**Output / reason codes :**
- `G_SHA_BOUND_MISSING` — artefact sans champ `candidate_tree`.
- `G_SHA_BOUND_MISMATCH` — le tree enregistré ≠ le tree recalculé indépendamment au moment de la consommation (candidat muté entre production et consommation de la preuve).
- `G_SHA_BOUND_CROSS_CANDIDATE` — un artefact d'un candidat présenté comme preuve d'un autre (l'erreur exacte que cette session a rattrapée 3× à la main : le tag Claude-review prématuré, le candidat contaminé mémoire — G-SHA-BOUND rend ce check automatique).

**Real entrypoint :** le chemin d'écriture de preuve de `RunRuntime` (là où gate/canary results sont persistés au `run_dir`, comme `build-review-evidence.json`/`final-review-evidence.json`) ET le chemin de consommation (ce qui lit ces fichiers pour décider éligibilité/`promote()`) — les deux bouts. Source : L-058 (C-8 §G), RI-3/RI-4.

---

## G-CANARY-FIRST — promotion bloquée sans canary vert sur le candidat exact

**Claim :** pour tout run dont la politique exige un canary (D3 : canary automatisé accepté s'il est synthétique, exact-SHA, sans effet externe réel), `promote()` refuse s'il n'existe pas de résultat canary **vert** pour le `candidate_tree` exact promu.

**Inputs :** `run.canary_required` (bool, par tier/politique de mission) ; un record canary (même forme SHA-bound : `candidate_tree`, pass/fail, chemin de preuve brute).
**Output / reason codes :**
- `G_CANARY_FIRST_MISSING` — `canary_required==true` et aucun record canary pour ce candidat.
- `G_CANARY_FIRST_FAILED` — record canary présent mais fail.
- `G_CANARY_FIRST_STALE_CANDIDATE` — canary vert mais pour un autre `candidate_tree`.

**Real entrypoint :** `RunRuntime.promote()` / `promotion._verify_acceptance_for_promotion` — une précondition de plus dans la fonction que le correctif A0.2 #8 durcit déjà, pas un nouveau chemin. Source : L-058 (C-8 §H), patron canary CV-SEC.

---

## G-FROZEN-FINISH-LINE — scope/critères gelés, réellement mécanique  *(v2: n'est plus documentaire)*

**Claim :** au `start()`, la mission gèle son scope et ses critères ; ensuite, tout changement (chemins modifiés, changement de mission, nouvelle correction demandée) doit **mapper vers un critère gelé**, sinon BLOCK. Aucun finding, même réel, ne peut élargir silencieusement le scope en cours de run.

**Correction (finding GPT #2).** La v1 admettait « pas de point d'entrée, repose sur la discipline » — or C-8 doit rendre les 7 leçons **exécutables**. v2 introduit un mécanisme réel :

```
frozen_mission.json  (écrit une fois par start(), immuable — comme checkpoints/0000-pending.json)
  spec_sha / roadmap_sha        # SHA des SPEC/ROADMAP approuvés
  risk_tier / canary_required
  forbidden_paths               # chemins interdits (ex. A0.2, memory/lessons.jsonl)
  criterion_bindings:           # LE mapping mécanique changement→critère (finding GPT v2 #3)
    AC-C8-001:
      allowed_paths:   ["src/joao_orchestrator/bubble/gates.py", "tests/test_c8_gates.py"]
      required_tests:  ["tests/test_c8_gates.py::test_g_dbl_audit_*"]
      allowed_actions: ["modify", "create"]     # modify = hunks additifs bornés
    AC-C8-002: { allowed_paths: [...], required_tests: [...], allowed_actions: [...] }
    ...
```

Sans `criterion_bindings`, les critères et les chemins existaient mais **sans relation mécanique** — le gate ne pouvait pas *prouver* qu'un fichier modifié « mappe vers un critère gelé » (le trou signalé par GPT v2 #3). Avec, le gate compare, avant chaque freeze/review :
```
pour chaque path modifié (git status) + chaque correction demandée :
   il doit exister UN criterion_bindings[AC].allowed_paths qui couvre le path
   ET l'action (modify/create) ∈ allowed_actions de ce même AC
   ET le path ∉ forbidden_paths
   → sinon BLOCK
toute correction en cours de run doit NOMMER son acceptance_criterion_id (AC-...), sinon BLOCK
pour chaque AC couvert par le run : CHACUN de ses required_tests doit produire un résultat PASS
   lié au candidate_tree exact (preuve SHA-bound, G-SHA-BOUND-PROOF) → sinon BLOCK
```

**Exigence C8-A (finding GPT v3) :** dès C8-A, un critère n'est « satisfait » que si **tous ses `required_tests` passent (PASS) avec une preuve liée au `candidate_tree`** ; un `required_test` manquant, rouge, ou non lié au tree → BLOCK. C'est ce qui empêche un `criterion_bindings` d'être une déclaration vide.

**Inputs :** `frozen_mission.json` ; l'ensemble des chemins modifiés du candidat ; toute correction/finding proposé en cours de run.
**Output / reason codes :**
- `G_FROZEN_FINISH_LINE_SCOPE_CREEP` — un changement ne mappe vers aucun critère gelé, ou touche un `forbidden_path`, ou sort d'`allowed_write_paths` (ex. un 8ᵉ gate, une claim OS-security, un changement de roadmap en cours de run).
- `G_FROZEN_FINISH_LINE_REQUIRES_NEW_AUTHORITY` — le changement est réel et souhaitable mais exige un nouveau run/document Boss distinct — jamais une inclusion silencieuse.

**Real entrypoint :** `RunRuntime.start()` écrit `frozen_mission.json` ; une fonction `bubble/gates.py` est appelée avant freeze/review (dans `_execute`) et compare l'état git réel du candidat au `frozen_mission.json`. Renforcé mécaniquement par les allowlists de G-NO-STALE-ENTRYPOINT et G-DBL-AUDIT. **Ce gate a désormais un vrai call-site et un vrai artefact, il n'est plus documentaire.**

**Red→green :** rouge : un candidat écrit un fichier hors `allowed_write_paths` (ou un 8ᵉ gate) → `G_FROZEN_FINISH_LINE_SCOPE_CREEP`. vert : tous les chemins modifiés mappent vers les critères gelés → pass. Source : L-050 (D-043/D-046), ligne d'arrivée gelée CV-SEC, D-055 (scope-leak).

---

## Séparation validation-technique ≠ approbation-Boss  *(finding GPT #5, s'applique à G-DBL-AUDIT et à la promotion)*

`RunRuntime.approve()` représente aujourd'hui l'**approbation humaine** (écrit `approval-record.json`, `approved_by: "human"`). Le contrôleur ne doit **jamais** l'appeler pour fabriquer une approbation « human » à la place du Boss. v2 distingue trois choses :

**Ordre unifié (correction pré-C8-B #3 — finding GPT v3) :**
```
reviews  →  canary  →  promotion-readiness.json  →  approbation Boss  →  promote()
```
- `promotion-readiness.json` = **artefact/flag mécanique** écrit par le contrôleur (gates verts + verdicts de familles distinctes liés au tree + canary vert), lié au `candidate_tree`. **PAS un nouvel état caché de `RunStatus`** — aucun nouvel état runtime n'est introduit ; c'est un fichier de preuve dans le `run_dir`.
- `approval-record.json` = **autorisation humaine** (le Boss, instruction nommée hashée et liée au run — D5). Jamais fabriquée par le contrôleur.
- `promote()` exige les DEUX : `promotion-readiness.json` valide (lié au tree) ET `approval-record.json` humain réel. « Techniquement éligible » ≠ « approuvé humainement ».

## Ce que ces 7 gates NE sont PAS (D-046)

Aucun ne recrée une sécurité OS maison (sandbox process, ACL réseau domaine, isolation kernel). Ils gouvernent la livraison : qui revoit, sur quel SHA, avec quelle preuve, dans quel ordre, avec quels critères gelés. L'isolation reste `ExecutionBackend` (`local_untrusted` ; `container`/`vm` = déclarés, non implémentés). **Pas de 8ᵉ gate.**
