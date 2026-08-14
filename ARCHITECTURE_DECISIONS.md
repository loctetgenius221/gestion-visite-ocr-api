# Décisions d'architecture — Backend SIGV

Ce document trace les décisions non arbitrées par `SPEC_BACKEND_SIGV_OCR_MRZ.md`,
conformément à la consigne §11 de la spec. Chaque décision indique son contexte,
le choix retenu et ce qui la ferait reconsidérer.

---

## ADR-001 — Pipeline OCR découpé en trois modules indépendants

**Contexte.** La spec (§4) demande un service isolé et testable `mrz_ocr_service.py`.
Mélanger preprocessing OpenCV, appel PaddleOCR et parsing ICAO dans un seul fichier
rendrait impossible de tester le parsing sans image ni modèle.

**Décision.** Le pipeline est éclaté en quatre fichiers :

| Module | Responsabilité | Testable sans |
|---|---|---|
| `services/image_preprocessing.py` | décodage, deskew, détection de bande MRZ, CLAHE, binarisation | modèle OCR |
| `services/ocr_engine.py` | wrapper PaddleOCR (singleton, normalisation de sortie) | — |
| `services/mrz_parser.py` | nettoyage, détection de format, parsing + checksums ICAO | image **et** modèle |
| `services/mrz_ocr_service.py` | orchestration + persistance de l'image | modèle (injectable) |

`mrz_parser` ne dépend ni d'OpenCV ni de PaddleOCR : ses 22 tests tournent en 0,15 s.
Le moteur OCR est injectable dans `MrzOcrService`, ce qui permet de tester
`POST /ocr/scan` de bout en bout avec un double.

---

## ADR-002 — `BackgroundTasks`/threads plutôt que Celery + Redis

**Contexte.** La spec (§2) laisse le choix entre Celery+Redis et un traitement en
processus, à condition de le justifier.

**Décision.** Pas de Celery, pas de Redis pour l'instant.

**Justification.**

1. **Le scan MRZ est synchrone par nature.** L'agent de contrôle attend le résultat
   à l'écran pour compléter le dossier du visiteur ; une file asynchrone imposerait
   un aller-retour de polling côté Flutter sans aucun gain d'expérience.
2. **Le volume ne le justifie pas.** Un poste d'accueil traite quelques dizaines de
   visites par jour, pas des milliers de documents par minute.
3. **Le traitement direct reste le bon modèle** : le modèle PaddleOCR est chargé une
   fois au démarrage (lifespan), et le travail CPU (OpenCV + inférence) part dans un
   thread via `anyio.to_thread.run_sync` pour ne pas bloquer l'event loop.

⚠️ **Correctif après mesure réelle** : le budget de 3 s de la spec §4.3 **n'est pas
tenu** sur la machine de développement (~5,5 s, entièrement en inférence OCR). Voir
ADR-013 pour la décomposition et les leviers. Cela ne remet pas en cause le choix
d'un traitement synchrone — une file d'attente ajouterait de la latence, pas moins.

**Conséquence sur la blacklist de tokens.** La spec (§5.1) autorise « Redis ou table
dédiée » : sans Redis, la révocation des refresh tokens vit dans la table
`revoked_tokens`, purgeable par `RevokedTokenRepository.purge_expired()`.

**À reconsidérer si** le scan devient un traitement par lots (import d'archives) ou
si plusieurs postes saturent un même worker : Celery se rebranchera sur
`MrzOcrService` sans toucher aux routers, les variables `CELERY_*` sont déjà en place.

---

## ADR-003 — `bcrypt` en direct plutôt que `passlib`

**Contexte.** La spec (§2) impose `passlib[bcrypt]`.

**Décision.** `app/core/security.py` appelle la lib `bcrypt` directement.

**Justification.** `passlib` 1.7.4 (dernière version publiée) est **incompatible avec
`bcrypt` >= 4.1** : sa détection de backend appelle `bcrypt.hashpw` avec une sonde
qui lève `ValueError: password cannot be longer than 72 bytes`. Le projet installe
`bcrypt` 5.0 ; `CryptContext(schemes=["bcrypt"])` échoue donc dès le premier hash.

L'API exposée reste identique (`hash_password` / `verify_password`), l'algorithme est
le même (bcrypt, coût par défaut), et les hashes sont interopérables : un futur retour
à passlib ne nécessiterait aucune migration de mots de passe.

**Note.** La troncature à 72 octets est explicite dans `security.py` plutôt que
silencieuse, et couverte par un test.

---

## ADR-004 — Idempotence de la synchro offline par `client_reference`

**Contexte.** `POST /visits/sync` (§5.3) doit retourner un statut individuel
succès/conflit/erreur par visite. Sans clé d'idempotence, un batch rejoué après une
coupure réseau créerait des doublons silencieux.

**Décision.** `Visit.client_reference` : chaîne unique (indexée) générée par l'app
Flutter au moment de la saisie hors-ligne.

- Référence déjà connue → `status: "conflict"`, `error_code: DUPLICATE_VISIT`, aucune insertion.
- Champ absent → la visite est créée sans garantie d'idempotence (compatible avec un
  `POST /visits` classique en ligne).

