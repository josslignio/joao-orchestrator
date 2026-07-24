# RAPPORT — A0 « INTÉGRITÉ CRITIQUE » (M1-A)

Run card : `~/Claude-HQ/JOAO_RUN_CARD_A0_INTEGRITE.md` · Repo : `~/joao-orchestrator`
Branche : `feat/joao-a0-integrite` (créée depuis `docs/m0-authority-v4` tip `02653a6`)
5 commits : `1bdfc48`, `b1f31de`, `93d81b0`, `ff55b94` (+ ce rapport)
Prérequis vérifié avant démarrage : `ACTIVATION_RECORD_V4.json` → `boss_go.given: true`,
`runtime_authority: true`, bundle V4 signé (DEC-009, 2026-07-18).

## BASELINE (RI-1 auto-application)

Worktree `~/joao-orchestrator` propre au démarrage à l'exception d'un seul fichier :
`.claude/settings.local.json` (config locale de l'outil, non trackée, pré-existante,
hors produit). Déclarée ici comme l'exception "baseline temporaire gelée" que RI-1
lui-même exige — non touchée par ce run.

## DELIVERABLES — état

1. **Baseline propre obligatoire (RI-1)** — `RunRuntime.start()` refuse tout worktree
   sale ; seule exception = `declared_baseline="<raison>"` explicite, enregistrée en
   évidence (`baseline_frozen_and_declared` event + `baseline_paths_at_start` dans
   `run.json`). `src/joao_orchestrator/bubble/runtime.py`.
2. **Capture complète des changements (RI-2)** — `bubble/change_capture.py` :
   `git add -N` (intent-to-add) sur les fichiers untracked + `git diff HEAD --binary -M`
   (jamais `git diff` seul, qui rate staged-vs-HEAD ET untracked), cross-validé contre
   un `git status` indépendant (`completeness_ok`, `missing_from_diff`). Démontré : un
   `git diff` nu rate à la fois un fichier untracked et une modification stagée —
   preuve inline dans les attack tests 1 et 2.
3. **Candidat immuable (RI-3)** — `bubble/candidate.py` : après build, `git add -A` +
   `write-tree` + `commit-tree` (parent = HEAD, jamais attaché à une branche) +
   `update-ref refs/joao/candidates/<run>-<n>` ; copie lecture-seule via
   `git worktree add --detach` + chmod récursif. `candidate_tree` (hash d'arbre git)
   recalculé (`add -A` + `write-tree` frais) et re-vérifié à deux reprises : juste après
   les tests, et une dernière fois dans `approve()` — donc même une falsification très
   tardive (après review, avant approbation humaine) est couverte.
4. **Reviewer lié au hash (RI-4)** — `bubble/reviewer_contract.py` : contrat JSON strict
   `{candidate_tree, verdict, findings, reviewer{provider,model}}`. Un texte libre ou un
   `candidate_tree` absent/différent est rejeté. Appliqué à deux niveaux : l'adaptateur
   (`CodexCLIReviewer` exige désormais ce JSON dans son prompt) ET le contrôleur
   (`_review_gate`, stage `"final"`) — donc un adaptateur bugué/malveillant qui renverrait
   `ok=True` pour le mauvais hash est quand même bloqué par le contrôleur.
5. **Évidence calculée par le contrôleur (RI-5)** — `_execute` ignore/écrase tout hash
   auto-déclaré par le builder (`output_sha256`, `sha256`, `candidate_tree` filtrés hors de
   `builder-evidence.json`) et recalcule lui-même `controller_verified_output_sha256`.
6. **Sandbox subprocess (RI-6)** — `bubble/sandbox.py` : HOME temporaire, env minimal
   (`policy.environment.build_task_env`), confinement fichier + réseau coupé par défaut
   via macOS Seatbelt (`sandbox-exec`) quand disponible (fallback honnête `enforcement:
   "env-only"` sinon — jamais présenté comme une garantie équivalente), timeout, et kill
   de tout l'arbre de descendants par `ppid` (survit à un `setsid()` d'évasion). Branché
   sur `LocalTestRunner` (réseau coupé sauf `network_capability` déclaré par la mission) et
   `GLMBuilder` (réseau autorisé — c'est son rôle — mais allowlist de credentials explicite
   au lieu d'un héritage d'environnement complet). `CodexCLIReviewer` reste hors du
   changement HOME/Seatbelt (limite documentée ci-dessous).
