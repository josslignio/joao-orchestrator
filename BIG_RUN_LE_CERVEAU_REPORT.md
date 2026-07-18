# RAPPORT — BIG RUN « LE CERVEAU »
**JOÃO · B-28 mémoire active + Phase 0 produit + B-29.1 cascade/best-of-N**
_repo `~/joao-orchestrator` · branche `feat/joao-runtime-bubble-v0` · 2026-07-18 · autonome · commits par phase_

Le Boss juge sur la démo E2E, pas sur ce rapport. **Chaque gate ci-dessous est un test EXÉCUTÉ**, pas une déclaration.
Résultat global : **6/6 gates verts · 42 tests dédiés · 219/219 tests du repo · 0 régression**.

---

## GATE 1 — LE STOCK · commit `a01434f`
**Livré** : `memory/import_ledger.py` parse `DEFECTS_LEDGER.md` (D-001→D-042, toutes les tables) + les 6 LOIS de l'analyse systémique → `memory/lessons.jsonl`, une leçon impérative par ligne, ids **attribués par le système** (`L-001…`, jamais par le run — D-029), mapping `D-xxx` conservé dans `source_defect`, **append-only idempotent**. `RULE_OVERRIDES` transcrit le remède PROSE du ledger pour les tables sans colonne « leçon » (D-016/018/021/042).
**Preuve** : **46 leçons** (40 ledger + 6 LOIS ≥ 40). 5 tests : schéma complet/typé, ids système + mapping défaut, tags critiques du sélecteur (`async→D-018`, `docs→D-021/D-042`), append-only (re-run = 0 ajout).
**10 leçons relues à la main (règle source ↔ impératif généré)** :

| id | défaut | sév | impératif généré |
|----|--------|-----|------------------|
| L-001 | LOI-1 | 3 | AUTORITÉ D'ABORD : aucun générateur/UI/feature avant validation propriétaire sur rendu. Silence ≠ GO. |
| L-004 | LOI-4 | 3 | Mémoire injectée par la machine (B-28) : lessons.jsonl + sélecteur top-K dans CHAQUE prompt. |
| L-016 | D-010 | — | Toute UI livrée = actions câblées + test E2E de clic obligatoire dans la gate. |
| L-024 | D-018 | 2 | Toute logique asynchrone exige une self-review adversariale dédiée aux courses temporelles. |
| L-027 | D-021 | 2 | Un générateur dérive TOUJOURS du master d'autorité (duplicate-and-edit) ; jamais de rebuild en code. |
| L-040 | D-034 | 3 | MÉTA-DÉFAUT : aucune gate ne comparait le rendu au visuel approuvé → sur-déclaration. |
| L-043 | D-039 | 2 | Toute attente interactive doit avoir un fallback sans TTY. |
| L-044 | D-040 | 2 | Décision Boss : pas de mention remote explicite = REJECTED direct. |
| L-046 | D-042 | 3 | Tout artefact humain dérive du master courant par duplicate-and-edit ; tour de contrôle = mêmes règles. |
| L-022 | D-016 | 2 | Jamais une session à 100% de contexte : compacter, sous-agents, missions bornées. |

## GATE 2 — LE SÉLECTEUR · commit `e8370f1`
**Livré** : `memory/select_lessons.py`, `{project, mission_type, tags, files_touched}` → top-K ≤ 800 tokens, score **sans LLM, sans horloge, sans aléa** : `(overlap tags+triggers × sévérité) + plancher sévérité + match projet + bonus récurrence + rang récence` ; tie-break stable par id ; fallback systémique sévérité-3 (6 LOIS + méta-défauts) si aucun tag.
**Preuve** : 7 tests — `visual/docx → D-021/D-042/D-034(L8)` ; `async → D-018` ; sans tags → sévérité-3 systémiques (LOIS) ; budget tokens respecté ; `files_touched` pilote l'inférence ; **déterminisme vérifié 10×**.