Chaque item est **commité indépendamment** : une référence invalide au milieu du batch
ne fait pas perdre les visites valides qui l'entourent. La réponse HTTP globale reste
`200`, le détail par item étant porté par le corps.

**Réponse à la question ouverte n°2 de la spec.** `/visits/sync` est livré en V1 : le
coût marginal était faible une fois `create_visit` factorisé, et le mode hors-ligne
est structurant pour un poste d'accueil dont la connectivité n'est pas garantie.

---

## ADR-005 — Le NIN n'est pas dans le MRZ ; le numéro de carte y déborde

**Contexte.** Question ouverte n°1 de la spec : le NIN est-il encodé dans le champ
`numero_document` du MRZ ?

**Statut. ✅ Tranché sur une CNI CEDEAO sénégalaise réelle** (2026-08-05). L'hypothèse
initiale — NIN porté par `optional_data` — est **infirmée**.

### Ce que contient réellement le MRZ

MRZ observé (structure ; valeurs remplacées ici par celles du jeu de test) :

```
I<SEN200998877<665544332<<<<<<     ← type, pays, n° carte (9), '<', débordement + checksum
9503124F3301018SEN<<<<<<<<<<<2     ← naissance, sexe, expiration, nationalité, composite
NDIAYE<<FATOU<<<<<<<<<<<<<<<<<     ← nom << prénoms
```

Deux constats :

1. **Le numéro de carte fait 17 chiffres**, alors que le champ `document_number` du
   TD1 en accepte **9**. La norme ICAO 9303 prévoit ce débordement : les 9 premiers
   caractères restent dans le champ, un `<` remplace le chiffre de contrôle pour
   signaler la continuation, et **le reste du numéro suivi du checksum du numéro
   entier** ouvre la zone `optional_data`, terminée par un `<`.
2. **Le NIN est totalement absent du MRZ.** Il est imprimé au verso, juste au-dessus
   de la bande MRZ, et ne peut donc pas être extrait par lecture du MRZ.

### Décisions

| Champ | Source | Comportement |
|---|---|---|
| `numero_document` | MRZ, **numéro complet reconstitué** (9 premiers + débordement) | extrait automatiquement |
| `nin` (réponse OCR) | **zone imprimée** lue au-dessus du MRZ | extrait automatiquement (voir ADR-014) |
| `Visitor.nin` (base) | réponse OCR, corrigeable par l'agent | colonne nullable indexée, migration `5f1c5bd2957b` |

`_resolve_document_number` dans `mrz_parser.py` implémente la reconstitution et
**recalcule lui-même le checksum** sur le numéro entier : la lib `mrz` ne connaît pas
cette convention et compare le `<` au chiffre attendu, ce qui invalidait à tort
**toute** CNI sénégalaise (`mrz_valid: false` sur une carte parfaitement authentique).

### Conséquence sur la spec

La spec §3 décrit `numero_document` comme « NIN pour CNI, n° passeport sinon ». C'est
factuellement impossible : ce sont deux numéros distincts, et seul le numéro de carte
est lisible dans le MRZ. D'où l'ajout d'une colonne `nin` séparée plutôt que de
détourner `numero_document` de son sens.

Le NIN reste dans un champ distinct : il n'est **pas** dans le MRZ, mais il est
récupérable par OCR de la zone imprimée (ADR-014), donc sans saisie manuelle.

### Données personnelles

Les cartes réelles ayant servi à la validation **ne sont pas versionnées**
(`storage/` est ignoré par git). Le jeu de test (`CNI_SEN_LINES` dans
`tests/unit/test_mrz_parser.py`) reproduit la structure à l'identique — débordement,
checksums cohérents — avec des valeurs fabriquées.

---

## ADR-006 — `/auth/forgot-password` livré en stub assumé

**Contexte.** La spec (§5.1) accepte un stub si aucun service mail n'est configuré,
à condition de le documenter.

**Décision.** La route accepte la demande, la trace dans les logs applicatifs et
répond `202 Accepted` — **sans révéler si le compte existe** (réponse strictement
identique pour un identifiant connu et inconnu, ce qui évite une énumération de comptes).

Aucun mail, SMS ni token de réinitialisation n'est émis à ce stade. Le branchement
d'un fournisseur d'envoi se fera dans `routers/auth.py::forgot_password`.

**Contexte d'usage.** Le nombre d'agents de contrôle est faible et l'administrateur
est joignable directement : une réinitialisation manuelle est acceptable en V1.

---

## ADR-007 — SQLite en mémoire pour les tests

**Contexte.** La spec (§8) laisse le choix entre SQLite en mémoire et un conteneur
PostgreSQL de test, à condition de documenter.

