# JOÃO C-8 GATE CONTRACTS (v2 — post GPT counter-audit `CHANGES_REQUIRED`)

Phase 0 — spec only, no implementation. Planning branch `feat/joao-c8-gates-spec`, base
`10156a2` (tip A0.2 propre). A0.2 gelé sur `cc796b55808b95210adf9d219ecd91a240b2676c`
(tree `7ae68454…`, `PROVISIONALLY_CLOSED`, Codex `PENDING_2026-07-23`) — **non modifié**
par ce document. **Exactement 7 gates, pas de 8ᵉ.** D-046 : JOÃO = control plane, PAS sécurité OS.

**v2 corrige les 4 findings bloquants de GPT** (paradoxe GLM builder/reviewer ; G-FROZEN-FINISH-LINE rendu mécanique ; G-HERMETIC rendu réellement fail-closed avec suite EXIT=0 ; boucle de correction = 1) + la séparation validation-technique/approbation-Boss + les décisions D1–D7 (voir `JOAO_C8_OPEN_DECISIONS.md`).

Convention : chaque gate rend `{"ok": bool, "decision": "pass"|"block", "reason_code": str, "reason": str, "candidate_tree": sha|null, ...champs}` — même vocabulaire que les adaptateurs existants (`execution_backend.py` `PREFLIGHT_UNAVAILABLE`, `reviewer_contract.py` `"decision": "block"`). Fail-closed : doute/entrée manquante/exception → BLOCK. Reason codes = chaînes stables contractuelles.

## Politique d'entrée stricte (fail-closed sur toute entrée malformée)

Une couche de validation interne unique et réutilisable (`_require_*` dans `bubble/gates.py`) valide **chaque entrée contractuelle à la frontière de chaque gate**, avant toute logique métier. Elle remplace la correction champ-par-champ : c'est la **classe entière** des entrées malformées qui est fermée, pas seulement les charges utiles déjà observées.

**Règle 1 — aucune truthiness Python sur une valeur contractuelle.**
```
booléen contractuel : type(value) is bool           # STRICT
  REJETÉS : "true", "false", 1, 0, None, ""         # "false" est une chaîne TRUTHY en Python
chaîne obligatoire  : isinstance(value, str) and value.strip()
  REJETÉS : None, " ", "", 7, True                  # aucune conversion implicite
liste obligatoire   : isinstance(value, list)       # une str/mapping n'est jamais « une liste de »
  éléments typés ; None/int refusés ; vide refusé quand le contrat exige une preuve
mapping obligatoire : isinstance(value, Mapping) + toutes les clés requises présentes
```

**Règle 2 — seules les erreurs de VALIDATION deviennent un BLOCK.** `_MalformedInput` est une exception interne dédiée, levée uniquement par les validateurs ; chaque gate n'intercepte que ce type. Il n'existe **aucun `except Exception`** générique : un vrai défaut de programmation reste visible en développement au lieu d'être masqué en « BLOCK ».

**Garantie publique des sept gates.** Pour toute entrée, même arbitrairement malformée : jamais de `PASS` ; jamais d'`AttributeError`/`TypeError`/`KeyError` qui s'échappe ; toujours la forme de résultat normale avec `decision="block"` ; `candidate_tree` n'est présent dans le résultat que lorsqu'il est valide (sinon `null`).

**Reason codes d'entrée malformée** — au plus **un** code stable par gate, jamais un code par champ :

| Gate | Code |
|---|---|
| G-DBL-AUDIT | `G_DBL_AUDIT_MALFORMED_INPUT` |
| G-HERMETIC | `G_HERMETIC_MALFORMED_INPUT` |
| G-AUTH-IO | `G_AUTH_IO_MALFORMED_INPUT` |
| G-NO-STALE-ENTRYPOINT | `G_NO_STALE_ENTRYPOINT_MALFORMED_INPUT` |
| G-SHA-BOUND-PROOF | `G_SHA_BOUND_PROOF_MALFORMED_INPUT` |
| G-CANARY-FIRST | `G_CANARY_FIRST_MALFORMED_INPUT` |
| G-FROZEN-FINISH-LINE | `G_FROZEN_FINISH_LINE_MALFORMED_INPUT` |