## GATE 3 — L'INJECTION · commit `b477717`
**Livré** : `memory/inject.py` = **la seule autorité** d'injection (bloc `🧠 RÈGLES ACTIVES` role-aware ; leçons pré-filtrées par `applies_to`, LOIS pour les 3 ; le reviewer reçoit en plus `🛑 MODES DE DÉFAILLANCE CONNUS`). `RunRuntime` (le SEUL chemin de lancement vers un vrai LLM) fait passer chaque rôle par `_inject()` : planner (étape de plan → ids dans `plan.json`), builder (bloc préfixé à la mission, **dans le FileLock** — garde anti-course D-018), reviewer (bloc + checklist préfixés au vrai prompt Codex aux 3 gates). Ids loggés par mission (`memory_injected`) + `active-rules-<role>.md`.
**Preuve** : 6 tests E2E sur le vrai orchestrateur — bloc dans le prompt builder, bloc + checklist dans le prompt reviewer (plan/build/final), ids planner dans `plan.json`, évidence + events par rôle, happy-path atteint toujours `needs_approval`, mission docx surface D-042/D-034.
**Self-review adversariale (obligatoire)** : (1) aucun bypass sur le chemin live — `start()/run_once()/_execute()/_review_gate()` passent tous par `_inject` ; la boucle de correction ré-injecte. (2) async : injection builder déplacée DANS le FileLock ; règles reviewer passées en argument, jamais persistées sur le run.

## GATE 4 — LA BOUCLE · commit `08ec2ac`
**Livré** : `memory/retro.py` — template de rétro (spec→résultat, taxonomie cause-racine, leçons candidates SANS id) ; `ingest_candidates()` ajoute les vraies nouvelles avec id système ; **détecteur de récidive** : une candidate correspondant à une leçon déjà au ledger ne crée AUCUNE leçon — elle incrémente le compteur via un **overlay append-only** (`recurrences.jsonl`, `lessons.jsonl` jamais réécrit — D-016) et lève l'alerte **🔴 « récidive = échec du SYSTÈME D'INJECTION, pas du run »**. Métrique `runs_until_perfect` par projet ; `projects/<name>/PROJECT_MEMORY.md`. Hook live dans `_finalize` (template + métrique, redirigés sous `state_root` via `set_state_dir`, jamais dans le brain committé).
**Preuve** : 7 tests — id système + append-only préservé, récidive détectée (similarité de règle ET `source_defect`) sans nouvelle leçon + alerte 🔴, overlay cumulé, streak `runs_until_perfect`, **`close_mission` E2E lève l'alerte 🔴** et écrit PROJECT_MEMORY, template porte la taxonomie ; + test runtime : hook déclenché, métrique state-local, `perfect=True`.

