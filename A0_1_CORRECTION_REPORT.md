# RAPPORT — A0.1 « CORRECTION POST-AUDIT » (M1-A)

Run card : run `A0.1` · Repo : `~/joao-orchestrator` · Branche : `feat/joao-a0-integrite`
(suite directe de `1b246fe`, tip A0 d'origine)
Source : contre-audit externe GPT, verdict **NO-GO**, 6 findings concrets sur la livraison
A0/RI-1..RI-8. Portée stricte : ces 6 points, rien d'autre (pas de nouvelle feature, pas de
mémoire/cascade, pas de CV-bot).

Prérequis vérifié avant démarrage : worktree propre à l'exception de `.claude/settings.local.json`
(config locale de l'outil, non trackée, pré-existante, hors produit — la même exception déjà
déclarée dans `A0_INTEGRITE_REPORT.md`).

## LES 6 CORRECTIONS

Chaque correction est qualifiée au format CLAIM / MÉCANISME DE PREUVE / PORTÉE EXACTE / LIMITES
(`SYSTEM_CONSTITUTION_V4.md` §D-043) — aucun mot absolu ("inattaquable", "sécurisé", etc.) n'est
utilisé sans cette qualification complète.

### A0-1 — le reviewer doit tourner sur la copie candidate immuable, jamais sur le workspace mutable

CLAIM : pour les stages `"build"` et `"final"`, `CodexCLIReviewer` (et tout reviewer avec
`review_stage`) est pointé exclusivement sur `run["candidate"]["readonly_copy"]`.

MÉCANISME DE PREUVE : `CodexCLIReviewer.review_stage` (`bubble/runtime.py`) refuse la review si
aucun candidat n'est gelé pour ces deux stages (`"A0-1: no frozen candidate readonly_copy
available"`), recalcule `candidate_tree` via `recompute_candidate_tree` immédiatement AVANT
d'invoquer Codex et à nouveau immédiatement APRÈS (`recomputed_tree_before_review`,
`recomputed_tree_after_review` dans l'évidence), et refuse sur toute divergence. Le chemin
inspecté (`reviewed_path`) et le commit candidat (`candidate_commit`) sont journalisés dans
`{stage}-review-evidence.json`. Seul le stage `"plan"` (avant tout candidat) reste sur
`run["workspace"]` — c'est correct par construction, pas une régression.

