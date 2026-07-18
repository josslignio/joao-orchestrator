# ROADMAP V4 — JOÃO.AI ET CV BOT
**STATUS: CANDIDATE · VERSION: 4.0.0 · DATE: 2026-07-18 · RUNTIME_AUTHORITY: intended-once-signed (voir SYSTEM_CONSTITUTION_V4.md §8)**

Ce document ne recopie JAMAIS le contenu des specs — il référence leurs IDs d'exigences
(voir `specs/*.yaml`), les dépendances entre milestones, les gates, et les décisions Boss
attendues. **Aucune date de GA** (D-043 : un jalon de statut se déclare sur checklist de
critères prouvés, jamais sur une date).

Chaque milestone ci-dessous correspond à une carte de run distincte (format §17 du rapport
V4), une seule `RISK_BOUNDARY` par carte (C-4). Les cartes déjà écrites sont référencées ;
celles qui restent à écrire sont marquées `(à écrire)`.

---

## M0 — SAFE STOP + AUTORITÉ DOCUMENTAIRE
**Carte** : `~/Claude-HQ/JOAO_RUN_CARD_M0_AUTORITE_V4.md` (M0) + `~/Claude-HQ/JOAO_RUN_CARD_M01_PATCH_ACTIVATION.md`
(M0.1, correctif documentaire suite contre-review externe : intégrité PASS, cohérence
sémantique FAIL). **Dépend de** : rien (racine).
**Livre (périmètre RÉEL, corrigé D-043)** : les 7 autorités du bundle + l'activation record
(§1 de la Constitution) · statut `ALPHA` affiché (README/CLI/UI/api.py) · Apply Assist
SUSPENDU (hérité, non réactivé par ce run) · claims trompeuses retirées (exact-SHA,
« validé GO Boss » sur les mauvais documents) · documents candidats générés ET vérifiés en
lecture (loader G1 + traçabilité G2). **M0 ne bloque PAS `/pkg/`** — le blocage de l'accès
brut `/pkg/` est une exigence CV-SEC (`governance/PROJECT_SPEC_V4.yaml` CV-3, M1-B), pas une
livraison de M0 : aucun code produit CV bot n'est touché par ce dépôt.
**Exigences touchées** : UI-9, OPS-9 (product_ui.yaml, operations.yaml).
**Gate** : une seule roadmap active, aucune ancienne spec chargée par le loader (G1), GO Boss.
**Décision Boss** : GO sur `SYSTEM_CONSTITUTION_V4.md` + `SPEC_BUNDLE_MANIFEST_V4.json` — c'est
LA signature, enregistrée dans `ACTIVATION_RECORD_V4.json` (jamais dans le bundle lui-même),
qui active V4 (fait passer `runtime_authority` à `true`).

## VAGUE 1 — PARALLÈLE (repos distincts, reviews et promotions séparées)

### M1-A — A0 « intégrité critique »
**Carte** : `~/Claude-HQ/JOAO_RUN_CARD_A0_INTEGRITE.md`. **Dépend de** : M0 promu.
**Exigences** : `specs/runtime_integrity.yaml` RI-1→RI-8 · `specs/security.yaml` SEC-3 ·
`specs/product_ui.yaml` UI-2, UI-3 · `specs/operations.yaml` OPS-1, OPS-8.
**Gate** : les 8 attaques (ATTACK_TESTS) rouge→vert · cycle complet build→candidat→tests→
review→approbation→promotion→rollback sur une mission jouet, MÊME hash de bout en bout.
**Décision Boss** : GO sur la démo du cycle complet.