7. **Promotion atomique + rollback (deliverable 7)** — `bubble/promotion.py` :
   `git update-ref <ref> <new> <old>` en compare-and-swap pour le fast-forward ET pour le
   rollback ; tag annoté + manifest JSON avec la commande de rollback exacte. Prouvé en
   direct (voir cycle E2E ci-dessous) : promotion, refus sur drift concurrent, rollback
   EXÉCUTÉ, refus d'un second rollback.
8. **Statuts V4** — inchangés dans ce run (hors périmètre RI ; le state machine V4
   existant — PENDING→…→ACCEPTED/STOPPED — est celui déjà en place, RI-3/RI-4 s'y greffent
   sans ajouter de nouveaux statuts).

## ATTACK_TESTS — les 8, rouge → vert

`tests/test_a0_attack_tests.py`, 8 tests (+1 sous-cas réseau). Chacun démontre en ligne
le comportement naïf/pré-fix AVANT d'affirmer la détection :

| # | Attaque | Rouge (démontré inline) | Vert |
|---|---|---|---|
| 1 | Untracked hors preuve | `git diff --binary` nu ne voit pas le fichier untracked | `capture_full_diff` le voit (`SECRET_PAYLOAD` présent dans `final-diff.patch`) |
| 2 | Staged modifié | `git diff` (sans HEAD) est vide alors que le contenu diffère de HEAD | `git diff HEAD` (via capture_full_diff) le voit |
| 3 | Candidat modifié après tests | Un `TestRunnerAdapter` factice altère la copie gelée pendant "les tests" | `run_once` → `blocked`, event `candidate_integrity_violation` |
| 4 | Reviewer répond pour un autre SHA | Un reviewer stub renvoie `ok=True` pour `candidate_tree="0"*40` | Le contrôleur écrase en `block`, `adapter_claimed_ok: True` prouve l'override |
| 5 | Builder fabrique son SHA256SUMS | Builder renvoie `output_sha256="0"*64` | `builder-evidence.json` ne contient jamais ce champ ; `controller_verified_output_sha256` = vrai hash |
| 6 | Lecture secret planté | `LocalTestRunner(sandboxed=False)` lit le secret en clair | `sandboxed=True` → `PermissionError`, secret absent du stdout |
| 7 | Réseau sans capability | (signal distinctif : `PermissionError`/`Operation not permitted` du syscall, pas juste un échec réseau ambiant) | `network=False` → refus syscall ; `network=True` → non refusé |
| 8 | Subprocess zombie | `sandboxed=False` : l'enfant évadé (`setsid`) survit au timeout | `sandboxed=True` : mort confirmée par balayage `ps` (descendant via `ppid`, pas `pgid`) |

## EVIDENCE_REQUIRED — cycle complet, même hash de bout en bout

`scripts/a0_toy_mission_e2e.py`, exécuté en direct, trace dans
`evidence/A0_TOY_MISSION/trace.json` :

```
candidate_tree            = f7b8ca3c4486499203e1482a9964c8ce5be63e90
build_review.candidate_tree  == candidate_tree   (True)
final_review.candidate_tree  == candidate_tree   (True)
approve() re-vérifie le tree hash une 3e fois avant ACCEPTED
promoted_commit^{tree}     == candidate_tree      (True)
rollback EXÉCUTÉ (pas seulement écrit) → branche + worktree restaurés à la base
```

Cycle : `start → run_once(build→candidat→review build→tests→re-check→review final)
→ approve → promote → rollback`, un seul hash tracé sans rupture.

## TESTS

293/296 tests passent. Les 3 échecs sont **pré-existants**, confirmés identiques sur
`docs/m0-authority-v4` avant ce run (vérifié par `git stash` + exécution ciblée) et hors
du périmètre `runtime_integrity` :
- `test_b28_import_ledger.py::test_import_is_append_only_idempotent` et
  `test_b28_select_lessons.py::test_visual_docx_surfaces_authority_chain` — dérive de
  `memory/lessons.jsonl` (contenu du "cerveau" réel, pas du code).
