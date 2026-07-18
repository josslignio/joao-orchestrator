# RAPPORT — BIG RUN « LA SUITE »
**JOÃO · cascade branchée au réel (ferme L9.3) + V2.2 Chat Era · fold-ins audit**
_repo `~/joao-orchestrator` · branche `feat/joao-bigrun-suite` (base CERVEAU `b95d5cd`) · 2026-07-18 · autonome · commits par phase_

Le Boss juge sur la démo, pas sur ce rapport. **Chaque gate ci-dessous est un test EXÉCUTÉ ou une démo LIVE**, pas une déclaration.
**Résultat : 4 phases livrées · 48 nouveaux tests · 267/267 du repo · 0 régression · B-28 ACTIVE (bloc RÈGLES ACTIVES dans chaque évidence).**

Commits : `9d8043a` (P1) · `6a977dd` (P2) · `40d6835` (P3) · `23c094d` (P4). HEAD = `23c094d`.

---

## PHASE 1 — LA CASCADE DEVIENT RÉELLE (ferme L9.3 du CERVEAU) · `9d8043a`
**Livré** : `providers/cascade_runtime.py` — `CascadeBuilder(BuilderAdapter)` branche `providers/cascade.py` aux VRAIS workers : tier (b)/(c) → subprocess `joao-glm` (même patron que `GLMBuilder`), tier (d) → `claude` CLI, reviewer Codex inchangé. Chaque candidat best-of-N build dans une passe git ISOLÉE (snapshot → run → capture → reset). Le bloc mémoire que `RunRuntime._inject('builder')` préfixe à la mission atteint CHAQUE candidat (le `builder-task.md` archivé de chaque candidat contient le bloc — preuve par worker, pas seulement le gagnant). Juge : tests objectifs d'abord, départage déterministe (tidiness), juge (LLM injectable) seulement sur égalité réelle. Critique/récidive/échec-de-gate forcent best-of-N (`start()` gagne `critical/recurrence/tags` → `run.json`). `MissionCost` + signal de coût réel (durées, tokens si rapportés) loggés ; économie vs direct-Claude archivée.

**Self-review adversariale (D-018, obligatoire)** : une revue dédiée a trouvé 5 défauts RÉELS du câblage subprocess, tous corrigés + testés :
- **P1-A** (worktree sali + run stranded si un worker lève) → `try/except` autour du routage + reset garanti + catch générique fail-closed dans `_execute` ; `real_glm_runner` attrape `OSError`.
- **P2-A** (WIP non suivi pré-existant wipé par `git clean`) → snapshot + restauration du WIP non suivi.
- **P2-B** (git non borné → wedge possible) → timeout sur tous les appels git, reset best-effort.
- **P2-C** (grand-processus orphelin au timeout) → `start_new_session` + `killpg` sur tout le groupe.
- **P3-A** (juge custom renvoyant un non-candidat → crash) → garde fail-closed BLOCK.

**GATE 1 — démo LIVE réelle** (`~/.local/share/joao/acceptance/bigrun-suite-gate1-20260718T130928Z`) :
- **DEMO A** (mission simple, chemin live complet `RunRuntime`) → tier **déterministe (a), coût 0**, `needs_approval`, **mémoire B-28 injectée pour les 3 rôles** (planner/builder/reviewer ; 5 fichiers `active-rules-*.md`).
- **DEMO B** (mission critique) → **best-of-3 joao-glm RÉEL** : 3 sorties `is_prime` archivées (angles performance/readability/edge-cases), **les 3 passent les tests objectifs**, gagnant `zai-coding-plan/glm-4.5-air` (angle edge-cases), durées réelles **89.55 / 17.2 / 25.85 s**, **coût proxy 4** (3×GLM + 1 juge de départage, les 3 étant à égalité) **vs 20 en direct-Claude → économie 16**. Injection par worker confirmée (chaque `builder-task.md` porte le bloc RÈGLES ACTIVES). `real_cost_signal = 0` car joao-glm n'a rapporté aucun token (honnête, non inventé — le signal réel est la durée).
- Fail-closed / escalade Claude / BLOCK : vérifiés par tests hermétiques (12 tests, `test_b29_cascade_wired.py`).

