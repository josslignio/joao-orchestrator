# SYSTEM CONSTITUTION V4 — JOÃO.AI (autorité documentaire unique)
**STATUS: PROMOTED_PENDING_GO_BOSS · VERSION: 4.0.0 · DATE: 2026-07-18 · RUNTIME_AUTHORITY: intended-once-signed**
**SOURCE: transcription fidèle du rapport d'audit externe GPT (`~/Claude-HQ/RAPPORT_OPTIMISATION_V4_GPT.md`, §6-7), validé GO Boss le 18/07 comme source d'autorité du run M0. Ce document devient l'autorité RUNTIME du système au moment où le Boss dit GO explicitement sur CE document + `SPEC_INDEX_V4.json` (voir §9 — c'est la signature qui active V4).**

Le runtime ne parcourt jamais librement les anciens Markdown. Un document actif est un document listé dans `SPEC_INDEX_V4.json`, avec un sha256 vérifié — jamais « le dernier document en date ».

---

## 1. LES 6 AUTORITÉS (l'architecture documentaire, rapport §5)

Les anciens documents restent dans l'historique, marqués `STATUS: SUPERSEDED / REPLACED_BY: MASTER_SPEC_V4 / RUNTIME_AUTHORITY: false` — leur contenu de fond survit **à l'intérieur** des specs V4, il n'est pas perdu, seulement plus une autorité d'exécution directe.

1. **`SYSTEM_CONSTITUTION_V4.md`** (ce document) — invariants universels : frontières de confiance, interdits, promotion, rôles, statuts, portée des preuves.
2. **`SPEC_INDEX_V4.json`** — index machine des documents actifs (versions + sha256 réels). Un document hors index n'est pas actif.
3. **`specs/*.yaml`** — 8 specs typées (`runtime_integrity`, `security`, `memory`, `phase0`, `provider_cascade`, `cv_bot`, `product_ui`, `operations`) ; chaque exigence porte : id immuable, statement, severity, verification_gate, roadmap_milestone, owner, status.
4. **`ROADMAP_V4.md`** — ne recopie jamais les specs ; référence leurs IDs, les dépendances, les gates, les décisions Boss.
5. **`TRACEABILITY_V4.jsonl`** — défaut → exigence → code → test → attaque → preuve → milestone → statut. Une exigence sans test/preuve/milestone est **invalide** (le générateur refuse de l'émettre — voir G2).
6. **`DECISION_LOG.jsonl`** — décisions Boss : décision, portée, date, version, hash, preuve, supersession. Le Markdown est une VUE humaine, jamais une autorité machine.

## 2. LES 7 LOIS (C-1 → C-7, verbatim rapport §6)

**C-1 — Une seule autorité.** Le runtime charge exclusivement `SPEC_INDEX_V4.json` — jamais « le dernier document ».

**C-2 — Silence ≠ GO.** Sont INVALIDES comme validation : l'absence d'opposition, une demande de lancement, l'ouverture d'un rendu, un message ambigu, l'approbation d'un autre artefact. Un GO exige : identité + hash + version + rendu si visuel.

**C-3 — Une propriété = une preuve définie.** Toute claim suit le format :
```
CLAIM: <la propriété annoncée>
MÉCANISME DE PREUVE: <le contrôle technique exact qui la produit>
PORTÉE EXACTE: <ce que ça couvre, précisément>
LIMITES: <ce que ça ne couvre PAS>
```
Interdits **sans cette qualification complète** : « inattaquable », « sécurisé », « exact-SHA », « conforme », « dérivé du master », « fail-closed », « production-ready », « GA ». Ce ne sont pas des mots bannis en soi — ce sont des mots qui EXIGENT le format CLAIM/MÉCANISME/PORTÉE/LIMITES à côté d'eux, sinon ils sont reformulés plus étroit ou supprimés (D-043).

**C-4 — Un run = une frontière principale de risque.** (intégrité, sécurité CV, mémoire, provider, interface, autonomie — jamais plusieurs frontières mélangées dans un même run ; récidive D-016/D-043 si violé — voir SUITE-A, retirée pour cette raison exacte).

**C-5 — Builder ≠ reviewer.** Le reviewer final est indépendant : modèle/provider distinct, contexte indépendant, artefact GELÉ, aucun pouvoir d'écriture, verdict structuré. Codex = auditeur externe final quand disponible, **jamais bloquant** (son absence ne doit jamais empêcher un candidat d'être évalué par le reviewer interne).

**C-6 — Le Boss valide, il ne debugge pas.** Il signe, choisit les autorités visuelles, arbitre le produit, accepte/rejette le candidat final. JAMAIS : logs techniques, corrections de fichiers, relances manuelles, vérification de hashes à la main.

**C-7 — Promotion atomique.** tag + snapshot + manifest + hashes + evidence + rollback TESTÉ. Jamais d'écrasement d'un artefact promu.

## 3. VOCABULAIRE DES STATUTS (rapport §7)

Statuts d'un **run** : `DRAFT` → `READY` → `RUNNING` → `FAILED` | `BLOCKED` → `CANDIDATE` → `REVIEWED` → `BOSS_APPROVED` → `PROMOTED` | `ROLLED_BACK`.
`DELIVERED` **n'existe plus** sans artefact promu — un run qui produit du texte de succès sans candidat gelé + reviewer + promotion n'est PAS `DELIVERED`, il est au mieux `CANDIDATE` ou `REVIEWED`.