- `test_m0_traceability.py::test_real_repo_produces_zero_unmapped_with_cv_bot_specs` —
  D-044 en `UNMAPPED_PENDING_SPEC` (documentaire ; couvert par la carte CV-SEC-V4 en cours,
  autre repo).

4 régressions réelles ont été introduites puis corrigées pendant ce run (doubles de test
utilisant l'ancien contrat reviewer `reviewed_diff_sha256` au lieu de `candidate_tree`,
et un test s'appuyant sur l'ancien comportement fail-open de `CodexEvidenceReviewer`) —
détail dans les messages de commit `1bdfc48`.

## AUTO-REVIEW ADVERSARIALE (async/races)

Revue indépendante (agent frais, focus concurrence) sur `candidate.py`, `promotion.py`,
`sandbox.py`, `change_capture.py`, `_execute`. Un vrai bug trouvé et corrigé (`ff55b94`) :
`freeze_candidate`/`recompute_candidate_tree` pouvaient lever une exception brute non
interceptée et échouer le run en `BUILDING` sans issue — désormais fail-closed vers
`BLOCKED` partout, cohérent avec le principe déjà existant du code (P1-A, builder).

Constats non corrigés dans ce run (documentés, pas des bugs de ce périmètre) :
- Race pré-existante (antérieure à ce run) dans les singletons paresseux
  `_injector()`/`_retro()`/`_ledger_sync_mod()` : deux threads lançant des missions
  proches peuvent transitoirement voir l'injection de mémoire indisponible. Dégradation
  honnête (event loggé), pas de corruption. Hors périmètre `runtime_integrity`.
- `FileLock` (existant) ne protège que le builder d'un run_id donné, pas
  `freeze_candidate`/`recompute_candidate_tree`/`release_candidate`, ni deux run_id
  différents pointant sur le même workspace. Seul `api.py`'s `_active_workspaces`
  couvre partiellement ce cas (`launch`/`retry`, pas `approve`).
- `bubble/promotion.py` n'a aucun appelant dans `runtime.py`/`api.py` — c'est une étape
  opérateur volontairement séparée de `approve()` (comme un gate de déploiement distinct
  d'un gate de revue), démontrée uniquement par le script E2E et les tests à ce stade.

## GLM SIDE-CHECK

Non exécuté en direct dans cette session : `ZAI_API_KEY` absent de l'environnement
courant (le wrapper `~/.local/bin/joao-glm` authentifie par variable d'env, pas par
fichier `~/`-résident). Le contrat GLM builder (allowlist de credentials explicite,
réseau autorisé) a été vérifié par lecture de code, pas par un appel réel à l'API. À
refaire quand une clé sera disponible dans l'environnement d'exécution.

## NON_VÉRIFIÉ / LIMITES

- Les 8 attaques listées sont bloquées, portée = ces scénarios précis — pas au-delà.
- `sandbox-exec` (Seatbelt) est macOS-only et marqué déprécié par Apple sans remplacement
  public documenté ; sur toute autre plateforme (ou binaire absent), l'enforcement retombe
  sur `env-only` (convention, pas une garantie du noyau) — chaque résultat porte le champ
  `enforcement` pour ne jamais confondre les deux.
- Le blocage réseau Seatbelt a été vérifié par erreur syscall (`PermissionError`), pas par
  un test de connectivité réelle réussie/échouée (qui dépendrait de l'environnement réseau
  ambiant, potentiellement lui-même restreint par la sandbox externe de cette session).
- `promotion.py` n'est pas câblé automatiquement après `approve()` — c'est un choix
  délibéré (gate opérateur séparé), pas encore exercé par un vrai flux `api.py`/`joao ui`.
- GLM side-check réel non exécuté (clé absente) ; Codex reviewer réel non exercé
  (quota bloqué jusqu'au 2026-07-23 06:22 selon le dernier état connu en mémoire — non
  revérifié dans ce run, hors périmètre).
- La race pré-existante des singletons paresseux et le FileLock à portée étroite sont
  documentés, non corrigés (hors du risk_boundary `runtime_integrity` de cette carte).
- Aucun merge, aucun push effectué. Branche locale `feat/joao-a0-integrite` prête pour
  revue.