Là où un code existant décrit déjà précisément la faute, il est **réutilisé** plutôt que doublé (ex. une identité reviewer incomplète reste `G_DBL_AUDIT_MALFORMED_VERDICT` ; un `candidate_tree` inutilisable en G-DBL-AUDIT reste `G_DBL_AUDIT_INSUFFICIENT_REVIEWERS`).

**Preuves obligatoires ajoutées (une preuve sans localisation n'est pas une preuve) :** `G-CANARY-FIRST` exige `canary_record.proof_path` non vide **et** `passed is True` exactement ; `G-SHA-BOUND-PROOF` exige `artifact.proof_path` non vide **et** un `candidate_tree` non vide des deux côtés.

**G-NO-STALE-ENTRYPOINT — `protected is True` obligatoire.** Tout entrypoint inventorié dont `protected` n'est pas exactement `True` **BLOQUE** (`G_NO_STALE_LEGACY_UNPROTECTED`), qu'il soit ancien ou tout neuf. Auparavant le blocage exigeait aussi `predates_gates=true`, si bien qu'un entrypoint de dispatch **neuf et non protégé** passait et le gate annonçait « every discovered callable is gate-protected » — ce qui était faux.

**G-HERMETIC — résolution obligatoire de `dir_fd`.** L'événement d'audit CPython `"open"` ne transporte **jamais** `dir_fd`. Un chemin relatif ouvert contre un descripteur de répertoire (`os.open("memory/lessons.jsonl", O_RDONLY, dir_fd=repo_fd)`) atteignait donc le vrai fichier sans être vu. `os.open` est désormais enveloppé et le descripteur est **résolu pour de vrai** (`fcntl F_GETPATH` sur macOS ; `/proc/self/fd` puis `/dev/fd` ailleurs), puis normalisé avant classification. Un `dir_fd` non résolvable **échoue fermé** (`HermeticViolation`), jamais ignoré. Aucune exemption de nom de fichier, de chemin, de module ou de node-id n'est introduite — et l'ancien faux positif sur le parcours fd de `shutil.rmtree` disparaît de lui-même, puisque ces entrées se résolvent maintenant vers leur vrai chemin `tmp`.

**Reason codes de succès (`ok=true`).** Chaque gate émet exactement un code de succès, stable et contractuel au même titre que ses codes de blocage : `G_DBL_AUDIT_OK`, `G_HERMETIC_OK`, `G_AUTH_IO_OK`, `G_NO_STALE_OK`, `G_SHA_BOUND_OK`, `G_CANARY_FIRST_OK`, `G_FROZEN_FINISH_LINE_OK`. Un seul code de succès supplémentaire existe, pour un gate dont la politique ne s'applique pas au run : `G_CANARY_FIRST_NOT_REQUIRED` (`canary_required=false`) — jamais un « pass » de fait obtenu par absence de vérification.

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

Chemins pour la 2ᵉ famille distincte en critical (décision d'implémentation, pas de nouveau provider payant) :
- **Correction (Codex+GPT = une seule famille) :** `Codex=openai`, `GPT=openai` — Codex et GPT (y compris un `GPTFormalEvidenceReviewer` à identité distincte fixée par le contrôleur, `provider="openai-gpt"`) partagent le **même** `provider_family`, donc **Codex+GPT est une seule famille**, jamais deux familles distinctes, quel que soit l'adaptateur d'identité utilisé. `GPTFormalEvidenceReviewer` reste un reviewer valide en tier `normal`, mais ne peut JAMAIS servir de 2ᵉ famille distincte pour un run `critical`. Un build `critical` d'un builder `zai` (GLM) exige **OpenAI + Anthropic**, ou une autre famille réellement distincte de `zai` — jamais deux outils `openai`.
- **Cible (backlog) :** ajouter un `ClaudeCLIReviewer` (ou autre provider de famille `anthropic`) comme 2ᵉ reviewer automatique.
- `GLMReviewer` reste utile comme reviewer indépendant **uniquement** quand le builder n'est PAS GLM (ex. `SandboxBuilder`, ou un futur builder d'un autre provider).

**Inputs :** `run.risk_tier` (`normal`|`critical` — D1 : absence → BLOCK) ; les verdicts du stage final, chacun avec son `reviewer.provider` **calculé par le contrôleur** (`reviewer_contract.py`, jamais l'identité auto-déclarée) ; `run.builder_provider`.
**Ordre de décision (précédence déterministe unique, appliquée aux DEUX tiers) :**

```
1. structure + identité de CHAQUE verdict fourni (pas seulement les ACCEPT)
2. tout verdict explicitement négatif (block/p1/ok≠true)  -> REVIEWER_DISAGREEMENT
3. cardinalité TOTALE exacte                              -> TOO_MANY_REVIEWERS
4. cardinalité des ACCEPT                                 -> INSUFFICIENT / NO_DISTINCT_FAMILY
5. indépendance builder/reviewer                          -> BUILDER_SELF_REVIEW
6. exigences de provider_family                           -> SAME_PROVIDER / SAME_FAMILY
```

Conséquence normative : un verdict négatif n'est **jamais** filtré hors de la décision — l'étape 2 voit tous les verdicts tels que fournis, et précède tout comptage. Table de vérité contractuelle :

| tier | verdicts | résultat |
|---|---|---|
| normal | `[PASS]` | **PASS** |
| normal | `[PASS, BLOCK]` | `G_DBL_AUDIT_REVIEWER_DISAGREEMENT` |
| normal | `[PASS, PASS]` | `G_DBL_AUDIT_TOO_MANY_REVIEWERS` |
| critical | `[PASS, PASS]` familles valides | **PASS** |
| critical | `[PASS, BLOCK]` | `G_DBL_AUDIT_REVIEWER_DISAGREEMENT` |
| critical | `[PASS, PASS, BLOCK]` | `G_DBL_AUDIT_REVIEWER_DISAGREEMENT` (le négatif prime sur la sur-cardinalité) |
| critical | `[PASS, PASS, PASS]` | `G_DBL_AUDIT_TOO_MANY_REVIEWERS` |

**Output / reason codes :**
- `G_DBL_AUDIT_OK` — succès : `normal` → exactement 1 verdict ACCEPT d'un provider ≠ builder ; `critical` → exactement 2 verdicts ACCEPT de 2 `provider_family` **distinctes entre elles ET distinctes du builder**. Tous liés au `candidate_tree` gelé, aucun dissident présent.
- `G_DBL_AUDIT_INSUFFICIENT_REVIEWERS` — moins de verdicts ACCEPT que requis pour le tier (couvre aussi : `risk_tier` absent/invalide, `builder_provider`/`builder_family` absent, `candidate_tree` absent/vide — entrées obligatoires, fail-closed).
- `G_DBL_AUDIT_TOO_MANY_REVIEWERS` — **cardinalité TOTALE EXACTE** : plus de verdicts que le tier n'en exige (`normal` > 1, `critical` > 2), tous positifs. Un verdict surnuméraire n'est jamais silencieusement ignoré ni toléré, même s'il est lui-même de famille distincte et non-builder. (Un sur-nombre **contenant** un négatif est classé `REVIEWER_DISAGREEMENT`, étape 2 < étape 3.)
- `G_DBL_AUDIT_MALFORMED_VERDICT` — **tout** verdict fourni (dissident compris, pas seulement les ACCEPT) sans identité exploitable — `provider`, `provider_family` ou `model` absent, vide, blanc ou non-`str` — ou qui n'est pas un objet, ou dont `decision` n'est pas ∈ {`pass`,`p1`,`block`}. Un verdict non identifié ne peut jamais prouver l'indépendance du reviewer (sans ce garde, `{"ok": true, "decision": "pass", "candidate_tree": …}` comptait comme reviewer valide avec `providers=[None]`, et les checks builder-self-review / distinct-family comparaient `None` à `None`).
- `G_DBL_AUDIT_REVIEWER_DISAGREEMENT` — **(D4)** **les DEUX tiers, un seul et même reason code** : au moins un verdict dissident (`decision` ∈ {`block`,`p1`} ou `ok` ≠ `true`) est présent. Évalué **AVANT** tout comptage, donc un dissident ne peut jamais être « out-voté » en ajoutant des ACCEPT. D4 interdit tout tie-break automatique : désaccord → BLOCK + escalade Boss. Il n'existe **pas** de second reason code de désaccord.
- `G_DBL_AUDIT_SAME_PROVIDER` — deux verdicts ACCEPT partagent un `reviewer.provider`. Atteignable uniquement au tier `critical` (au tier `normal`, deux verdicts sont déjà tranchés à l'étape 3).
- `G_DBL_AUDIT_SAME_FAMILY` — tier critical : deux verdicts partagent un `provider_family` (ex. Codex+GPT = openai).
- `G_DBL_AUDIT_BUILDER_SELF_REVIEW` — un reviewer a `provider == run.builder_provider` (ou `provider_family == builder_family`).
- `G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE` — tier critical mais aucun 2ᵉ reviewer d'une famille distincte n'existe → BLOCK fail-closed.
- `G_DBL_AUDIT_TREE_MISMATCH` — un verdict est lié à un tree ≠ `candidate_tree` gelé (vérifié sur **chaque** verdict fourni, étape 1).

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
- `G_HERMETIC_MISSING_AUDIT_JOURNAL` — **aucun journal d'ouvertures n'a été fourni** (`touches=None`) : l'auditeur n'a pas tourné, ou sa sortie est perdue. Un journal absent n'est jamais un journal propre — seule une liste **explicitement vide** (`[]`) signifie « l'auditeur a tourné et n'a vu aucune racine couverte ».
- `G_HERMETIC_REAL_MEMORY_TOUCHED` — lecture/écriture hors de la copie isolée `JOAO_MEMORY_DIR`.
- `G_HERMETIC_EXTERNAL_LEDGER_DEPENDENCY` — un test dépend d'un fichier hors repo et hors `tmp_path` (cas D-044 : `scripts/build_traceability.py` `DEFAULT_LEDGER = Path.home()/"Claude-HQ"/"DEFECTS_LEDGER.md"` — à injecter via fixture).
- `G_HERMETIC_UNINJECTED_ROOT` — un module a résolu une constante de root vers un chemin réel non injecté.

**Seeding de la racine mémoire isolée (correctif L3 #7) :** la fixture `_isolated_joao_memory_dir` sème les fichiers `.py` depuis les **sources du repo** (immuables, versionnées, hors périmètre du gate) mais toute **donnée** exclusivement depuis une **fixture gelée et committée** (`tests/fixtures/frozen_lessons.jsonl`), jamais depuis le `memory/` vivant. Auparavant elle copiait *chaque* fichier du vrai `memory/` avant même l'installation du hook — donc jamais interceptée, et le contenu « isolé » suivait le contenu live (remplacer `memory/lessons.jsonl` par 1 seul record faisait échouer `test_at_least_40_lessons` *à travers* la fixture « isolée »). Fixture gelée absente → `RuntimeError`, jamais un repli silencieux sur le fichier vivant.

**Real entrypoint :** fixture autouse de session dans `tests/conftest.py` (étend `_isolated_joao_memory_dir` à TOUT root externe) + un auditeur d'ouvertures de fichier qui FAIL un test qui s'échappe. C'est un gate de **détection et d'échec**, pas seulement de fourniture d'une copie isolée.

**Red→green :** rouge : un test lisant le vrai `~/Claude-HQ/DEFECTS_LEDGER.md` passe → `G_HERMETIC_EXTERNAL_LEDGER_DEPENDENCY`. vert : le même test lit une fixture gelée sous `tmp_path`, déterministe, suite `EXIT=0`. Source : L-051 (D-047), D-054, D-044.

---

## G-AUTH-IO — le contrôle est exercé via le VRAI point d'entrée protégé

**Claim :** la suite de tests de chaque gate/correctif appelle le **vrai point d'entrée de production** (`Adapter.review()`/`.build()`, une méthode `RunRuntime`, une frontière CLI subprocess) — jamais seulement un helper interne (`validate_reviewer_verdict()` appelé en direct sans prouver qu'il est câblé à `CodexEvidenceReviewer.review()` en production).

**Inputs :** pour chaque fichier de test de gate, la liste statique des call-sites dont dépendent ses assertions.
**Output / reason codes :**
- `G_AUTH_IO_HELPER_ONLY_COVERAGE` — un gate a des tests d'attaque mais aucun ne traverse le vrai entrypoint (le défaut d'origine du correctif A0.2 #5). Couvre aussi (correctif L3 #6) : `gate_name` absent/vide/blanc/non-`str` → BLOCK **avant toute autre vérification** — un verdict de couverture attribué à un gate anonyme est inauditable.
- `G_AUTH_IO_ENTRYPOINT_UNREACHABLE` — l'entrypoint réel nommé ne peut être construit/appelé depuis un test (doc de contrat périmée / entrypoint refactoré).

**Real entrypoint :** un **auditeur statique de suite de tests** (`scripts/audit_test_entrypoints.py`, nouveau) qui mappe chaque fichier de test de gate aux symboles d'entrypoint qu'il doit référencer ; exécuté en pre-close/CI (la cible auditée EST la suite de tests).

**Renforcement dynamique (à ajouter en C8-B — finding GPT v2, « à renforcer avant C8-B ») :** l'audit statique de **présence de symbole** est **nécessaire mais insuffisant** — une référence morte (un import/appel jamais exécuté) pourrait le satisfaire sans que l'entrypoint réel tourne. C8-B ajoute une **preuve dynamique** : un spy/monkeypatch/trace d'appel posé sur l'entrypoint réel (`GLMReviewer.review_stage`, `CodexEvidenceReviewer.review`, etc.) qui **assert que l'entrypoint a bien été invoqué** pendant le test du gate — pas seulement référencé. `G_AUTH_IO_HELPER_ONLY_COVERAGE` doit donc être prouvé par un compteur d'invocation réel, pas par une analyse statique seule. Source : C-8 §D, D-051 (contrôle→actif).

---

## G-NO-STALE-ENTRYPOINT — inventaire exhaustif, aucun entrypoint legacy/dupliqué/non-protégé

**Claim :** tout chemin capable de déclencher build/review/promotion/lecture d'artefact est énuméré exactement une fois, protégé par le même jeu de gates que son frère canonique ; aucun 2ᵉ chemin ancien/oublié n'atteint le même effet ungated.

**Inputs :** un inventaire AST (même technique que `test_a02_single_dispatch_point_ast_guard_no_direct_subprocess_in_adapters`) de tout callable atteignant (a) `ExecutionBackend.execute()`, (b) `RunRuntime.promote()`/`.approve()`, (c) une lecture de `PKG_ROOT`/artefact ; croisé contre une allowlist des entrypoints canoniques.
**Output / reason codes :**
- `G_NO_STALE_UNLISTED_DISPATCH` — un nouveau callable atteint le dispatch/promotion/lecture sans être dans l'allowlist (forme permanente et généralisée du garde single-dispatch d'A0.2, étendu « adapters seulement » → « tout le repo »). Couvre aussi : inventaire absent/vide, allowlist absente/vide, et (correctif L3 #5) **entrée d'inventaire malformée** — un `discovered_callable` sans clé `name` ou `effect` exploitable (ou qui n'est pas un objet) était silencieusement groupé sous `None` et passait tant qu'il était par ailleurs `canonical=True` ; un inventaire ininterprétable ne peut jamais prouver un inventaire exhaustif.
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
- `G_FROZEN_FINISH_LINE_SCOPE_CREEP` — un changement ne mappe vers aucun critère gelé, ou touche un `forbidden_path`, ou sort d'`allowed_write_paths` (ex. un 8ᵉ gate, une claim OS-security, un changement de roadmap en cours de run). Couvre aussi les entrées obligatoires manquantes : `spec_sha`, `roadmap_sha`, `authority_instruction_hash`, `criterion_bindings` vide, un AC dont `required_tests` est vide, et (correctif L3 #3) **`candidate_tree` absent/vide** — c'était le seul gate à ne pas valider son propre `candidate_tree` (contrairement à `gate_dbl_audit`/`gate_sha_bound_proof`), si bien qu'un `candidate_tree=None` et une preuve de test portant `candidate_tree=None` s'auto-appariaient par coïncidence (`None == None`) et validaient le gate.
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
