# JOÃO WORKER INTEGRATION SPEC (v2 — post GPT counter-audit)

Phase 0 — spec only. Branche `feat/joao-c8-gates-spec`, base `10156a2`. A0.2 gelé `cc796b5`, non modifié.
**v2 :** résout le paradoxe GLM builder/reviewer (finding #1), sépare éligibilité mécanique/approbation Boss (#5), corrige la boucle de correction à 1 (#4), inscrit D1–D7.

## 1. Current-state architecture (vérifiée en source)

| Rôle | Classe | Fichier | Dispatch réel | Statut |
|---|---|---|---|---|
| Builder (fixture) | `SandboxBuilder` | `bubble/runtime.py` | callable Python direct | done |
| Builder (GLM réel) | `GLMBuilder` | `runtime.py:274` | `~/.local/bin/joao-glm --mode workspace-write` → Z.AI `zai-coding-plan/glm-4.5-air` | **réel** (siblings read-only utilisés 3× cette session) |
| Reviewer (import) | `CodexEvidenceReviewer` | `runtime.py:320` | lit `run_dir/review-import.json` → `validate_reviewer_verdict` | done, reviewer par défaut |
| Reviewer (Codex live) | `CodexCLIReviewer` | `runtime.py:336` | `codex exec --json --sandbox read-only -C <readonly_copy>` via `ExecutionBackend.execute()` | **réel** |
| Reviewer (GLM live) | *(aucun)* | — | — | **MANQUE** — la vraie brique à ajouter |
| Orchestration (Boss GO→promotion) | *(aucun)* | — | — | **MANQUE** — chaque run historique fut piloté à la main |

Le gap réel = (a) un `GLMReviewer`, (b) un contrôleur d'orchestration. GLM-builder et Codex-reviewer sont du code réel, tournant.

## 2. Target architecture

### 2.1 `GLMReviewer(ReviewerAdapter)` — nouveau, `bubble/runtime.py`

Miroir de `CodexCLIReviewer`, cible de dispatch substituée :
- `provider = "zai-coding-plan"`, `model = os.environ.get("JOAO_GLM_MODEL", "zai-coding-plan/glm-4.5-air")`.
- `executable = ~/.local/bin/joao-glm` (le MÊME binaire que `GLMBuilder`, en `--mode read-only`).
- `review_stage(...)` : même discipline de liaison candidat que `CodexCLIReviewer` (recompute `candidate_tree` avant ET après dispatch pour `build`/`final`), parse la sortie JSON-lines de `joao-glm`, passe par `validate_reviewer_verdict` (pas de 2ᵉ parseur divergent). Ajouté à l'allowlist du garde AST single-dispatch (`_GUARDED_METHODS`).

**Contrainte d'indépendance (finding GPT #1) :** `GLMReviewer.provider == GLMBuilder.provider == "zai-coding-plan"`. Donc `GLMReviewer` **ne peut PAS** être compté comme reviewer indépendant d'un candidat **construit par GLM** — il déclencherait `G_DBL_AUDIT_BUILDER_SELF_REVIEW`. `GLMReviewer` n'est un reviewer indépendant valide que lorsque le builder n'est PAS GLM (`SandboxBuilder`, ou futur builder d'un autre provider). Pour les runs GLM-buildés (le cas par défaut), voir §5.

### 2.2 Contrôleur d'orchestration — `bubble/orchestrator.py` (nom TBD, pas créé en Phase 0)

Pilote fin, PAS une réécriture de `RunRuntime` :

```
Boss GO (référence SPEC+ROADMAP approuvée, hashée — D5)
  → load_approved_spec_and_roadmap(ref) → mission, risk_tier (obligatoire, D1), canary_required
  → RunRuntime.start(...)  → écrit frozen_mission.json (G-FROZEN-FINISH-LINE)   [existant + additif]
  → RunRuntime.run_once(...)  [build + tests]                                    [existant]
  → G-HERMETIC / G-NO-STALE-ENTRYPOINT vérifiés dans le run de test lui-même
  → candidate_tree gelé (RI-3)                                                   [existant]
  → lancer reviewer(s) selon le tier (G-DBL-AUDIT) :
       normal   → 1 reviewer, provider ≠ builder
       critical → 2 provider_family DISTINCTES, toutes ≠ famille builder
                  (builder GLM/zai -> Codex/openai + une 2e famille != openai, ex. Claude/anthropic)
  → reviews (familles distinctes selon tier, liées au tree)                        (G-DBL-AUDIT)
  → si canary_required : canary synthétique exact-SHA sans effet externe (D3)       (G-CANARY-FIRST)
  → écrire promotion-readiness.json (ARTEFACT mécanique lié au tree — PAS un nouvel état RunStatus)
  → [ARRÊT BOSS] : approbation humaine (approval-record.json) — jamais fabriquée par le contrôleur
  → RunRuntime.promote()  → exige promotion-readiness.json valide ET approval humain réel   (G-NO-STALE, G-SHA-BOUND, G-CANARY re-check)
```

Le contrôleur choisit la combinaison de reviewers et appelle `RunRuntime` dans l'ordre avec les gates actifs. Il ne réimplémente rien du moteur.

## 3. Boss GO / contrat SPEC+ROADMAP approuvée  *(D5)*

« Boss GO » = instruction humaine explicite, datée, nommant un document de scope exact — comme cette session. Le contrôleur refuse d'agir sans une référence de mission (chemin + SHA git d'approbation) nommée dans l'instruction. **v2 (D5) :** ce texte d'autorité est **hashé et lié au run** (`frozen_mission.json.spec_sha`/`roadmap_sha` + hash de l'instruction), enregistré dans la preuve du run. Pas de schéma de signature crypto (hors scope, zéro coût) — la « signature » MVP est le texte d'instruction lui-même, hashé.

## 4. GLM/ZCode launch contract (builder)

Déjà réel (`GLMBuilder`). Argv : `~/.local/bin/joao-glm --workspace <ws> --task-file <run_dir>/builder-task.md --output <run_dir>/glm-builder.jsonl --mode workspace-write --budget small --allowed-path <p>…`, via `ExecutionBackend.execute(network=<capabilities.network_capability>, environment_allowlist=_GLM_ENV_ALLOWLIST, extra_write_paths=[run_dir])`. Réseau uniquement depuis le scope gelé (correctif A0.2 #2, fail-closed si absent). Hash builder recalculé par le contrôleur (RI-5). Seul ajout C-8 : `set_capabilities(frozen_scope)` appelé par le contrôleur au même point qu'aujourd'hui — pas de changement de `GLMBuilder`.

## 5. Codex/GLM reviewer launch contract  *(v4: 2ᵉ FAMILLE distincte, jamais un simple « 3ᵉ provider »)*

**Modèle d'identité faisant autorité (aucune autre lecture n'est valide) :**

```
GLM / Z.AI          -> provider_family = zai
Codex               -> provider_family = openai
GPT (contre-audit)  -> provider_family = openai      # MÊME famille que Codex
Claude              -> provider_family = anthropic
```

Le tier `critical` exige **2 `provider_family` distinctes entre elles ET distinctes de la famille du builder** — jamais un simple comptage de providers. Pour une mission critical **buildée par GLM (`zai`)** : Codex/`openai` + Claude/`anthropic` est **VALIDE** ; Codex/`openai` + GPT/`openai` est **INVALIDE** (une seule famille) ; si aucune 2ᵉ famille valide n'est disponible → **BLOCK** fail-closed (`G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE`), jamais une acceptation avec deux verdicts OpenAI.

Il n'existe **aucune** famille « session » : le builder des lots C-8 est `provider_family = anthropic` (session Claude pilotée par un humain), ce qui est précisément pourquoi un reviewer Claude ne peut pas auditer indépendamment un lot C-8 construit par cette session.

- **Codex :** réel (`CodexCLIReviewer`), inchangé — reviewer #1 par défaut d'un candidat GLM-buildé.
- **GLM :** nouveau (`GLMReviewer`, §2.1), `joao-glm --mode read-only` — reviewer indépendant **seulement si builder ≠ GLM**.
- **2ᵉ FAMILLE (tier critical, builder=GLM) :** reviewer #2 doit appartenir à une `provider_family` ≠ `zai` **et** ≠ celle du reviewer #1 (donc ≠ `openai` si #1 est Codex). **Piège corrigé (finding GPT v2 #1) :** ne PAS importer le verdict GPT via `CodexEvidenceReviewer` — cet adaptateur fixe l'identité à `provider="codex"`, donc le verdict GPT deviendrait un **2ᵉ verdict Codex**, pas une 2ᵉ famille distincte → G-DBL-AUDIT échouerait sur un run critical GLM-buildé. Il faut un adaptateur d'import à **identité distincte fixée par le contrôleur** :
  - **`GPTFormalEvidenceReviewer(ReviewerAdapter)` :** `provider = "openai-gpt"`, `provider_family = "openai"`, `model = os.environ.get("JOAO_GPT_MODEL", "gpt-5.6-thinking")` ; lit un import off-disk → `validate_reviewer_verdict(..., provider="openai-gpt", ...)`. **L'identité vient de l'adaptateur/contrôleur, JAMAIS du JSON importé.**
  - **MAIS (correction pré-C8-B #1 — finding GPT v3) :** `GPTFormalEvidenceReviewer.provider_family == "openai" == CodexCLIReviewer.provider_family`. Codex + GPT = **une seule famille**. Pour un run **critical** GLM-buildé exigeant 2 familles distinctes, GPT-formel **ne suffit PAS** — il faut une famille ≠ openai (Anthropic/Claude). GPT-formel reste utile comme reviewer `normal` ou comme 2ᵉ avis non-family-critique ; mais tant que `ClaudeCLIReviewer` (famille `anthropic`) n'existe pas, un build GLM **critical** est **bloqué** (`G_DBL_AUDIT_NO_DISTINCT_FAMILY_AVAILABLE`), jamais accepté avec Codex+GPT.
  - **Cible (débloque le critical GLM) :** `ClaudeCLIReviewer` (`provider_family="anthropic"`), 3ᵉ adaptateur à dispatch automatique.

**Sécurité de l'import de preuve (correction pré-C8-B #2 — finding GPT v3) :** un fichier de verdict importé (`gpt-review-import.json` et tout `*-review-import.json`) doit provenir d'un **inbox contrôleur hors des chemins accessibles au builder** (jamais sous `workspace`/`allowed_write_paths` du builder), et porter : (a) un **nonce** émis par le contrôleur pour ce run, (b) le **`candidate_tree` exact**, (c) une **ingestion unique** (consommé une seule fois, marqué). Sinon un builder pourrait **préfabriquer une fausse preuve GPT**. Le contrôleur rejette un import sans nonce valide, au mauvais tree, déjà consommé, ou situé dans une zone écriture-builder.
- **Identité provider :** constantes de classe écrasées dans `proof.reviewer` (jamais l'auto-déclaré) — réutilise A0.2 correctif 5.

## 6. Modèle de preuve SHA/tree

Réutilise RI-3 (`recompute_candidate_tree`) + correctifs A0.2 #7/#8. Généralisé par G-SHA-BOUND-PROOF à canary + logs de gate. Pas de nouveau schéma de hash.

## 7. Machine à états

États `RunRuntime` inchangés (`ready → building → needs_approval → accepted → promoted`, + `blocked`/`paused`). C-8 ajoute des checks aux transitions (`promotion_ready` mécanique ; `approve()` reste humain ; `promote()` exige les deux), pas de nouveaux états.

## 8. Correction-loop maximum  *(finding #4)*

**`max_corrections = 1`** (`runtime.py:593`, `plan.json`), valeur réelle du runtime — pas 2. Une tentative + une correction ciblée, puis arrêt → Boss.

## 9. Boss approval points  *(D6)*

Exactement 3 : (a) GO initial (référence hashée) ; (b) escalade de tier en cours (déterministe par chemin, D2) ; (c) promotion. Entre les gates : autonomie autorisée pour runs synthétiques/normal ; promotion et effets externes restent Boss-controlled. Le contrôleur ne fabrique jamais l'approbation humaine (§2.2, SPEC §13).

## 10. Failure & recovery

Tout gate `ok=false` → `blocked` (comme le chemin « no proof imported » de `CodexEvidenceReviewer` aujourd'hui), jamais de downgrade silencieux. Récupération = nouveau run (nouveau `candidate_tree`). Désaccord reviewer critique → BLOCK + notification Boss, aucun tie-break auto (D4).

## 11. Immutable candidate / tag / rollback

Réutilise le promote/rollback atomique de `promotion.py` (`a0_toy_mission_e2e.py`, 7/7) + tags annotés pointant un commit exact. Candidats superseded archivés/révoqués, jamais auto-supprimés (D7).

## 12. Compatibilité SOURCE-FRESH

Non conçu/démarré ici. Après C8-A→C8-C, SOURCE-FRESH est lançable via le contrôleur ; son **premier** run = `critical` + `canary_required` (→ `normal` après 3 runs propres, D-décision). Aucun chemin de code SOURCE-FRESH-spécifique anticipé.

## 13. Cost policy

Zéro coût métré incrémental : `GLMReviewer` réutilise l'abonnement Z.AI de `GLMBuilder` (forfait) ; `CodexCLIReviewer` l'abonnement Codex ; la 2ᵉ FAMILLE critique = `ClaudeCLIReviewer` (`anthropic`, cible). Le GPT-formel ne peut PAS la fournir — sa famille est `openai`, identique à Codex — il sert donc de reviewer `normal` ou d'avis supplémentaire, jamais de 2ᵉ famille critical. Aucun nouveau provider métré.

## 14. No OS-security boundary recreation

Inchangé A0/A0.2 : les gates préviennent erreurs, bypasses connus, faux PASS, mutation de candidat, promotion non autorisée, fuites de secret évidentes — pas une frontière contre un process hostile ayant les mêmes droits OS. Isolation forte = `container`/`vm` `ExecutionBackend`, non implémenté (`PREFLIGHT_UNAVAILABLE` fail-closed), inchangé.
