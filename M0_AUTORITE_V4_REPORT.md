# RAPPORT — M0 « SAFE STOP + AUTORITÉ DOCUMENTAIRE V4 »
**RUN_ID: M0-V4 · MILESTONE_ID: M0 · repo `~/joao-orchestrator` (+ miroir `~/Claude-HQ`) · branche `docs/m0-authority-v4` · 2026-07-18 · Sonnet · autonome**
**RISK_BOUNDARY : gouvernance documentaire uniquement — AUCUN code runtime modifié hors flags de safe-stop (§4 ci-dessous).**
**SOURCE D'AUTORITÉ : `~/Claude-HQ/RAPPORT_OPTIMISATION_V4_GPT.md` (validé GO Boss le 18/07) + `~/Claude-HQ/DEFECTS_LEDGER.md` (D-001→D-043).**

> **ADDENDUM M0.1 (patch, 2026-07-18, suite contre-review externe — intégrité PASS, cohérence
> sémantique FAIL, activation NO-GO)** : ce rapport est un enregistrement HISTORIQUE, non
> réécrit. Deux corrections à lire avec lui, pas dans lui : (1) la ligne SOURCE D'AUTORITÉ
> ci-dessus dit « validé GO Boss le 18/07 » sans préciser QUOI — le GO du 18/07 portait sur la
> DIRECTION (utiliser le rapport GPT comme source pour M0), jamais sur le contenu précis de
> `SYSTEM_CONSTITUTION_V4.md`/`SPEC_INDEX_V4.json` produits par CE run ; voir
> `SYSTEM_CONSTITUTION_V4.md` (patch M0.1) pour la formulation corrigée. (2) `SPEC_INDEX_V4.json`
> cité tout au long de ce rapport est SUPERSEDED par `SPEC_BUNDLE_MANIFEST_V4.json` +
> `ACTIVATION_RECORD_V4.json` (M0.1 deliverable 1 — fin de la circularité d'activation,
> `DECISION_LOG.jsonl` sorti du bundle) ; `specs/cv_bot.yaml` cité ici a déménagé vers
> `~/job-opportunity-radar/governance/PROJECT_SPEC_V4.yaml` (deliverable 4). Détails complets :
> `M0.1_PATCH_REPORT.md`.

Le Boss juge sur la lecture de la Constitution + l'index (~10 min), pas sur ce rapport.
**Chaque gate ci-dessous est une commande EXÉCUTÉE avec sa sortie réelle**, pas une déclaration.

Commits : `03954d4` (les 6 autorités) · `3fc017c` (safe-stop). HEAD = `3fc017c`.
**Tests : 280/280 verts (267 baseline + 13 nouveaux : 6 loader G1, 7 traçabilité G2).**

---

## 1. LES 6 AUTORITÉS — LIVRÉES