### M1-B — CV-SEC V4 « sécurité du produit utilisé »
**Carte** : `~/Claude-HQ/CV_BOT_RUN_CARD_CVSEC_V4.md`. **Dépend de** : rien (repo distinct,
parallèle à M1-A). **Repo** : `~/job-opportunity-radar`.
**Exigences** : `governance/PROJECT_SPEC_V4.yaml` (ce repo, ex-`specs/cv_bot.yaml` — déplacé
par le patch M0.1 D4) CV-1→CV-10 · `governance/cv_bot_business_rules.yaml` (ce repo, patch
M0.1 D6) CVBIZ-1→CVBIZ-11 · JOÃO `specs/security.yaml` SEC-1, SEC-2, SEC-4 ·
JOÃO `specs/product_ui.yaml` UI-4, UI-8.
**Gate** : les 10 attaques rouge→vert · parcours réel complet (offre→CV→lettre→download→
Apply Assist en DONNÉES DE TEST→revue humaine) · preuve que le flux quotidien du Boss est inchangé.
**Décision Boss** : GO sur le parcours + levée de la suspension Apply Assist (CV-10) — APRÈS
un premier passage supervisé en données réelles.

## M2 — A2 « mémoire + Phase 0 sécurisées »
**Carte** : `~/Claude-HQ/JOAO_RUN_CARD_A2_MEMOIRE_PHASE0.md`. **Dépend de** : M1-A promu.
**Ordre voulu** : M2 AVANT M3 — on ne branche pas de vrais workers sur une mémoire
empoisonnable ni une signature imitable.
**Exigences** : `specs/memory.yaml` MEM-1→MEM-7 · `specs/phase0.yaml` P0-1→P0-6 ·
`specs/security.yaml` SEC-5.
**Gate** : les 9 attaques rouge→vert · une signature Boss réelle de bout en bout
(UI→Keychain→journal) · reconstruction d'une vue Markdown depuis le journal.
**Décision Boss** : signer les 3 PROJECT_SPEC (joao / job-cv-auto / weekly-trading-radar)
via le nouveau mécanisme (≈10 min).

## M3 — A1 « cascade réelle + télémétrie »
**Carte** : `~/Claude-HQ/JOAO_RUN_CARD_A1_CASCADE_REELLE.md`. **Dépend de** : M1-A ET M2 promus.
**Exigences** : `specs/provider_cascade.yaml` CAS-1→CAS-7 · `specs/operations.yaml` OPS-6.
**Gate** : les 5 missions de preuve (déterministe / GLM simple / best-of-3 réel / échec
total→BLOCKED / récidive→escalade forcée), toutes réelles, jamais simulées.
**Décision Boss** : GO → enchaîner M4 (aucune nouvelle feature pendant M4) — c'est la porte
d'entrée de Chat Era.

## M4 — VALIDATION SUR 10 MISSIONS RÉELLES
**Carte** : (à écrire, dépend de M3 promu). Aucune nouvelle feature pendant M4.
**Bloquants** : 10/10 mêmes trees testés/revus · 10/10 évidences complètes · zéro fail-open ·
zéro secret exposé · zéro P0/P1 post-approbation · zéro récidive silencieuse · valeurs
manquantes = `UNKNOWN` (jamais un proxy).
**Performance initiale** (à recalibrer après ce jalon, jamais présumée avant) : ≥8/10
utilisables au premier jugement · médiane ≤1 boucle de réparation · aucune session saturée
(OPS-8) · baisse mesurable des minutes Boss / résultat (North Star, §19 du rapport).
**Décision Boss** : poursuivre / corriger / rollback. **Chat Era interdite avant cette décision.**

## M5 — CLIQUET DÉTERMINISTE
**Carte** : (à écrire, dépend de M4 = poursuivre). Outils avec contrat, version, health
check, tests, mutation tests, fallback. **Aucune autonomie nocturne à ce stade.**
**Exigences** : `specs/memory.yaml` MEM-7 (graduation vers le code) ·
`~/job-opportunity-radar/governance/cv_bot_business_rules.yaml` CVBIZ-1→CVBIZ-11
et `specs/product_ui.yaml` (règles CV/UX graduées hors prompt une fois couvertes par une gate technique).

