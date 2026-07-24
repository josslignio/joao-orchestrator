# PROJECT_SPEC — weekly-trading-radar · v1.0 (Phase 0 rétroactive) · 2026-07-18
SIGNÉ : ❌ EN ATTENTE

## 1. PRODUIT
Radar de trading hebdomadaire — synthèse de signaux de marché.

### Problème tué
Produire une revue hebdo reproductible sans travail manuel.

## 2. AUTORITÉS (à valider sur rendu)
Source immuable : git@github.com:josslignio/weekly-trading-radar.git@c8d8390c8f76 (non réécrite).

## 3. RÈGLES BINAIRES
Données sensibles jamais commitées ; sorties reproductibles ; append-only sur les historiques.

## 4. INTERDITS ABSOLUS
Réécrire l'historique source ; inventer un signal non calculé.

## 5. BUILD
Pipeline data → indicateurs → rapport ; environnement à figer.

## 6. HORS-SCOPE V1
Exécution d'ordres réels ; conseil financier.

## 7. PRE-MORTEM (10 échecs connus → parade)
1. [D-042] Tout artefact servi à l'humain dérive du master d'autorité courant par duplicate-and-edit (jamais reconstruit en code = récidive D-021 → alarme rouge) ; la tour de contrôle est soumise aux mêmes règles, silence ≠ GO.  → parade dans la spec ?
2. [D-038] Une référence d'autorité n'existe qu'avec un « GO » EXPLICITE du propriétaire sur le rendu — jamais inférée. Statut actuel du master v2 : PROVISOIRE, en attente du verdict Boss  → parade dans la spec ?
3. [D-035] Tout rapport final DOIT contenir la section « NON VÉRIFIÉ / LIMITES » — l'absence de cette section invalide le rapport  → parade dans la spec ?
4. [D-034] 🔴🔴 MÉTA-DÉFAUT : les gates valident la MÉCANIQUE (1 page, hash, boutons) mais AUCUNE gate ne compare le rendu au VISUEL APPROUVÉ par le Boss → des ✅ affichés sur un produit jugé « horrible » = sur-déclaration systémique  → parade dans la spec ?
5. [D-031] Tout master visuel passe par une session d'options validées par le Boss AVANT usage  → parade dans la spec ?
6. [D-029] La rétro doit LIRE le ledger central avant de numéroter — à intégrer dans B-28 (ids attribués par le système, pas par le run)  → parade dans la spec ?
7. [D-017] Les demandes récurrentes de l'humain en test = features à spec'er immédiatement  → parade dans la spec ?
8. [D-013] Volume = plus de sources ; bucket to_resolve doit se VIDER  → parade dans la spec ?
9. [D-009] Placeholder inconnu = VIDE + surligné, jamais deviné  → parade dans la spec ?
10. [D-008] DOCX→PDF via LibreOffice ; jamais valider un PDF sans l'OUVRIR  → parade dans la spec ?

## AVENANTS
_(aucun — spec rétroactive, à signer par le Boss dans l'UI)_