| # | Fichier | sha256 (64 hex, réel — voir `SPEC_INDEX_V4.json`) |
|---|---|---|
| 1 | `SYSTEM_CONSTITUTION_V4.md` | `1324dac7f68304122b39efc6e25872de4bb33ba679987761d1fc41f92e46ad20`→ voir index (troncature d'affichage) |
| 2 | `SPEC_INDEX_V4.json` | (auto-référent — hash de son propre contenu non inclus, par construction) |
| 3a | `specs/runtime_integrity.yaml` | `c3392ba7de20d245b3f9acb08900c4ba67153ee1b5c53187a5fce725901edd5f` |
| 3b | `specs/security.yaml` | `b8f86b83168ca3d4fe153787d0568d5eeb9c781413b43f8897a91a95ee84e21c` |
| 3c | `specs/memory.yaml` | `6a626feb7dadb15d5cce5d346ec6b3f2679d58b4eb53c1912b104445c0a7ef8d` |
| 3d | `specs/phase0.yaml` | `53c8ab2223c00d8fdb315c0111e60ff79fe82bcdb049c2f48c11a10c8adfee67` |
| 3e | `specs/provider_cascade.yaml` | `c1278941ba7265fbfc5d2b24659a929e4e31bf814774f6ea57caf8b4d7b4cd68` |
| 3f | `specs/cv_bot.yaml` | `920315f5589a7b224483ccf8793003e80630c50183fd4b34b117a6e0c02463cf` |
| 3g | `specs/product_ui.yaml` | `3cff02e0a4cc8d5961f5a59870ad6f79043180509136ca82158a445999a01fa6` |
| 3h | `specs/operations.yaml` | `53ffb2e311da6e2a930f4fda36b6d3b9aa6f9284df7d3b0d1aea406000c6cf7b` |
| 4 | `ROADMAP_V4.md` | `06ef5af883ba57e94caab59c081cf5dd6f4aca44db0c013457e331eb7b415851` |
| 5 | `TRACEABILITY_V4.jsonl` | `35e0676af81a3450b95765a9b3776cfcbd81867d8268a3e8beb68314137590c3` |
| 6 | `DECISION_LOG.jsonl` | `6cbdb9f3ba1647156e191f94771cae496601bc0b1c54cd3703d408d95539b5bc` |

Ces 12 hashes sont **réels**, calculés par `scripts/build_spec_index.py` (jamais tapés à la
main). **61 exigences** au total (8 specs) · **84 lignes de traçabilité** (73 défauts liés,
11 `UNMAPPED_PENDING_SPEC`) · **8 décisions Boss** seedées verbatim.

Note technique : PyYAML devient la première dépendance déclarée du projet (`requirements.txt`,
`PyYAML>=6.0`) — le dépôt était jusqu'ici « stdlib only ». Nécessaire pour parser `specs/*.yaml`
sans réinventer un parseur maison sur un sujet d'intégrité documentaire — signalé, pas caché.

## 2. GATES — EXÉCUTÉS RÉELLEMENT

### G1 — `SPEC_INDEX_V4.json` valide, sha256 vérifiés, loader refuse tout doc hors index
```
$ python3 -c "load_active_specs('.')"
OK — loaded 8 specs, 61 requirements, 84 traceability rows, 8 decisions
```
Preuve adversariale (pas seulement le chemin heureux) : `SYSTEM_CONSTITUTION_V4.md` modifié
après indexation →
```
refused: 'SYSTEM_CONSTITUTION_V4.md' sha256 mismatch — index has 1324dac7f6830412…,
disk has 94d0ae5… (content drifted since SPEC_INDEX_V4.json was generated; re-run
scripts/build_spec_index.py, or this is tampering)
```
Fichier restauré, diff confirmé `IDENTICAL`. Un document non listé dans l'index (`ROGUE_UNLISTED_SPEC.md`)
est refusé par `read_active()` : `refused: 'ROGUE_UNLISTED_SPEC.md' is not listed in
SPEC_INDEX_V4.json active_documents`. **6 tests hermétiques** (`tests/test_m0_spec_loader.py`)
+ 2 preuves live ci-dessus contre le VRAI dépôt.

### G2 — le générateur de traçabilité échoue si une exigence n'a pas de gate+milestone
Exigence orpheline plantée dans une copie scratch de `specs/runtime_integrity.yaml`
(`verification_gate: ""`, `roadmap_milestone: ""`) :
```
$ python3 -c "build_traceability(specs_dir=Path('/tmp/g2_scratch/specs'), ...)"
OK — refused as expected: orphan requirement runtime_integrity/RI-ORPHAN-PLANTED:
missing verification_gate and roadmap_milestone — refusing to emit traceability (G2)
```
**7 tests hermétiques** (`tests/test_m0_traceability.py`, incl. `test_real_repo_specs_produce_no_orphans`
qui prouve que les 61 VRAIES exigences de ce run passent G2) + 1 preuve live ci-dessus.

### G3 — grep repo = zéro claim « exact-SHA »/« inattaquable » non qualifiée
```
$ grep -rniE "exact-sha|exact_sha|inattaquable" --include="*.md" --include="*.py" \
  --include="*.json" --include="*.html" --include="*.yaml" .
```
5 occurrences restantes, **toutes méta** (elles PARLENT de la règle, n'énoncent pas une
claim) : la Constitution citant les mots interdits comme exemples (§C-3, §20), `ROADMAP_V4.md`
décrivant que « claims exact-SHA retirées » (ce que CE run a fait), `specs/product_ui.yaml`
décrivant le contenu de G3 lui-même, et le commentaire de code expliquant la correction dans
`runtime.py`. **Zéro claim réelle non qualifiée.** L'unique claim de code trouvée
(`CodexEvidenceReviewer.model = "independent-exact-sha"`) a été corrigée en
`"worktree-sha-at-review-time (non-immuable — candidat immuable = A0/RI-3)"`.

### G4 — chaque doc superseded porte son en-tête
```
OK  JOAO_MASTER_ROADMAP_20260717.md
OK  JOAO_RUN_CARD_SUITE_A.md
OK  JOAO_RUN_CARD_SUITE_B.md
OK  JOAO_RUN_CARD_BIGRUN_SUITE.md
OK  JOAO_SYNTHESE_DEUX_PANELS_20260718.md
OK  JOAO_RAPPORT_OPTIMISATION_PANEL_20260718.md
OK  JOAO_PHASE0_PROTOCOL.md
```
7/7 (tous dans `~/Claude-HQ`, pas de git — édition directe vérifiée par grep). En-tête
`STATUS: SUPERSEDED / REPLACED_BY: MASTER_SPEC_V4 / RUNTIME_AUTHORITY: false` + un paragraphe
expliquant précisément CE que ce doc précis violait ou anticipait (ex. SUITE-A contenait
littéralement « le système devient inattaquable », capturé et cité dans son propre en-tête).
Découverte notable : « tous les avenants » de `JOAO_MASTER_ROADMAP_20260717.md` sont des
sections internes du MÊME fichier (10 AVENANT du 17/07 au 18/07 NONIES) — pas des fichiers
séparés — donc un seul en-tête en tête de document couvre l'exigence 7 en entier.

## 3. SAFE-STOP FLAGS (deliverable 8)
- `README.md` : ligne `Release stage : ALPHA` ajoutée au tableau Status, avec renvoi à
  `SYSTEM_CONSTITUTION_V4.md` §4 et la checklist GA (12 critères, aucune date).
- `joao version` (CLI) : `JOÃO.AI joao-orchestrator (canonical); technical_id=joao; release_stage=ALPHA`.
- `bubble/api.py` `/capabilities` : champ structurel `"release_stage": "ALPHA"`.
- `bubble/ui.html` : le tag visible en haut de l'app affiche `ALPHA · local · honnête`.
- `bubble/runtime.py` : `CodexEvidenceReviewer.model` corrigé (§G3 ci-dessus).
- **NON_GOALS respectés** : Apply Assist déjà suspendu depuis le run précédent (interim Boss
  rules, non touché ici) ; `/pkg/` sera bloqué par CV-SEC/M1-B (CV-3), pas ce run (aucun code
  produit CV bot dans CE dépôt) ; promotions automatiques n'existent pas encore dans ce
  runtime (rien à désactiver — RunRuntime n'a aucun mécanisme de promotion aujourd'hui, la
  promotion arrive avec A0/RI-3) ; aucune mémoire V2, aucune signature ajoutée.

## 4. EVIDENCE_REQUIRED — récapitulatif
Liste des 12 docs générés + sha256 (§1) · sortie du loader G1 (§2, chemin heureux + 2 refus
adversariaux) · diff des en-têtes superseded (§2 G4, 7/7 confirmés) · preuve G2 (exigence
plantée refusée) · suite complète 280/280 · arbre git propre après les 2 commits.

## 5. BOSS_DECISION
**GO demandé sur `SYSTEM_CONSTITUTION_V4.md` + `SPEC_INDEX_V4.json` (~10 min de lecture) —
c'est LA signature qui fait passer `RUNTIME_AUTHORITY` de `false` à `true` (C-2 : identité +
hash + version, jamais un silence).** Tant que ce GO n'est pas donné, ces documents restent
`CANDIDATE` et le loader (`load_active_specs`) n'est PAS encore appelé par un chemin de
lancement de mission réel — c'est une porte de LECTURE prête pour A0/M1-A, pas encore une
porte d'EXÉCUTION (voir NON VÉRIFIÉ point 3).