Statuts d'une **exigence** (`specs/*.yaml`, vocabulaire introduit par ce document — dérivé du besoin de traçabilité §5.5, non explicitement nommé par le rapport, donc marqué comme extension) :
`PLANNED` (exigence transcrite, aucun run de mise en œuvre n'a encore tourné sous le régime V4) → `IN_PROGRESS` (un run M1-x/M2/M3 est en cours dessus) → `SATISFIED` (gate passée, preuve archivée) → `REGRESSED` (une gate qui passait échoue à nouveau — alerte récidive).

## 4. NIVEAUX DE PREUVE (P0 → P7, rapport §7)

| Niveau | Signification |
|---|---|
| **P0** | Non vérifié. |
| **P1** | Tests mécaniques passés sur un candidat identifié (hash connu). |
| **P2** | Tests d'ATTAQUE passés (les ATTACK_TESTS de la carte de run). |
| **P3** | Review indépendante du MÊME tree (builder ≠ reviewer, C-5). |
| **P4** | Validation Boss (GO explicite, C-2). |
| **P5** | Promotion (C-7 : tag + snapshot + manifest + hashes + evidence + rollback testé). |
| **P6** | Stable — missions réelles sans défaut bloquant post-approbation. |
| **P7** | GA — checklist complète verte (§15 du rapport). |

**Règle de lecture obligatoire** : « 219/219 tests » (ou tout nombre similaire) prouve **P1 pour CES tests précisément** — ça ne prouve JAMAIS, seul, P2 à P7. Un rapport qui cite un compte de tests verts comme preuve de sécurité, de conformité ou de disponibilité générale viole C-3.

## 5. FRONTIÈRES DE RISQUE (C-4, la liste fermée)

`runtime_integrity` (A0/M1-A) · `cv_security` (CV-SEC/M1-B) · `memory_governance` (A2/M2) · `provider_cascade` (A1/M3) · `product_interface` (M6/M7) · `autonomy` (M10/M11 — nuits, débat). Un run de type carte V4 déclare EXACTEMENT une de ces frontières dans son champ `RISK_BOUNDARY` ; un run qui en touche plusieurs est refusé au format (NOT_READY, §17 du rapport).

## 6. INTERDITS PERMANENTS (rapport §20, formellement retiré)

SUITE-A monolithique · SUITE-B immédiatement après CV-SEC · Chat Era avant les 10 missions réelles (M4) · freeze GA daté · HMAC fichier présenté comme preuve absolue de l'action du Boss · mémoire brute injectée (prose libre en zone d'instruction) · best-of-N séquentiel présenté comme parallèle · coûts proxy présentés comme réels (télémétrie doit dire `UNKNOWN` plutôt que d'inventer) · accès `/pkg/` brut · Apply Assist sur données réelles avant gate CV-SEC · claims « inattaquable » · nuits sans supervision · rapports sans section NON VÉRIFIÉ/LIMITES (D-035) · signature ligne-Markdown seule sans mécanisme (P0-4/MEM §11) · roadmap en prose libre sans IDs d'exigences.

## 7. RÔLES

- **Builder** : produit un candidat (jamais sa propre preuve finale de conformité — C-5, RI-5).
- **Reviewer** : indépendant, contexte gelé, verdict structuré `{candidate_tree, verdict: ACCEPT|P1|BLOCK, findings, reviewer:{provider, model}}` — un verdict sur un hash différent ou en texte libre est rejeté par le contrôleur.
- **Contrôleur** (le runtime, ex. `RunRuntime`) : calcule l'évidence, applique les gates, JAMAIS ne fait confiance à une preuve produite par le builder lui-même.
- **Boss** : voir C-6.

## 8. CE DOCUMENT N'EST PAS ENCORE UNE AUTORITÉ RUNTIME

Conformément à C-2 et à la carte de run M0 (`~/Claude-HQ/JOAO_RUN_CARD_M0_AUTORITE_V4.md`), ce document et `SPEC_INDEX_V4.json` sont produits en statut `CANDIDATE`. Ils deviennent l'autorité (`RUNTIME_AUTHORITY: true`) au moment — et seulement au moment — où le Boss donne un GO explicite (identité + hash + version, C-2) sur leur lecture. Ce GO est demandé dans `BOSS_DECISION` du rapport de ce run — il n'est jamais auto-déclaré par la tour de contrôle (LOI 1, D-038).

## NON VÉRIFIÉ / LIMITES DE CE DOCUMENT
- C'est une **transcription** des §6-7 du rapport V4, pas une réinterprétation : les 7 lois et le vocabulaire sont repris texte-pour-texte quand le rapport les formule explicitement.
- Le vocabulaire de statut des EXIGENCES (§3, deuxième bloc) est une EXTENSION construite par la tour de contrôle — le rapport ne nomme pas ce vocabulaire précis pour les exigences (seulement pour les runs). Signalé pour arbitrage Boss.
- Ce document n'a aucune force d'exécution tant que `RUNTIME_AUTHORITY` n'est pas passé à `true` par un GO Boss explicite et qu'aucun `load_active_specs()` ne le fait respecter dans le code (le loader livré par ce run est un GATE DE LECTURE, §1 du rapport section 5 point 2 — il ne bloque pas encore de mission réelle : cette exécution runtime arrive avec A0/M1-A).
