# PROJECT_SPEC — joao · v1.0 (Phase 0 rétroactive) · 2026-07-18
SIGNÉ : ❌ EN ATTENTE

## 1. PRODUIT
JOÃO — orchestrateur local qui transforme une intention en code livré, review indépendante et validation humaine, à coût minimal (cascade GLM→best-of-N→Claude) et sans jamais mentir.

### Problème tué
Faire livrer des IA de code de façon FIABLE (spec nette + exécution réelle + review croisée + mémoire des erreurs), là où un LLM seul se trompe.

## 2. AUTORITÉS (à valider sur rendu)
UI bubble (rendu validé Boss), rapports de run (section NON VÉRIFIÉ obligatoire).

## 3. RÈGLES BINAIRES
Fail-closed partout ; le modèle réellement utilisé est TOUJOURS affiché ; jamais un PASS inventé ; Phase 0 obligatoire avant toute mission projet.

## 4. INTERDITS ABSOLUS
Commit sur une baseline gelée ; run 22 min sur une salutation ; réponse chat inventée sur un doc illisible ; builder = reviewer sur le chemin critique.

## 5. BUILD
GLM (volume) build, Codex/Claude review adversariale, cascade pour le coût. Missions bornées.

## 6. HORS-SCOPE V1
Recherche web dans le chat (non branchée), génération d'images (budget), débat multi-IA.

## 7. PRE-MORTEM (10 échecs connus → parade)
1. [LOI-3] LES GATES JUGENT CONTRE LE STANDARD DU PROPRIÉTAIRE. Side-by-side rendu↔autorité dans l'évidence (L8), section NON VÉRIFIÉ obligatoire (L9), contrôle TECHNIQUE pas déclaratif (hash de dérivation, blocage ⬇). Un ✅ sans preuve = un mensonge.  → parade dans la spec ?
2. [D-034] 🔴🔴 MÉTA-DÉFAUT : les gates valident la MÉCANIQUE (1 page, hash, boutons) mais AUCUNE gate ne compare le rendu au VISUEL APPROUVÉ par le Boss → des ✅ affichés sur un produit jugé « horrible » = sur-déclaration systémique  → parade dans la spec ?

## AVENANTS
_(aucun — spec rétroactive, à signer par le Boss dans l'UI)_