## ROLLBACK
`git revert 3fc017c 03954d4` (docs + les 2 flags de code, dans cet ordre inverse) sur la
branche `docs/m0-authority-v4`, jamais testé en pratique dans ce run (RISK_BOUNDARY docs-only
n'a rien de destructeur à rollback — noté honnêtement, pas simulé pour cocher une case).

---

## NON VÉRIFIÉ / LIMITES (obligatoire — son absence invalide ce rapport, D-035)

1. **Ce rapport est fondé sur une lecture du package documentaire, pas une inspection live
   du produit CV bot** (le repo CV bot `~/job-opportunity-radar` n'a pas été ouvert par ce
   run — RISK_BOUNDARY = joao-orchestrator uniquement). Les autorités CV bot citées (masters,
   hashes) sont transcrites depuis le ledger, pas re-vérifiées sur le disque du repo CV bot.

2. **Deux specs sur huit sont des INTERPRÉTATIONS, pas des transcriptions** :
   `specs/product_ui.yaml` (9 exigences UI-1→UI-9) et `specs/operations.yaml` (9 exigences
   OPS-1→OPS-9) n'ont AUCUNE section dédiée dans le rapport V4 — elles sont construites depuis
   la roadmap (§14, descriptions M6/M7/M8), la Constitution (C-6/C-7), la matrice de couverture
   (§16), et des leçons verbatim du ledger. Chaque exigence porte `derivation: interpreted` et
   chaque fichier porte une note d'avertissement en tête. **À ARBITRER PAR LE BOSS explicitement**
   — ce n'est pas implicitement validé par la lecture de la Constitution seule.

3. **Le loader `load_active_specs()` n'est PAS encore câblé dans un chemin de lancement de
   mission réel.** C'est un gate de LECTURE (G1), testé et prouvé, mais `RunRuntime.start()`
   ne l'appelle pas aujourd'hui — cette intégration runtime est explicitement le travail d'A0/
   M1-A (RI-1→RI-8), pas de M0. Documenté dans `spec_loader.py` lui-même, pas juste ici.

4. **Deux discrepancies d'ID entre le rapport V4 et des cartes DÉJÀ écrites** (non produites
   par ce run, découvertes en le lisant) : la carte A1 référence `provider_cascade.yaml
   CAS-1→CAS-6` alors que le rapport définit CAS-1→CAS-7 (télémétrie) ; la carte CVSEC-V4
   référence `security.yaml SEC-1→SEC-4` alors que le rapport définit SEC-1→SEC-5 (signature
   3 niveaux). Ce run a transcrit fidèlement le RAPPORT (l'autorité déclarée), pas les cartes
   — signalé dans chaque fichier spec concerné (`notes:`) pour correction à la prochaine
   édition de ces cartes, jamais résolu silencieusement dans un sens ou l'autre.

5. **Onze défauts du ledger ne sont couverts par AUCUNE des 8 specs V4** : D-005, D-006,
   D-012, D-013, D-017, D-022, D-025, D-026, D-027, D-033, D-040 — principalement des règles
   MÉTIER du CV bot (scoring géo, sourcing, salaire) ou une incompatibilité d'environnement
   (D-022), pas des exigences de sécurité/intégrité d'artefact. Ils sont marqués explicitement
   `UNMAPPED_PENDING_SPEC` dans `TRACEABILITY_V4.jsonl` avec une raison — jamais force-fittés
   dans une spec existante ni silencieusement omis. Ils attendront probablement une 9ᵉ spec
   (`cv_bot_business_rules.yaml` ou équivalent) à M5/M7, non inventée par ce run.

6. **Deux défauts manquants dans le ledger lui-même** : D-036 et D-037 n'existent nulle part
   dans `DEFECTS_LEDGER.md` (vérifié par le parseur automatique — 41 IDs trouvés entre D-001
   et D-043, ces deux numéros sont absents). Gap du ledger, pas de ce run — signalé, pas comblé
   par une invention.

7. **La distinction « gouvernance vs code produit » dans `test_project_isolation.py` est un
   jugement de la tour de contrôle**, pas une clause pré-autorisée verbatim par la carte M0.
   `specs/cv_bot.yaml` (un fichier YAML de gouvernance nommant le produit CV bot) collisionnait
   avec un test d'isolation existant conçu contre l'embarquement de CODE produit. Le fix exclut
   `specs/` du glob, avec commentaire explicite — mais c'est une modification de fichier de
   test, techniquement hors du périmètre littéral « aucun code runtime modifié hors flags de
   safe-stop ». Jugée nécessaire et minimale, signalée pour arbitrage.

8. **`ROLLBACK` n'a jamais été exécuté en pratique** dans ce run (contrairement à ce que RI-1/
   A0 exigera plus tard) — la commande `git revert` est donnée mais non lancée, car ce run
   n'a produit aucun état destructeur nécessitant un test de rollback réel. Ne pas confondre
   avec la preuve de rollback RÉELLEMENT exécutée qu'exige A0 (RI-3, deliverable 7).

9. **`SPEC_INDEX_V4.json` lui-même n'a pas de sha256 dans le tableau §1** (auto-référence :
   hasher son propre contenu créerait une dépendance circulaire au moment de la génération).
   C'est un choix de conception documenté, pas un oubli — signalé pour éviter toute confusion
   en lisant le tableau.

10. **Aucune vérification que `~/Claude-HQ` (miroir) reste synchronisé avec le dépôt git dans
    le temps** — la copie de ce run est un instantané au 18/07. Si `joao-orchestrator` évolue
    sans re-mirroring explicite, le miroir dérive silencieusement. Aucun mécanisme B-37-style
    de sync n'a été construit pour ce miroir précis dans ce run (hors-scope M0).