## GATE 5 — PHASE 0 DANS LE PRODUIT · commit `4a3faad`
**Livré** : `bubble/kickoff.py` — interview progressive (8 blocs A→G, **une question courte à la fois**, réponses stockées au fil de l'eau) ; bloc H (pré-mortem) **généré par le sélecteur Phase 2** ; sortie `projects/<name>/{PROJECT_SPEC.md,ROADMAP.md,PROJECT_MEMORY.md}` (spec née **NON signée**). `sign()` = **action explicite « GO Boss »** écrivant `SIGNÉ : ✅ GO Boss — <date>`, jamais automatique, refusée avant la fin de l'interview. **Hard gate** : `RunRuntime.start(enforce_phase0=True)` REFUSE toute mission sur spec absente/non signée → **« Phase 0 non faite — lance le Kickoff »** (activé dans `joao ui`). Endpoints `/kickoff/*` + bouton « ✅ GO Boss » dans l'UI.
**Preuve** : 8 tests — interview progressive couvrant A–G, 3 artefacts non signés générés, pré-mortem surface les leçons d'autorité, signature explicite/refusée-tôt, **lancement REFUSÉ sans spec signée (E2E)**, **E2E complet kickoff→GO Boss→mission acceptée** (module + API HTTP).

## GATE 6 — CASCADE + BEST-OF-N VÉRIFIÉ · commit `a874d70`
**Livré** : `providers/cascade.py` — tiers escaladés, moins cher d'abord : (a) outil déterministe (~0) → (b) GLM solo → (c) best-of-N GLM → (d) Claude. Escalade **explicite et fail-closed** (échec de vérif / tâche critique / récidive / échec de gate → on descend, jamais silence ; si même Claude échoue → **BLOCKED**). Best-of-N : N=3 GLM sur angles DIVERS (perf/lisibilité/edge-cases), juge **tests objectifs d'abord**, juge LLM **seulement pour départager** — builder ≠ juge. Décisions + coût loggés (`MissionCost`).
**Preuve** : 8 tests + **démo réelle** :
- tâche simple `rename-symbol` → tier **déterministe, coût 0**.
- tâche critique `auth-token-rotation` → **best-of-3, 3 sorties**, gagnant par tests objectifs (readability 0.91), **coût 3 unités** — vs **20** en allant direct à Claude → **cascade économise 17**.
- juge LLM invoqué 0 fois quand les tests tranchent, exactement 1 fois sur égalité réelle ; fail-closed BLOCK quand rien ne se vérifie (Claude tenté avant de bloquer).
**Self-review adversariale** : cascade-down loggé `escalated=True`, jamais silencieux ; Claude en dernier ; non-vérifiable → BLOCKED, jamais accepté.

---

## COÛT PAR PHASE (proxy structurel)
| Phase | commit | fichiers | tests gate | boucles de réparation |
|-------|--------|----------|-----------|----------------------|
| P1 stock | a01434f | import_ledger.py + lessons.jsonl | 5 | 0 (fix rule-extraction pré-commit) |
| P2 sélecteur | e8370f1 | select_lessons.py | 7 | 0 |
| P3 injection | b477717 | inject.py + runtime.py | 6 | 0 |
| P4 boucle | 08ec2ac | retro.py + runtime.py | 7 | 0 |
| P5 Phase 0 | 4a3faad | kickoff.py + api.py + cli + runtime | 8 | 0 |
| P6 cascade | a874d70 | cascade.py | 8 | 0 |

**Total** : 6 commits · ~1230 lignes de code neuf (hors tests) · 42 tests de gate · **0 boucle de réparation consommée** (plafond : 2/item).
_Le coût en tokens/€ n'est pas instrumenté dans ce run (voir L9)._

## MÉTRIQUE — runs_until_perfect
Dogfood exécuté via le système Phase 4 : `record_run_metric("joao", "BIG-RUN-LE-CERVEAU", perfect=True)` →
**`runs_until_perfect(joao) = 0`** (le dernier run était parfait : 6/6 gates, 0 récidive, 0 régression). Métrique initialisée dans le brain-state (`memory/run_metrics.jsonl`, gitignoré).

## NON VÉRIFIÉ / LIMITES (L9)
1. **Deux mondes de prompt.** Le monde typé `ProviderRequest/invoke` (benchmark + queue fail-closed) est **dormant** : aucun chemin de lancement n'y pilote un vrai LLM. Il n'est **délibérément pas câblé** à l'injection. Le chemin live (`RunRuntime`/`joao ui`) l'est intégralement. Si ce monde devient actif, il faudra câbler l'injection à la création de `ProviderRequest`.
2. **Fail-open sur mis-install.** Si le sous-système `memory/` est absent, `_inject` loggue `memory_injection_unavailable` (bruyant) et poursuit sans bloc, plutôt que de bloquer un package mal installé. Dans le vrai repo, `memory/` est présent → injection obligatoire.
3. **Cascade non câblée au builder live.** La logique de routage B-29.1 (cascade + best-of-N + ledger de coût + escalade fail-closed) est complète et vérifiée avec workers injectés. **Le câblage aux vrais workers GLM/Codex (subprocess) dans `RunRuntime` est la prochaine étape d'intégration** — même patron que `GLMBuilder`/`CodexCLIReviewer`.
4. **Planner déterministe.** Dans le produit bubble, le « planner » est déterministe (pas d'appel LLM) : son bloc mémoire est injecté dans l'artefact de plan + loggé, pas dans un prompt LLM. Les 3 rôles reçoivent bien le bloc ; seul le builder et le reviewer parlent à un vrai LLM.
5. **Leçons candidates de CE run non gravées.** Le stock reste curaté : les propositions de leçons issues de ce run (ex. « architecture prompt à 2 mondes → tout point de lancement live passe par une autorité d'injection unique ») sont soumises au Boss plutôt qu'auto-append (D-029 : le système attribue l'id, le Boss valide le fond).
6. **Coût tokens non instrumenté** pour ce run (proxy structurel ci-dessus uniquement).

## HORS-SCOPE (non démarré, conforme à la carte)
B-29.2+ : nuits spéculatives, cliquet déterministe, auto-training, B-26 débat.