**Décision.** SQLite en mémoire (`aiosqlite`, `StaticPool`) pour la suite de tests ;
PostgreSQL reste la cible d'exécution et a été validé de bout en bout (migrations,
seed, parcours API complet) sur PostgreSQL 18.1.

**Justification.** Aucun service externe à démarrer, suite complète en ~100 s dont
l'essentiel est le coût (volontaire) de bcrypt. Le code applicatif n'utilise aucune
fonctionnalité spécifique à PostgreSQL : les types sont génériques (`sa.Uuid`,
`sa.Enum`), et le calcul des durées de visite du dashboard est fait en Python
précisément parce que les fonctions d'intervalle diffèrent entre les deux moteurs.

**Garde-fou.** `tests/integration/test_migrations.py` applique les migrations Alembic
sur une base vierge et compare le résultat au `metadata` des modèles : un modèle
modifié sans migration correspondante fait échouer la suite.

**Limite acceptée.** Les contraintes `ondelete` et le comportement transactionnel de
PostgreSQL ne sont pas exercés. À reconsidérer (via `testcontainers`) le jour où une
requête spécifique à PostgreSQL apparaîtra.

---

## ADR-008 — `422` réservé aux échecs métier, `400` aux payloads invalides

**Contexte.** FastAPI renvoie `422` par défaut sur une erreur de validation Pydantic,
alors que la spec (§5.6) réserve `422` aux « échecs métier type MRZ non détecté » et
attribue `400` aux requêtes malformées.

**Décision.** Le handler `RequestValidationError` est surchargé pour renvoyer `400`
avec `error_code: VALIDATION_ERROR` et le détail des champs fautifs. `422` n'est plus
émis que par les erreurs métier (`MRZ_NOT_DETECTED`, `MRZ_PARSING_FAILED`).

**Conséquence.** Toutes les erreurs sortantes — y compris celles levées par Starlette
et les exceptions non gérées — passent par un handler qui produit le format
`{error_code, message, details}`. Aucune stacktrace n'atteint le client.

---

## ADR-009 — Détection de la bande MRZ tolérante, avec repli

**Contexte.** La spec (§4.1) demande de préciser si l'UI Flutter impose un cadre de
capture fixe, ce qui simplifierait la détection de zone.

**Décision.** La détection est implémentée **sans faire d'hypothèse sur le cadrage**,
avec un repli qui couvre le cas guidé :

1. Morphologie (blackhat + gradient Sobel + fermeture horizontale) pour isoler les
   blobs très allongés dans la moitié basse du document.
2. Les blobs retenus sont **fusionnés par union** : chaque ligne du MRZ ressort
   souvent comme un blob distinct, et n'en garder qu'un seul amputerait le MRZ d'une
   à deux lignes.
