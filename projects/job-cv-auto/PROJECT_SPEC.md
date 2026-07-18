# PROJECT_SPEC — job-cv-auto · v1.0 (Phase 0 rétroactive) · 2026-07-18
SIGNÉ : ❌ EN ATTENTE

## 1. PRODUIT
CV bot — génère CV + lettre de motivation dérivés d'un master validé, et assiste la candidature.

### Problème tué
Postuler vite et bien sans re-fabriquer la mise en page à chaque offre.

## 2. AUTORITÉS (à valider sur rendu)
CV master = assets/cv-master/Jocelyn_Grosjean_CV_master.docx (sha256 831a9215…, JC-M0, validé Boss). Cover letter master = CoverLetter_master_FINAL_O1v2.docx (sha256 c034573a…, validé Boss).

## 3. RÈGLES BINAIRES
Tout document servi DÉRIVE du master d'autorité par duplicate-and-edit (jamais reconstruit en code) ; le dashboard BLOQUE le téléchargement si le hash de dérivation ne remonte pas au master courant.

## 4. INTERDITS ABSOLUS
Servir un CV non dérivé du master validé (D-042) ; traiter le silence comme un GO design (D-038) ; DOCX→PDF via Pages (aplatit — LibreOffice obligatoire).

## 5. BUILD
Sourcing multi-ATS, resolver découverte→ATS, autofill ; revue humaine du formulaire = filet.

## 6. HORS-SCOPE V1
Package zip en bouton ; premium navy (RETIRED).

## 7. PRE-MORTEM (10 échecs connus → parade)
1. [LOI-1] AUTORITÉ D'ABORD (contract-first). Aucun générateur, aucune UI, aucune feature avant que les artefacts d'autorité (visuels, formats, règles métier) soient VALIDÉS par le propriétaire sur RENDU avec GO explicite. Phase 0 obligatoire de tout projet. Silence ≠ GO.  → parade dans la spec ?
2. [D-042] Tout artefact servi à l'humain dérive du master d'autorité courant par duplicate-and-edit (jamais reconstruit en code = récidive D-021 → alarme rouge) ; la tour de contrôle est soumise aux mêmes règles, silence ≠ GO.  → parade dans la spec ?
3. [D-038] Une référence d'autorité n'existe qu'avec un « GO » EXPLICITE du propriétaire sur le rendu — jamais inférée. Statut actuel du master v2 : PROVISOIRE, en attente du verdict Boss  → parade dans la spec ?
4. [LOI-3] LES GATES JUGENT CONTRE LE STANDARD DU PROPRIÉTAIRE. Side-by-side rendu↔autorité dans l'évidence (L8), section NON VÉRIFIÉ obligatoire (L9), contrôle TECHNIQUE pas déclaratif (hash de dérivation, blocage ⬇). Un ✅ sans preuve = un mensonge.  → parade dans la spec ?
5. [D-034] 🔴🔴 MÉTA-DÉFAUT : les gates valident la MÉCANIQUE (1 page, hash, boutons) mais AUCUNE gate ne compare le rendu au VISUEL APPROUVÉ par le Boss → des ✅ affichés sur un produit jugé « horrible » = sur-déclaration systémique  → parade dans la spec ?
6. [D-031] Tout master visuel passe par une session d'options validées par le Boss AVANT usage  → parade dans la spec ?
7. [D-008] DOCX→PDF via LibreOffice ; jamais valider un PDF sans l'OUVRIR  → parade dans la spec ?
8. [D-007] Template d'autorité + duplicate-and-edit OBLIGATOIRE, jamais de rebuild  → parade dans la spec ?
9. [D-041] Le side-by-side mockup↔render s'applique AUSSI aux masters fabriqués par la tour de contrôle  → parade dans la spec ?
10. [D-033] Exiger le tableau rendement PAR PLATEFORME à chaque run ; la découverte de slugs doit passer par les pages carrières des entreprises (elles révèlent l'ATS)  → parade dans la spec ?

## AVENANTS
_(aucun — spec rétroactive, à signer par le Boss dans l'UI)_