## PHASE 2 — V2.2 « CHAT ERA » · `6a977dd`
**BLOC A** `bubble/intent.py` — routeur déterministe `CHAT | MISSION_CODE | KICKOFF | AMBIGU`, override manuel (Chat/Mission/Auto), seam classifieur-LLM (seulement si l'heuristique hésite ; sinon **2 boutons**, jamais un run à l'aveugle). Les 2 ratés du 17/07 sont corrigés (18 tests).
**BLOC B** `bubble/chat.py` — cerveau chat streamé via CLIs : `claude -p` stream-json (streaming réel de tokens, épinglé Sonnet, **modèle réel toujours affiché**) + GLM `opencode` (0 forfait, coût réel). Subprocess borné (`start_new_session`+`killpg`). **B3 pièces jointes = extraction déterministe** : pypdf pour PDF (texte RÉEL, jamais estimé), lecture directe texte/code/csv, image déclarée non-lue honnêtement. **B4 anti-mensonge** : prompt système + note explicite « NON LISIBLE » pour qu'aucun contenu ne soit inventé (9 tests, dont extraction d'un VRAI PDF).
**Produit** `bubble/api.py` + `ui.html` — chat-first (adaptation 2.2a) : `/chat` SSE, `/chat/classify` (routage loggé), `/chat/attach`, `/chat/mission`, historique ; bulles, toggle de mode, boutons d'ambiguïté, panneau `.md`, capacités honnêtes (web ✗). Kickoff = intention routée (2.2b) ; missions du chat via la cascade Phase 1 + hard gate Phase 0 (2.2c, le CLI câble `CascadeBuilder`). 5 tests HTTP.

**GATE 2 — démos LIVE E2E** (`bigrun-suite-gate2-*`) :
- « Hello ça va ? » → **CHAT, ZÉRO run créé** (le raté du 22 min est mort), réponse en **3.3 s via `claude-sonnet-5`** (cerveau par défaut). _GLM en alternative marche mais démarre à froid ~108 s — limite honnête (L9), c'est pourquoi Claude est le défaut._
- question sur un **VRAI PDF joint** → réponse **fondée sur le texte pypdf-extrait « 4242 »**, traçable, via `claude-sonnet-5`.
- « crée is_prime.py avec tests » → **MISSION_CODE** (→ cascade).
- « le module de paiement » → **AMBIGU → 2 boutons**.