PORTÉE EXACTE : couvre `CodexCLIReviewer` (adaptateur réel) pour les stages `build`/`final`.
Démontré par `test_a01_reviewer_runs_on_candidate_copy_never_the_mutable_workspace` : un faux
exécutable Codex journalise le chemin exact qu'il reçoit à chaque invocation ; le workspace est
délibérément fait diverger de la candidate entre le freeze et la review finale (via un
`TestRunnerAdapter` qui mute `module.py` dans le workspace pendant l'étape « tests ») ; les 3
chemins journalisés sont `workspace` (plan, correct), `readonly_copy` (build), `readonly_copy`
(final) — jamais `workspace` pour build/final malgré la divergence réelle et vérifiée entre les
deux répertoires à ce moment précis.

LIMITES : un reviewer legacy sans `review_stage` (ex. `AcceptReviewer` dans les tests, ou
`CodexEvidenceReviewer` qui lit une preuve importée) n'est pas concerné par ce mécanisme — il n'a
jamais accès au workspace ni à la candidate directement, il consomme un JSON déjà produit ailleurs.
Codex CLI réel non exercé (quota — cf. `A0_INTEGRITE_REPORT.md`, non revérifié dans ce run) ; la
démonstration utilise un faux exécutable qui lit réellement les fichiers reçus.

### A0-2 — parseur JSON strict pour la réponse du reviewer

CLAIM : `json.loads(raw.strip())` est l'unique mécanisme de parsing ; tout octet avant/après
l'objet JSON rend la réponse entière non-parseable. L'ensemble de clés est strict (clés en trop
rejetées), `findings` doit être une liste de chaînes, `reviewer.provider`/`reviewer.model` du
corps de la réponse sont TOUJOURS écrasés par les valeurs calculées par le contrôleur.
ACCEPT exige simultanément `returncode==0 AND schema_valid AND candidate_tree==expected AND
verdict=="ACCEPT"`.

MÉCANISME DE PREUVE : `bubble/reviewer_contract.py` — l'ancien fallback par comptage d'accolades
(qui extrayait le dernier `{...}` de niveau supérieur dans un texte libre) est supprimé
entièrement, pas contourné. `validate_reviewer_verdict` prend un paramètre `returncode` explicite
et ne calcule `ok=True` que si `returncode==0` en plus de la correspondance de hash et du verdict ;
`proof.reviewer` est construit à partir des arguments `provider`/`model` du contrôleur, jamais du
corps parsé.

PORTÉE EXACTE : 3 tests dédiés. `test_a02_prose_around_json_is_refused` réimplémente inline
l'ancien fallback pour prouver qu'il aurait accepté un ACCEPT noyé dans du texte libre, puis prouve
que `parse_reviewer_response` retourne `None` sur le même texte. `test_a02_nonzero_returncode_refused_despite_valid_accept_payload`
prouve qu'un payload ACCEPT bien formé avec `returncode=1` est bloqué. `test_a02_forged_reviewer_metadata_is_overwritten`
prouve qu'un `reviewer.provider="FORGED-PROVIDER"` dans le corps n'apparaît jamais dans
`proof.reviewer` — remplacé silencieusement par l'identité du contrôleur.

LIMITES : la vérification de schéma reste superficielle (types + clés), pas un schéma JSON formel
(pas de bibliothèque de validation ajoutée — hors périmètre de cette carte). `CodexEvidenceReviewer`
(qui lit une preuve importée depuis un fichier, sans process associé) utilise `returncode=0` par
défaut faute de process réel à interroger — documenté, pas une garantie de contrôle d'exécution
pour ce chemin-là spécifiquement.

### A0-3 — la baseline exceptionnelle est réellement gelée

CLAIM : une `declared_baseline` produit un vrai objet git (tree/commit), jamais seulement une
étiquette texte ; le diff du builder est isolable de la dérive pré-existante.

MÉCANISME DE PREUVE : `candidate.freeze_baseline` (nouveau) — `git add -A` + `write-tree` +
`commit-tree` (parent = HEAD réel) + `update-ref refs/joao/baselines/<run_id>`, avec vérification
`git cat-file -e <commit>^{commit}` avant tout retour ; l'event `baseline_frozen` n'est émis QUE si
cette vérification réussit (sinon `RuntimeStateError` en `start()`, jamais un event mensonger).
`_execute` calcule ensuite `builder-only-diff.patch` = `capture_full_diff(workspace,
base_ref=baseline_commit)` — isolant ce que le builder a réellement ajouté — en plus, inchangé, du
`final-diff.patch` habituel (toujours vs HEAD, donc toujours la vue complète y compris la dérive).

PORTÉE EXACTE : `test_a03_baseline_genuinely_frozen_and_distinguishable_from_builder_work` — une
modification pré-existante non committée est déclarée en baseline ; le test vérifie l'existence
réelle de l'objet commit, puis que la ligne ajoutée par le builder (`+BUILDER_ADDED = True`)
apparaît dans `builder-only-diff.patch` alors que la ligne pré-existante (`+VALUE = 1  #
pre-existing dirty edit`) n'y apparaît QUE dans `baseline-drift.patch` — jamais comme une addition
attribuée au builder dans le diff isolé (elle peut apparaître comme contexte non préfixé, ce qui
est le comportement normal et correct d'un diff unifié).

LIMITES : le `final-diff.patch` "vue complète" reste, comme avant, un diff vs HEAD qui mélange
baseline et travail du builder — c'est `builder-only-diff.patch` (nouveau) et
`baseline-drift.patch` (nouveau) qui apportent la distinction, en évidence SUPPLÉMENTAIRE, pas en
remplacement du mécanisme RI-2 existant (délibéré : ne pas toucher au gate RI-2 déjà testé). Le
builder continue de travailler directement dans `workspace` (pas dans une copie dérivée séparée de
la baseline gelée) — la carte demandait « build dans une copie propre dérivée de cette baseline » ;
la mise en œuvre retenue isole la PREUVE (diff) plutôt que l'ESPACE de travail du builder, un choix
plus étroit et documenté ici plutôt que silencieusement présenté comme équivalent.

### A0-4 — fichiers gitignorés dans des chemins sensibles

CLAIM : inventoriés via `git ls-files --others --ignored --exclude-standard`, refusés s'ils
correspondent à un motif « sensible » documenté ; ne survivent pas à la promotion.

MÉCANISME DE PREUVE : `change_capture.ignored_files_inventory` (nouveau) exécute exactement cette
commande. `policy/paths.py` ajoute `SENSITIVE_IGNORED_PATTERNS` (politique documentée en commentaire :
`*.secret`, `*.pem`, `*.key`, `*.env`/`.env*`, `id_rsa*`, `id_ed25519*`, `credentials*`,
`secrets.{json,yaml,yml}`, etc. — motifs de NOM de fichier, pas d'inspection de contenu) et
`detect_sensitive_ignored_files`. `_execute` appelle les deux après le build et bloque
(`sensitive_ignored_file_detected`) avant tout gel de candidat si un fichier correspond.

PORTÉE EXACTE : `test_a04_gitignored_sensitive_file_detected_refused_and_does_not_survive_promotion` —
un `.gitignore` avec `*.secret` plus un `payload.secret` planté par le builder est détecté et
bloque le run (`ignored-files-evidence.json` le liste, event émis) ; démontré inline que `git
status --porcelain` ne le voit jamais. Deuxième moitié du même test : même en accordant qu'un
fichier ignoré traîne dans le workspace AU MOMENT de la promotion (scénario différent — pas ce
run-ci), la promotion (A0-6, worktree stérile) ne le fait jamais apparaître dans le résultat promu ;
démontré inline qu'un `git reset --hard` (mécanisme pré-A0.1) ne l'aurait pas supprimé.

LIMITES : politique par NOM de fichier uniquement — un secret dans un fichier au nom anodin
(`notes.txt` contenant une clé API) n'est pas détecté par ce mécanisme. Liste de motifs volontairement
resserrée (documentée dans `policy/paths.py`), pas exhaustive ; élargir la liste est un changement
de politique, pas un changement de mécanisme.

### A0-5 — complétude RI-6/RI-7

CLAIM (3 volets) : (1) un run `critical=True` refuse plutôt que de retomber silencieusement sur
`env-only` si aucune sandbox noyau réelle n'est disponible ; (2) limites OS appliquées (CPU, mémoire,
fichiers ouverts, nombre de process) ; (3) `network_capability`/`read_only`/`allowed_write_paths`
résolus depuis l'enregistrement de mission gelé au démarrage, jamais depuis un argument d'appel
vivant ni un re-lecture du profil statique.

MÉCANISME DE PREUVE :
- (1) `sandbox.run_sandboxed(..., protected=True)` retourne immédiatement `ok=False,
  enforcement="refused-no-kernel-sandbox"` si `sandbox-exec` est absent — plus de fallback silencieux.
  `RunRuntime` passe `protected=bool(run.get("critical"))` à `LocalTestRunner.run(...)`.
- (2) `_resource_limits_preexec` applique `RLIMIT_CPU`/`RLIMIT_NOFILE`/`RLIMIT_NPROC` (ce dernier
  calculé comme `count_process_actuel_du_UID + budget`, jamais un nombre absolu — un hôte partagé a
  déjà des centaines de process). `RLIMIT_AS` est AUSSI posé en défense en profondeur mais n'est PAS
  le mécanisme réellement invoqué : vérifié empiriquement sur cet hôte macOS que
  `setrlimit(RLIMIT_AS, 200 Mo)` laisse passer une allocation de 20 Go sans broncher (limitation
  documentée du noyau XNU, pas un bug de ce code). Le mécanisme réellement démontré est un
  **watchdog RSS côté parent** (thread qui interroge `ps -o rss=` sur tout l'arbre suivi et tue au
  dépassement) — portable, vérifié fonctionnel sur macOS.
- (3) `RunRuntime._resolve_mission_scope` relit `checkpoints/0000-pending.json` (écrit une seule
  fois par `start()`, jamais réécrit ensuite) et compare `network_capability`/`read_only`/
  `profile.allowed_write_paths` avec ce que le `run` dict vivant prétend actuellement ; toute
  divergence bloque le run (`mission_scope_tampered_or_unresolvable`) au lieu de faire confiance à
  la valeur vivante.

PORTÉE EXACTE (4 tests) :
- `test_a05_process_bomb_is_bounded_by_nproc_ceiling` : avec `max_new_processes=8`, un fork-bomb
  (plafond de sécurité interne à 300 pour ne jamais réellement saturer la machine) s'arrête vers
  ~20 forks (`OSError` du noyau) contre 300 sans limite.
- `test_a05_memory_exhaustion_is_caught_by_rss_watchdog` : avec `memory_bytes=200 Mo`, un script qui
  tente d'allouer 40 Go est tué (`memory_limit_exceeded=True`) avant d'afficher un seul octet alloué ;
  sans limite, le même script réussit intégralement (preuve que le kill n'est pas un hasard).
- `test_a05_double_fork_reparenting_escape_is_caught` : un script qui fork, dont le parent immédiat
  sort (orphelinant le petit-fils, reparenté au PID 1 — le ppid ne remonte alors plus jamais à la
  racine suivie) est tout de même tué, via le balayage par jeton d'environnement
  (`JOAO_SANDBOX_TOKEN`, hérité à travers fork()), là où un balayage par ppid seul (mécanisme RI-6
  pré-A0.1) ne l'aurait structurellement pas pu voir.
- `test_a05_forged_tampered_signed_scope_is_caught` : `run.json` est édité directement sur disque
  après `start()` pour passer `network_capability` à `True` (hors de toute API publique) ; `run_once()`
  bloque avec le mismatch nommé explicitement dans l'évidence.

LIMITES : (1) `protected=True` n'est câblé que pour `LocalTestRunner` (les tests de la mission) —
PAS pour `GLMBuilder`/`SandboxBuilder` (le build lui-même), car `BuilderAdapter.build()` ne reçoit
pas le `run` complet (signature existante, changer l'interface aurait dépassé le périmètre
chirurgical de cette carte). (2) le balayage par jeton est contourné par un descendant qui `exec`
un nouveau process avec un environnement explicitement vidé — documenté, pas silencieusement
ignoré. (3) le mécanisme de « signature » de la mission est une évidence content-addressed
append-only (le checkpoint 0000), pas une signature cryptographique au sens strict — un attaquant
qui modifierait `run.json` ET `checkpoints/0000-pending.json` de façon cohérente ne serait pas
détecté par ce mécanisme seul ; c'est une défense contre une dérive/un bug/une édition partielle,
pas contre un accès disque total et délibéré aux deux fichiers.

### A0-6 — promotion atomique vérifiée

CLAIM : le code de retour du checkout est vérifié (plus de `check=False` ignoré) ; en cas d'échec,
rollback CAS immédiat ; le manifeste de succès n'est écrit qu'après vérification de l'arbre ET de
l'état du worktree.

MÉCANISME DE PREUVE : `bubble/promotion.py` réécrit — après le CAS sur la ref de branche, `git
worktree add --detach <sterile> <candidate_commit>` (jamais `git reset --hard` sur le workspace
vivant) ; `checkout.returncode` est vérifié explicitement ; `_verify_sterile_worktree` recalcule le
hash d'arbre (`recompute_candidate_tree`), vérifie `git status --porcelain` vide, et l'absence de
fichier ignoré sensible — toute anomalie déclenche `_cas_rollback_ref` (compare-and-swap immédiat
vers `previous_tip`) AVANT que `promotion-manifest.json` ne soit jamais écrit.

PORTÉE EXACTE : `test_a06_worktree_sync_failure_after_ref_cas_never_produces_false_success` — le
`git worktree add` est intercepté (monkeypatch) pour échouer APRÈS que le CAS de la ref de branche
ait déjà réussi ; le test vérifie que la branche est revenue à son tip d'avant-promotion et
qu'aucun `promotion-manifest.json` n'a été écrit — donc aucun faux succès.

LIMITES : la promotion ne touche plus JAMAIS le `workspace` vivant (choix délibéré, imposé par la
carte : « never reset --hard on the current worktree ») — effet de bord documenté : après une
promotion, le `workspace` d'origine n'est plus automatiquement synchronisé avec sa propre branche
(le ref a bougé, les fichiers checkés out non) ; le résultat vérifié vit dans le worktree stérile
retourné par `manifest["promoted_worktree"]`, pas dans `workspace`. `scripts/a0_toy_mission_e2e.py`
a été mis à jour en conséquence (les assertions post-promotion portent sur le worktree stérile).

## E2E — cycle complet avec un reviewer qui lit réellement les fichiers

`scripts/a0_toy_mission_e2e.py`, `InspectingReviewer` remplace l'ancien `ToyReviewer` (qui se
contentait de renvoyer le hash reçu sans jamais lire un fichier). `InspectingReviewer` lit
`greeting.py` depuis le chemin exact reçu (`readonly_copy`), échoue si le contenu ne satisfait pas
le critère de la mission, ET recalcule indépendamment le hash d'arbre — échouant s'il diverge.
Exécuté en direct, trace dans `evidence/A0_TOY_MISSION/trace.json` :

```
candidate_tree                 = c50fd027293ab89709db2dd216c638037d805b54  (cette exécution)
build_review.candidate_tree    == candidate_tree   (True)
final_review.candidate_tree    == candidate_tree   (True)
promoted worktree (STÉRILE, jamais `workspace`) reflète le contenu promu
promoted_commit^{tree}         == candidate_tree      (True)
rollback EXÉCUTÉ et VÉRIFIÉ (manifest["verified"], rollback_result["verified"])
```

Codex CLI réel non exercé dans ce cycle (contrainte de quota inchangée depuis le rapport A0
original) — le remplacement est un faux reviewer qui lit réellement les fichiers, pas un simple
echo, conformément à EVIDENCE_REQUIRED.

## TESTS — suite complète, comparaison par `git stash`

11 nouveaux tests d'attaque (`tests/test_a0_1_corrections.py`), un par sous-scénario listé
ci-dessus (A0-1 ×1, A0-2 ×3, A0-3 ×1, A0-4 ×1, A0-5 ×4, A0-6 ×1), chacun démontré rouge → vert dans
le corps du test (mécanisme naïf/pré-A0.1 réimplémenté ou contrasté inline, puis comportement
corrigé prouvé). Les 16 tests A0 existants (`test_a0_attack_tests.py`, 8 attaques) et les 6 tests de
`test_bubble_runtime.py` passent tous sans modification de comportement (une seule signature de
fixture ajustée : `_TamperingTestRunner.run(...)` accepte désormais le nouveau paramètre `protected`).

Suite complète après ce run : **304 passed, 3 failed** (307 tests, +11 vs les 296 de la baseline
A0 d'origine).

Les 3 échecs sont vérifiés PRÉ-EXISTANTS par la même technique que la livraison A0 d'origine
(`git stash` du travail A0.1, ré-exécution complète sur le tip pré-A0.1, comparaison) :
- Baseline (stash appliqué, tip `1b246fe`) : **294 passed, 2 failed** —
  `test_b28_select_lessons.py::test_visual_docx_surfaces_authority_chain` et
  `test_m0_traceability.py::test_real_repo_produces_zero_unmapped_with_cv_bot_specs`.
- Une exécution ciblée séparée sur la même baseline montre que le troisième
  (`test_b28_import_ledger.py::test_import_is_append_only_idempotent`) échoue ou passe selon
  l'ordre/état de `memory/lessons.jsonl` — confirmé DÉJÀ documenté comme tel dans
  `A0_INTEGRITE_REPORT.md` (« dérive du contenu du cerveau réel, pas du code »), reconfirmé
  indépendamment ici : ces 2-3 tests forment un groupe flaky lié au contenu partagé et mutable de
  `memory/lessons.jsonl`, pas au code de ce périmètre. Après A0.1, exactement les 3 mêmes noms de
  test apparaissent dans ce groupe, jamais un test hors de ce groupe.
- `memory/lessons.jsonl` est modifié comme effet de bord de l'exécution des tests locaux (le
  mécanisme retro/ledger-sync existant, hors périmètre de cette carte, écrit dans le `memory/` réel
  du dépôt faute de `JOAO_MEMORY_DIR` positionné) ; ce fichier a été restauré (`git checkout --`)
  avant chaque commit de ce run pour ne jamais committer une pollution de test.

## NON_VÉRIFIÉ / LIMITES GÉNÉRALES

- Les 6 findings sont chacun couverts par au moins un test rouge→vert dans ce périmètre précis —
  pas au-delà des scénarios décrits ci-dessus.
- `RLIMIT_AS` reste posé (défense en profondeur, gratuite) mais son inefficacité empirique sur
  macOS/Darwin pour les allocations `mmap` est documentée sans détour dans `sandbox.py` — le
  mécanisme réellement vérifié pour A0-5 (mémoire) est le watchdog RSS, pas le rlimit.
- Le balayage par jeton d'environnement (`JOAO_SANDBOX_TOKEN`) suppose que le processus échappé
  hérite de son environnement via `fork()`/`exec()` sans le vider explicitement ; un `execve` avec
  un environnement neuf y échapperait (non testé, documenté).
- `protected=True` (A0-5, volet 1) n'est câblé que pour le TestRunner, pas pour le Builder — limite
  d'interface documentée ci-dessus (A0-5).
- La « signature » de mission (A0-5, volet 3) est une évidence content-addressed append-only
  (checkpoint 0000), pas une signature cryptographique — portée exacte documentée ci-dessus.
- A0-3 : le builder continue de travailler dans le `workspace` live, pas dans une copie dérivée
  isolée de la baseline gelée — la distinction obtenue porte sur la PREUVE (diff), pas sur
  l'ESPACE de travail ; écart assumé et documenté vs. la lettre de la carte.
- A0-4 : politique de détection par nom de fichier uniquement, pas par contenu.
- Codex CLI réel non exercé dans ce run (quota, hérité du rapport A0 original, non revérifié) — la
  correction A0-1 et la démonstration E2E utilisent un faux exécutable/reviewer qui lit
  effectivement les fichiers reçus, pas un simple echo.
- GLM side-check réel non exécuté (`ZAI_API_KEY` absent), inchangé depuis le rapport A0 original —
  hors périmètre de cette carte de toute façon (RI-6, pas les 6 findings A0.1).
- Aucun merge, aucun push, aucun tag effectué. Branche locale `feat/joao-a0-integrite` — prête pour
  un nouveau contre-audit externe sur le SHA final de ce run.