3. Si aucun blob ne convient → repli sur la bande basse du document
   (`OCR_MRZ_BAND_TOP_RATIO`, réglable par variable d'environnement).

**Réponse à la question ouverte n°4.** Si l'app Flutter impose effectivement un cadre
de capture fixe, le repli suffit à lui seul : il suffira de fixer
`OCR_MRZ_BAND_TOP_RATIO` à la valeur du cadre. Aucune modification de code n'est
nécessaire dans un sens comme dans l'autre.

**Robustesse OCR.** Les corrections de confusion `O`/`0`, `I`/`1` sont appliquées
**par position**, en s'appuyant sur la norme : seules les zones strictement numériques
(dates, checksums) ou strictement alphabétiques (codes pays) sont retouchées. Le
numéro de document, qui peut légitimement être alphanumérique, n'est jamais modifié.

---

## ADR-012 — Rattrapage de l'orientation de la photo

**Contexte.** Une photo réelle de CNI (2026-08-05) est arrivée **pivotée de 90°** :
la carte, au format paysage, avait été photographiée en tenant le téléphone en
portrait. Le pipeline échouait intégralement — `deskew` ne corrige que ±20°, et
`detect_mrz_region` ne cherche que des bandes **horizontales**. Les métadonnées EXIF
ne sont pas exploitables : elles disparaissent dès que l'app mobile recadre l'image.

**Décision.** `preprocess_candidates` prépare l'image dans les **quatre quarts de
tour** ; `MrzOcrService` les essaie dans l'ordre et s'arrête au premier MRZ qui se
parse. C'est le parsing qui arbitre : des checksums ICAO qui se recomposent ne
peuvent pas être le fruit du hasard, l'orientation retenue est donc forcément la
bonne.

**Ordre des candidats.** Les orientations où une bande MRZ est effectivement
localisée passent en tête, les autres suivent en repli. Les replis sont **conservés**
et non écartés : la détection morphologique produit des faux positifs (une texture de
fond peut passer pour une bande), et s'arrêter aux seules orientations « détectées »
ferait rater la bonne.

**Coût.** Nul dans le cas nominal : une photo droite est reconnue à la première passe
OCR (test `test_une_photo_droite_ne_coute_quune_passe_ocr`). Une photo pivotée coûte
une à deux passes supplémentaires ; le plafond de quatre n'est atteint que sur une
image sans MRZ, qui part de toute façon en `422`.

---

## ADR-010 — Un visiteur unique par (type de document, numéro de document)

**Contexte.** Non tranché par la spec : faut-il créer une fiche visiteur par passage ?

**Décision.** `POST /visits` réutilise la fiche existante correspondant au couple
(`type_document`, `numero_document`) et met à jour ses coordonnées avec les valeurs
non nulles du payload.

**Justification.** Un même visiteur revient régulièrement au poste d'accueil.
Dupliquer sa fiche à chaque passage fausserait la recherche (`GET /visits?search=`)
et empêcherait toute lecture de son historique. Les valeurs `null` envoyées par le
client n'écrasent jamais une donnée déjà connue.

---

## ADR-011 — Rôles présents en base, non appliqués en permissions

**Contexte.** Question ouverte n°3 de la spec.

**Décision.** L'enum `UserRole` (`AGENT_CONTROLE`, `SUPERVISEUR`, `ADMIN`) existe en
base et le rôle est porté par le JWT (claim `role`), mais **aucune route n'est
restreinte** sur un rôle, conformément à la consigne §3 de la spec.

Toutes les routes métier exigent en revanche une authentification valide. Le jour où
des permissions fines seront nécessaires, le claim est déjà présent dans le token :
il suffira d'ajouter une dépendance `require_role(...)` sur les routes concernées,
sans migration de schéma.

---

## ADR-013 — Réglages PaddleOCR imposés par la validation sur photo réelle

**Contexte.** Le pipeline n'avait jamais tourné sur une vraie photo : le moteur OCR
était doublé dans tous les tests. Le premier essai sur une photo de CNI a révélé
trois problèmes qu'aucun test ne pouvait exposer.

### 1. PaddleOCR refuse les images à 2 dimensions

Le preprocessing produit du niveau de gris binarisé (tableau 2D), alors que
`predict()` exige 3 canaux — sinon `ValueError: not enough values to unpack`.
`ocr_engine.to_bgr()` fait la conversion, et un test la verrouille.

### 2. Le backend oneDNN est cassé sur cette plateforme

Toute inférence échouait sur :

```
NotImplementedError: (Unimplemented) ConvertPirAttribute2RuntimeAttribute
not support [pir::ArrayAttribute<pir::DoubleAttribute>]
```

Reproduit sur **toutes** les tailles d'image : ce n'est pas un problème de forme mais
un défaut du build PaddlePaddle 3.3 sous Windows. D'où `OCR_ENABLE_MKLDNN=false` par
défaut, réactivable par variable d'environnement si un build corrigé est déployé.

### 3. Modèles « mobile » plutôt que « medium »

Mesuré sur la photo réelle, à qualité de lecture MRZ **strictement identique** :

| Modèles | Latence OCR |
|---|---|
| PP-OCRv6_medium (défaut) | ~16 s |
| **PP-OCRv5_mobile** (retenu) | **~5,5 s** |

Piloté par `OCR_DET_MODEL` / `OCR_REC_MODEL`.

⚠️ **Ne pas forcer `text_det_limit_side_len`** : contre-intuitivement, le fixer
dégrade fortement la latence (jusqu'à 28 s), le paramètre agissant comme une borne
*minimale* qui réagrandit l'image. La valeur par défaut est la plus rapide.

### Budget de 3 s (spec §4.3) : non tenu sur cette machine

Décomposition mesurée, photo 4032×3024 :

| Étape | Durée |
|---|---|
| décodage + redimensionnement | 0,25 s |
| détection + redressement de la carte | 0,03 s |
| extraction de la bande basse | 0,03 s |
| **inférence OCR** | **~5,5 s** |

Le preprocessing est négligeable : **tout le coût est dans l'inférence**, et il ne
dépend quasiment pas de la taille de l'image (5,5 s de 800 à 1600 px de large) —
c'est un coût fixe de modèle. Les leviers restants sont donc externes :

- `OCR_USE_GPU=true` ;
- un build PaddlePaddle où oneDNN fonctionne (l'accélération CPU manque aujourd'hui) ;
- le cadre de capture guidé prévu côté Flutter, qui permettrait de sauter la
  détection de carte mais ne réduirait pas l'inférence.

---

## ADR-014 — Détection de la carte, puis lecture NIN + MRZ en une passe

**Contexte.** Sur une photo réelle, la carte n'occupe qu'un tiers du cadre, sur fond
de bois. La détection morphologique du MRZ, calée sur la largeur de l'**image**,
tronquait la 3e ligne du MRZ. Par ailleurs le NIN doit être extrait automatiquement :
sa saisie manuelle par l'agent est exclue.

### Détection et redressement de la carte

1. Seuillage d'Otsu — une CNI est un rectangle clair sur un fond plus sombre.
2. Plus grand contour à 4 sommets, occupant entre 10 % et 97 % de l'image.
3. **Contrôle des proportions ID-1** (85,6 × 54 mm, ratio 1,585, tolérance 1,35–1,85).
   Sans ce garde-fou, le cadre de la photo ou une feuille posée à côté seraient pris
   pour la carte — observé pendant la mise au point.
4. Correction de perspective vers un cadre canonique de 1300 px de large.

Tout devient alors exprimable en **ratios de la hauteur de carte**, indépendamment du
cadrage, de la distance et de l'angle de prise de vue.

### Une seule passe OCR pour le NIN et le MRZ

Le NIN est imprimé juste **au-dessus** du MRZ. Plutôt que deux passes (une par zone,
soit le double de la latence), une bande unique part de 20 % au-dessus du MRZ détecté
et descend **jusqu'au bord bas de la carte**.

Ce prolongement jusqu'au bord est indispensable : la bande morphologique s'arrête au
dernier trait détecté et amputait systématiquement la ligne des noms.

Sortie OCR réelle obtenue, en une passe :

```
NIN1 895 2003 00511
I<SEN101200302<010005582<<<<<<
0302014M3110061SEN<<<<<<<<<<<8
COLY<<PAPE<SOULEYMANE<<<<<<<<<
```

`extract_nin` récupère le NIN par libellé (l'OCR colle souvent `NIN` au premier
chiffre), avec repli sur une ligne de 13 chiffres quasi exclusivement numérique. Les
lignes imprimées ne polluent pas le MRZ : le filtre de `build_candidate` les écarte,
faute de remplisseur `<` et de longueur suffisante.

### Correctif du 2026-08-07 — le champ NIN affichait le numéro de carte

**Symptôme.** En production, après quelques scans réussis, le champ NIN de l'app
mobile s'est mis à afficher le numéro de document, sur plusieurs cartes différentes.

**Cause.** `extract_nin` renvoyait `null` bien trop souvent, et le client comblait
le vide par `numero_document`. Cinq modes de défaillance, reproduits en test :

| Sortie OCR | Ancien résultat |
|---|---|
| `N° 1 895 2003 00511` | `null` — « N° » dépassait le budget d'un caractère non numérique |
| `NIN 1 89S 2003 0O511` | `null` — confusions `S`/`5` et `O`/`0` non corrigées |
| `NIN … CARTE …` sur une ligne | `null` — 30 chiffres au lieu de 13 |
| Libellé et chiffres sur deux lignes | `null` — recherche ligne par ligne |
| Numéro de carte tronqué à 13 chiffres | **le numéro de carte**, renvoyé comme NIN |

La dernière ligne est la plus grave : un identifiant faux mais parfaitement
crédible, impossible à repérer à l'œil.

**Décisions.**

1. **Le numéro de document est passé en garde-fou.** Tout candidat qui est un
   fragment du numéro de carte — ou l'inverse — est rejeté. Le numéro sénégalais
   fait 17 chiffres et vit sur la même zone imprimée que le NIN : sans ce
   contrôle, sa troncature par l'OCR produit un faux NIN.
2. **Les lignes de MRZ sont écartées de la recherche.** Corriger les confusions
   OCR sur un MRZ produit une longue suite de chiffres où n'importe quel groupe
   de 13 se laisserait prendre pour un NIN.
3. **Les confusions OCR sont corrigées sur les suites candidates uniquement**,
   jamais sur le texte entier : un `O` reste un `O` dans un nom de famille.
4. **La marge au-dessus du MRZ passe de 10 % à 20 %.** À 10 %, sur une carte
   redressée de 820 px, il ne restait que ~82 px au-dessus du MRZ — à peine une
   ligne de texte. Selon le cadrage, la ligne NIN tombait hors de la zone envoyée
   à l'OCR.

**Côté client.** Un repli sur `numero_document` quand `nin` est nul est à retirer :
il transforme une donnée absente en donnée fausse. `nin: null` doit rester visible
comme tel, à charge pour l'agent de le saisir.

### Effet de bord : moins d'orientations à tester

La carte redressée est ramenée en paysage, donc l'ADR-012 se simplifie quand elle est
détectée : **2 candidats** (endroit / à l'envers) au lieu de 4. Le repli sur les
quatre quarts de tour ne subsiste que si aucune carte n'est localisée.

---

## ADR-015 — Session longue et glissante, jamais éternelle

**Contexte.** L'agent du poste d'accueil est présent toute la journée et les
visites s'enchaînent. Une reconnexion hebdomadaire — durée initiale du refresh
token — est une friction inutile, et la demande était d'aller vers « jamais, sauf
déconnexion volontaire ».

### Ce qui produit réellement la fatigue

Il faut distinguer deux horloges, souvent confondues :

| Jeton | Durée | Visible par l'agent ? |
|---|---|---|
| `access` | 30 min | **Non**, si le client le renouvelle en silence |
| `refresh` | 30 jours | Oui : à son expiration, écran de connexion |

Un agent qui se reconnecte plusieurs fois par jour ne souffre donc pas d'une durée
trop courte, mais d'un **client sans intercepteur de rafraîchissement**. Allonger
les durées ne corrigerait rien. Le diagnostic se fait par le journal d'audit :
`GET /api/v1/audit-logs?action=auth.login.success` — plus d'une connexion par
agent et par mois signale un problème côté client.

### Décision : glissement plutôt qu'éternité

1. `REFRESH_TOKEN_EXPIRE_DAYS` passe de **7 à 30 jours**.
2. **Renouvellement glissant** : passé la moitié de sa vie, le refresh token est
   remplacé lors d'un rafraîchissement, et le nouveau est renvoyé dans un champ
   `refresh_token` de la réponse de `POST /auth/refresh`.

Effet recherché : un appareil qui sert quotidiennement voit sa session repoussée
indéfiniment — l'agent n'est jamais déconnecté. Un appareil qui cesse d'être
utilisé voit sa session mourir seule au bout de 30 jours.

### Pourquoi pas « jamais »

Un jeton sans expiration sur une tablette de poste d'accueil est une **clé
permanente** vers des pièces d'identité. Perdue ou remplacée, elle reste valable
indéfiniment, et plus personne ne se souvient qu'elle existe. L'expiration par
inactivité est le seul mécanisme qui nettoie sans intervention humaine.

Le glissement donne le confort demandé sans ce défaut : l'usage entretient la
session, l'abandon la referme.

### Ce qui coupe une session, malgré le glissement

Déconnexion volontaire · révocation à distance par un administrateur
(`DELETE /users/{id}/sessions/{sessionId}`) · désactivation du compte ·
réinitialisation du mot de passe. Ces quatre chemins sont couverts par des tests.

### Rétro-compatibilité et limite assumée

Le champ `refresh_token` de la réponse est **additif**, et l'ancien jeton **n'est
pas révoqué** lors du glissement : un client qui ignore le champ continue de
fonctionner jusqu'à l'expiration de son jeton. Sans cela, la mise en production
aurait déconnecté toutes les tablettes.

Contrepartie : un refresh token volé reste utilisable jusqu'à son terme, même
après glissement. La rotation stricte — révocation immédiate de l'ancien jeton, et
détection de vol par réutilisation — deviendra possible une fois que l'application
mobile enregistrera systématiquement le jeton renvoyé. À reprendre à ce
moment-là.

### Note d'implémentation

Le glissement réécrit le `jti` **sur la ligne de session existante** plutôt que
d'en créer une nouvelle. Une ligne par rafraîchissement ferait enfler la table —
un poste rafraîchit toutes les 30 minutes — et noierait la liste des sessions
actives du dashboard sous des doublons du même appareil.

---

## ADR-016 — Le NIN est alphanumérique, et se lit par blocs

**Contexte.** L'ADR-014 lisait le NIN comme une suite de **13 chiffres**. Une carte
réelle (2026-08-14) porte `NIN 2 K05 2012 00108` : le code d'état civil contient une
lettre. L'hypothèse « 13 chiffres » est donc **infirmée**.

### Deux défaillances, dont une silencieuse et une dangereuse

| Sortie OCR | Ancien résultat | Pourquoi |
|---|---|---|
| `NIN 2 K05 2012 00108` | `null` | `K` n'a pas d'équivalent numérique : il était **supprimé**, il ne restait que 12 chiffres |
| `NIN 2 D05 2012 00108` | `2005201200108` | `D` est une confusion OCR connue : traduit en `0`, le compte retombait à 13 — **NIN faux mais crédible** |

Le second cas est celui que le garde-fou du numéro de carte (ADR-014) cherchait
déjà à éviter, réapparu par un autre chemin. Un NIN faux ne se repère pas à l'œil.

### Structure retenue

```
2   K05   2012   00108
│    │      │      └── numéro d'ordre séquentiel, 5 chiffres
│    │      └───────── année d'enregistrement, 4 chiffres
│    └──────────────── code d'état civil (commune de déclaration),
│                      3 caractères **alphanumériques**
└───────────────────── sexe : 1 (homme) ou 2 (femme)
```

`_lire_nin` valide chaque bloc séparément et corrige les confusions OCR **vers le
chiffre**, y compris sur le code d'état civil. Une lettre sans équivalent numérique
(`K`, `M`, `R`…) y survit donc telle quelle ; un `S` lu pour un `5` est redressé.

### Limite assumée

Un code d'état civil commençant réellement par `D`, `O`, `S`, `B`, `Z`, `G`, `I`,
`L` ou `Q` sera numérisé à tort. Sans référentiel des communes, **rien ne départage
les deux lectures** : on tranche pour la confusion OCR, incomparablement plus
fréquente qu'une commune dont le code débute par l'une de ces neuf lettres. Le
comportement est figé par un test, pour qu'il reste un choix et non une surprise.

Le jour où le référentiel des codes d'état civil est disponible, il remplace cette
heuristique par une vérification exacte — c'est le bon moment pour rouvrir l'ADR.

### Ce qui remplace le comptage de chiffres

La validation par blocs est plus stricte que « 13 chiffres », mais pas suffisante à
elle seule : un numéro de carte de 17 chiffres contient, en position 4, treize
caractères structurellement valides. Deux contrôles la complètent :

1. **Le bruit autour du NIN est borné à 2 caractères** de chaque côté sur une ligne
   sans libellé — de quoi absorber un préfixe « N° », pas de quoi laisser une ligne
   bavarde offrir treize caractères pris en son milieu. C'est ce bornage qui écarte
   le numéro de carte, désormais **sans dépendre du garde-fou**.
2. **L'année doit être passée** (`1900 ≤ année ≤ année courante`). C'est le contrôle
   le plus discriminant : les découpages parasites produisent presque toujours une
   année absurde (`0030`, `3020`).

Le garde-fou du numéro de document (ADR-014) subsiste et compare désormais des
caractères **alphanumériques** : un NIN portant une lettre ne peut plus être déclaré
fragment d'un numéro de carte purement numérique.

### Normalisation en entrée

`VisitorInput.nin` retire séparateurs et espaces et passe en majuscules. Sans cela,
un NIN saisi à la main (`2 K05 2012 00108`) et le même NIN lu par l'OCR
(`2K05201200108`) formeraient deux valeurs distinctes dans une colonne indexée et
destinée à la recherche.

---

## ADR-017 — Réenregistrer un visiteur connu sans rescanner sa pièce

**Contexte.** Un visiteur régulier faisait rescanner sa pièce à chaque venue, alors
que sa fiche existe déjà : `_upsert_visitor` déduplique sur le couple
`(type_document, numero_document)` depuis l'ADR-010. Ce qui manquait n'était pas la
déduplication, mais le moyen de **retrouver** la fiche : `GET /visits?search=`
cherche des visites, et `POST /visits` exigeait le bloc `visitor` complet.

### Deux ajouts

| Route | Rôle |
|---|---|
| `GET /visitors?search=` | Retrouve une fiche par nom, prénom, n° de document ou NIN |
| `POST /visits` avec `visitor_id` | Enregistre la visite à partir de cette fiche |

`visitor` et `visitor_id` sont **exclusifs**. Accepter les deux obligerait à trancher
un désaccord entre la fiche référencée et l'identité fournie — un arbitrage qu'aucune
règle ne rend évident, et qui finirait par inscrire une identité erronée au registre.

La recherche renvoie deux champs calculés dans la **même requête** que les fiches —
`derniere_visite_at` et `visite_ouverte_id`. Les calculer fiche par fiche produirait
un N+1 sur une route appelée à chaque frappe de l'agent.

### Un visiteur déjà présent ne peut pas entrer une seconde fois

`find_open_visit` existait depuis l'origine sans être appelé nulle part : deux visites
`PRESENT` pouvaient coexister pour la même personne. Le rescan de la pièce freinait
naturellement la double saisie ; en reprenant une fiche connue, elle ne coûte plus que
deux gestes et **va se produire**.

Décision : `409 VISITOR_ALREADY_PRESENT`, avec la visite ouverte et son heure d'entrée
dans les `details`. Le client propose « clôturer puis réenregistrer » au lieu
d'afficher une impasse, et `visite_ouverte_id` lui permet même d'anticiper sans
attendre l'erreur.

Les deux alternatives ont été écartées :

- **Accepter en signalant** — le compteur des présents du dashboard devient faux, et
  rien ne nettoie jamais les visites fantômes.
- **Clôturer automatiquement l'ancienne** — inscrit au registre une heure de sortie
  qui n'a jamais été constatée. Sur un registre destiné à l'audit, c'est une donnée
  inventée.

Contrepartie assumée : si un visiteur est parti sans être clôturé, l'agent doit
d'abord fermer l'ancienne visite. Un tap — et c'est précisément ce qui purge les
visites restées ouvertes.

**Effet sur la synchro hors-ligne.** Un batch contenant deux entrées pour la même
personne voit la seconde remonter en `conflict`, avec son `error_code`. C'est le
comportement voulu, mais il découle d'une limite préexistante : `POST /visits/sync`
ne transporte que des **entrées**, jamais les sorties saisies hors-ligne. Tant que
c'est le cas, un aller-retour dans la même journée hors connexion ne se rejouera pas
tel quel. À traiter quand la synchro des clôtures sera spécifiée.

### Champs de passage : surcharge plutôt que migration

`provenance`, `immatriculation_vehicule` et `telephone` vivent sur `Visitor` alors
qu'ils décrivent le **passage** : reprendre une fiche telle quelle recopierait en
silence la plaque du véhicule d'il y a trois mois. Le bloc `visitor_passage` les
rafraîchit au moment de l'enregistrement.

C'est un compromis, pas la modélisation juste. Celle-ci porterait ces champs sur
`Visit`, avec une migration et une reprise des données existantes ; la surcharge est
livrable immédiatement et couvre le besoin réel — que la visite du jour porte les
bonnes informations. **Limite connue :** la fiche ne garde que la dernière valeur,
l'historique par visite n'est pas reconstituable. Le jour où on veut savoir avec quel
véhicule quelqu'un est venu en mars, il faut faire la migration.

### Données personnelles de la recherche

La recherche expose de l'identité, et pas uniquement aux administrateurs — l'agent
d'accueil en a besoin. Trois garde-fous :

1. **Trois caractères minimum** : sans eux, une seule lettre parcourt le fichier
   entier. Un terme réduit à de la ponctuation est également neutralisé, sans quoi il
   deviendrait `%%` sur les colonnes de numéros.
2. **Pagination bornée** à 100 entrées par page, comme le reste de l'API.
3. **Trace applicative** de chaque recherche (acteur, nombre de résultats), sans le
   terme cherché. Le journal d'audit n'est pas alimenté : une entrée par frappe le
   rendrait illisible pour ce qu'il sert vraiment, les écritures. En revanche
   `visit.created` porte désormais `visiteur_reutilise`, qui dit si la visite a été
   enregistrée **sans rescan** de la pièce.

---

## ADR-018 — Photos recto et verso, et durée de vie des images de pièces

**Contexte.** Le scan MRZ ne lit qu'une face : le **verso** d'une CNI sénégalaise,
celle qui porte la bande MRZ. C'est suffisant quand l'OCR réussit. Quand il échoue,
l'agent saisit l'identité à la main — et il n'avait alors **aucun moyen de déposer
une photo de la pièce** : `/uploads/signature` était le seul endroit où poser un
fichier. La seule justification de ce qui venait d'être saisi à la main manquait.

### Deux colonnes, un endpoint

| Ajout | Rôle |
|---|---|
| `POST /uploads/document?face=recto\|verso` | Dépose une face, renvoie son URL |
| `Visitor.document_recto_url` / `document_verso_url` | Portent ces URLs |

Le recto n'est pas un doublon du verso : il porte la photo du titulaire et des
mentions absentes de l'autre face. Sans lui, une identité saisie à la main ne
s'appuie sur rien de vérifiable.

Les faces sont rangées séparément (`documents/recto/`, `documents/verso/`) : la
purge et toute inspection ultérieure y gagnent, pour un coût nul.

### `mrz_image_url` reste, et alimente `document_verso_url`

L'ancienne colonne désigne exactement la même face que `document_verso_url`. La
migration `b4e2af8c1d93` recopie les valeurs existantes, et `VisitorInput` **reporte**
la valeur reçue sur le nouveau champ quand celui-ci est absent.

Elle n'est pas supprimée : les tablettes déjà déployées l'envoient. Sans ce report,
un client resté sur l'ancien champ verrait le verso disparaître de la nouvelle
colonne — et la purge raisonnerait sur une donnée incomplète. À retirer une fois
l'app mobile basculée.

### Conservation : un réglage et une commande, pas une tâche de fond

Une photo de CNI identifie complètement une personne. La conserver indéfiniment n'a
aucune justification métier passé un délai — et c'est le premier point qu'une
autorité de protection des données regarde.

| Élément | Choix |
|---|---|
| Durée | `document_images_retention_days`, 365 jours par défaut, `0` désactive |
| Référence | La **dernière visite** du visiteur, pas la date de la photo |
| Déclenchement | `python -m app.purge_documents`, sur timer systemd |
| Portée | Les images **seules** |

**La date de référence est la dernière venue** : quelqu'un qui revient chaque mois
garde ses images, quelqu'un qui n'est plus venu depuis deux ans les perd. Compter
depuis la date de la photo effacerait la pièce d'un visiteur régulier.

**Seules les images partent.** Visiteurs, visites et journal d'audit survivent : le
registre doit pouvoir dire qui est venu, quand et voir qui, des années après que la
photo a été effacée. Les signatures ne sont pas purgées non plus — elles attestent
du passage lui-même et appartiennent au registre, pas à la pièce d'identité.

**Pourquoi pas une tâche de fond interne.** Elle s'exécuterait une fois par worker
uvicorn, et son échec ne serait visible de personne. Un timer systemd laisse une
trace dans journald, se teste à la main (`--dry-run`) et se désactive sans
redéploiement — cohérent avec le déploiement natif déjà en place. Chaque exécution
écrit en plus une entrée `visitor.documents_purged` au journal d'audit : une
suppression de données doit rester défendable après coup.

### Ce que ça ne règle pas

Nginx sert `/storage/uploads/` **sans authentification** (voir DEPLOYMENT.md §8) :
les noms de fichiers sont des UUID v4 non devinables, mais quiconque obtient une URL
accède à l'image. Doubler le volume d'images de pièces double d'autant l'exposition.
Le passage à `X-Accel-Redirect` derrière le bearer token devient plus urgent qu'il
ne l'était — c'est un changement de contrat côté client, à planifier.