## PHASE 3 — PREUVE DE VIE GLOBALE + RÉTRO · `40d6835`
**3.1 Scénario complet filmé** (`bigrun-suite-gate3-*`) : chat (Claude Sonnet, « c'est quoi un nombre premier ? ») → « ok build-le » → **MISSION_CODE** → mission cascade (**best-of-3 GLM réel**) → tests → review → **ACCEPTED** → **livrable `is_prime.py` réel dans le panneau** → **coût total 4 (économie 16)** → mémoire injectée 3 rôles. Reviewer = accept-fixture (honnête : Codex quota-bloqué jusqu'au 23/07 ; le câblage du reviewer est inchangé).
**3.2 Rétro L7 via `memory/retro.py`** (`bigrun-suite-retro-*`) : diff spec→résultat, **3 leçons candidates SANS id** (le système attribue les ids — D-029), **détecteur de récidive actif** (les 3 sont NOUVELLES, aucune récidive), **`runs_until_perfect(joao) = 1`** (honnête : 1 passe de correction issue de la self-review adversariale — PAS un run 0-loop parfait ; rien d'auto-congratulant).

## PHASE 4 — FOLD-INS DE L'AUDIT (borné) · `23c094d`
- **B-37 sync ledger→cerveau** : `import_ledger.import_lessons()` extrait (paramétré, idempotent, append-only) ; `memory/ledger_sync.py` re-importe si `mtime` du ledger > dernier import ; `RunRuntime(ledger_sync=True)` l'exécute au lancement (événement `ledger_synced`), activé dans le CLI. **Prouvé LIVE** : le vrai ledger avait dérivé (nouveau défaut **D-043**) → le cerveau committé était périmé → resynchronisé (**L-047**, id attribué par le système). Fallback du sélecteur durci : les 6 LOIS (colonne vertébrale systémique) ne peuvent plus être évincées par un défaut sévérité-3 frais.
- **B-24 tiering Claude** : `CascadeBuilder`/`real_claude_runner` prennent un tier `claude_model` (sonnet build / haiku smoke, jamais Opus) ; une mission `smoke` prend haiku pour le tier Claude de dernier recours (test unitaire).
- **B-36 Phase 0 rétroactive** : 3 specs générées (`projects/{joao,job-cv-auto,weekly-trading-radar}/PROJECT_SPEC.md` + PROJECT_MEMORY), **NON signées** (`SIGNÉ ❌ EN ATTENTE`) — le Boss signe dans l'UI (jamais auto-signé, LOI 1). Reconnues non-signées par `Kickoff.spec_is_signed`.
- **GATE 4** : 3 specs ✅ · hook de sync testé (toucher le ledger → re-import) ✅ · tiering haiku unit-vérifié ✅.

---

## COÛT PAR PHASE (réel, quand mesurable)
| Phase | preuve | coût réel |
|-------|--------|-----------|
| P1 GATE 1 best-of-3 GLM | 3 appels GLM réels, durées 89.55/17.2/25.85 s | proxy 4 vs 20 direct-Claude → **−16** ; $ non instrumenté (L9) |
| P2 GATE 2 chat | greeting 3.3 s (Sonnet) ; PDF Q 5 s (Sonnet) ; extraction pypdf locale (0) | forfait Sonnet minime ; GLM 0 forfait |
| P3 scénario | 1 mission best-of-3 GLM réelle | proxy 4 vs 20 → **−16** |
| P4 fold-ins | déterministe (pas d'appel modèle) | 0 |

Tests : **267/267** (219 base + 48 neufs). 4 boucles/item jamais atteint (self-review = 1 passe de correction, sous le plafond 2).

## NON VÉRIFIÉ / LIMITES (L9 — l'absence de cette section invalide le rapport, D-035)
1. **Modèle de session = Opus 4.8**, pas Sonnet. La carte demandait Sonnet (doctrine « Opus jamais »), mais l'environnement a imposé `claude-opus-4-8[1m]` à la session ORCHESTRATRICE — non contrôlable depuis le run. Les cerveaux DÉLÉGUÉS respectent la doctrine : chat = Sonnet, build = GLM. Déviation honnêtement signalée.
2. **Reviewer Codex quota-bloqué** jusqu'au 2026-07-23 06:22 → GATE 1/3 ont utilisé un reviewer accept-fixture ÉTIQUETÉ pour montrer le flux complet ; le câblage du vrai `CodexCLIReviewer` est inchangé et fail-closed. Aucune review Codex réelle n'a tourné.
3. **Tier (d) Claude-CLI builder non exercé en LIVE** : câblé + unit-testé (fakes), mais jamais invoqué réellement (les démos GLM réussissent avant lui ; et par doctrine forfait). Le tiering haiku/sonnet est unit-vérifié, pas exercé live.
4. **`real_cost_signal` en tokens = 0** : joao-glm ne rapporte pas de compteur de tokens dans sa sortie → l'extracteur renvoie 0.0 (jamais inventé) ; le signal réel exploité est la durée. Coût en $ non instrumenté.
5. **GLM chat démarre à froid ~108 s** (opencode) → non « en secondes ». Claude (défaut) tient <10 s (3.3 s mesuré). GLM reste l'option 0-forfait, pas le défaut.
6. **P3-B/C/D (nits self-review) non corrigés** : inflation cosmétique de l'évidence quand le worktree a du WIP pré-existant (base≠HEAD) ; parsing de rename staged (injoignable, builders écrivent non-staged) ; démotion staged→unstaged du WIP pré-existant. Aucun biais inter-candidats ; documentés, non bloquants.
7. **B-16 (archiver `joss_orchestrator`) et B-25 (convention supplement-vN) DÉLIBÉRÉMENT reportés** : archiver le shim de compat risque les tests de frontière — à faire dans un changement borné dédié. Pas commencé.
8. **Recherche web chat = non branchée** (hors-scope carte) : annoncée honnêtement (`web ✗` dans les capacités), jamais simulée.
9. **Leçons candidates de ce run non gravées** (D-029) : les 3 candidates sont soumises au Boss, pas auto-append au cerveau committé.

## HORS-SCOPE (non démarré, conforme à la carte)
B-29.2+ (nuits spéculatives, cliquet, auto-training) · B-26 débat · V2.5 · B-34 recherche web · B-35 images.