## M6 — CHAT CORE MINIMAL
**Carte** : (à écrire, dépend de M4 = décision GO). **INTERDIT avant la décision M4** (§4.2 du
rapport : les 10 missions sont une gate AVANT Chat Era, pas seulement avant le label GA).
**Exigences** : `specs/product_ui.yaml` UI-5, UI-6, UI-7 · `specs/phase0.yaml` (hard gate
mission-sans-spec-signée réutilisée depuis le chat).
**Contrat** : « Bonjour » ne lance rien · mission sans spec signée refusée · verdict réel
visible · reprise après reload. **Hors-scope** : web généralisé, nuits, premium, multi-projets.

## M7 — CONTRAT DESIGN ET UI
**Carte** : (à écrire, dépend de M6 promu — « sur moteur stable »).
**Exigences** : `specs/product_ui.yaml` UI-1, UI-4 · maquettes, tokens, composants, E2E.

## M8 — SUPERVISION
**Carte** : (à écrire, dépend de M3+M4 promus — a besoin de télémétrie réelle CAS-7).
**Exigences** : `specs/operations.yaml` OPS-2→OPS-5.
**Contrat** : digest humain post-run · alertes · budgets · sentinels de régression ·
kill-switch · queue d'approbation · GitHub contrôlé · rollback visible.

## M9 — BENCH + RED TEAM ÉTENDUS
**Carte** : (à écrire, dépend de M8 promu). Rejeu D-001→D-043 · injection · supply chain ·
mutations · dérive de prompts · comparaison providers · crash recovery · attaques serveur ·
fuite de secrets · chaos subprocess.

## M10 — NUITS LIMITÉES
**Carte** : (à écrire, dépend de M9 promu — supervision + red team AVANT toute nuit).
**Exigences** : `specs/operations.yaml` OPS-7.
**Autorisé** : refresh données, health checks, bench, branches spéculatives, préparation
locale. **Interdit** : merge, publication, candidature, email, signature, activation de
règle, changement d'autorité, promotion.

## M11 — DÉBAT, AUTO-TRAINING, FACTORY KICKOFF, NOUVEAUX PROJETS
**Carte** : (à écrire, dépend de M10 promu). Estimations `NON MESURÉE` tant que non
soutenues par le bench (M9).

---

## CHECKLIST GA (rapport §15 — aucune date, 12 critères, tous vérifiables indépendamment)
1. A0 promu + audité. 2. CV-SEC promu + Apply Assist réactivé après test contrôlé. 3. A2 promu.
4. A1 promu. 5. Dix missions validées (M4). 6. Review Codex externe ACCEPT du tree courant.
7. Zéro P0/P1 ouvert sur chemins actifs. 8. Zéro route contournant les gates. 9. Rollback
réellement exécuté. 10. Rapports conformes D-043 (format CLAIM/MÉCANISME/PORTÉE/LIMITES).
11. Trois specs projet signées sur leur hash. 12. NON VÉRIFIÉ/LIMITES publiés à chaque rapport.
**Le jour Codex (23/07 06:22) est un jour de REVIEW, jamais une date de GA.**

## NON VÉRIFIÉ / LIMITES DE CE DOCUMENT
- L'attribution des exigences UI-*/OPS-* à M6/M7/M8 suit le découpage du rapport §14, mais
  ces deux specs sont elles-mêmes des interprétations (voir `specs/product_ui.yaml` et
  `specs/operations.yaml`) — à arbitrer par le Boss avec le reste de ces deux fichiers.
- Les cartes M4→M11 n'existent pas encore sur disque ; seules M0/M1-A/M1-B/M2/M3 sont
  écrites (dans `~/Claude-HQ/`). Ce roadmap ne prétend pas qu'elles existent, il les nomme
  comme prochaines étapes.
- Aucune estimation de durée n'est donnée pour un milestone — le rapport V4 l'interdit
  explicitement (§20 : « pas de freeze GA daté »).
