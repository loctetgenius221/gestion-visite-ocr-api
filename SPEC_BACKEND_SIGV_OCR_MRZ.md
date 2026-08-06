# Spécification technique — Backend API SIGV (Système Intelligent de Gestion des Visites)

> **Note pour Claude Code** : Ce document est une spécification complète destinée à guider le développement du backend depuis zéro. Lis-le intégralement avant d'écrire la moindre ligne de code. Si un point te semble ambigu ou contradictoire, pose la question avant de faire une hypothèse silencieuse qui pourrait diverger de l'architecture attendue. Procède ensuite de manière incrémentale en suivant le plan de développement en section 11.

---

## 1. Contexte du projet

SIGV est une application de contrôle d'accès pour un poste d'accueil (Ministère de la Fonction Publique, Sénégal). Un **agent de contrôle** enregistre chaque visiteur externe qui se présente :

1. Il scanne la **zone MRZ** (Machine Readable Zone) de la pièce d'identité du visiteur (CNI ou passeport) via l'app mobile Flutter.
2. Le backend extrait automatiquement les informations d'identité (nom, prénom, numéro de document/NIN, dates, etc.) via un pipeline OCR.
3. L'agent complète le dossier : service/direction visité, agent du ministère à rencontrer, motif, coordonnées, signature manuscrite.
4. La visite est enregistrée avec le statut `PRESENT`, puis clôturée (`SORTI`) à la sortie du visiteur.

Le **frontend Flutter existe déjà** (prototype UI-first, mocké, sans aucun appel réseau réel) — voir section 3 pour le détail exact des écrans et des payloads attendus. Ta mission est de construire le **backend API** qui remplace tous les mocks.

### ⚠️ Précision importante sur le scan de document

Le frontend capture **une seule image** : la face du document contenant le **MRZ** (verso de la CNI, ou page d'identité du passeport). **Il n'y a pas de capture recto séparée à traiter par OCR classique** — toute l'extraction d'identité se fait exclusivement via la lecture et le parsing du MRZ (norme ICAO 9303). Ne conçois pas de logique de double upload recto/verso : un seul champ fichier `mrz_image`.

---

## 2. Stack technique imposée

Respecte strictement cette stack — ne propose pas d'alternative sans le signaler explicitement :

| Composant | Techno | Rôle |
|---|---|---|
| Framework API | **FastAPI** (Python 3.12+) | API REST asynchrone |
| Preprocessing image | **OpenCV** (`opencv-python-headless`) | Détection/redressement/binarisation de la zone MRZ |
| OCR | **PaddleOCR** | Extraction du texte brut des lignes MRZ |
| Parsing & validation MRZ | **`mrz`** (bibliothèque `arthurdejong/python-mrz`) | Parsing structuré + validation des checksums ICAO 9303 |
| ORM | **SQLAlchemy 2.0** (mode async) + **Alembic** pour les migrations | Persistance |
| Base de données | **PostgreSQL 16** | Stockage principal |
| Validation/serialization | **Pydantic v2** | Schémas de requête/réponse |
| Authentification | **JWT (OAuth2 Password Flow)** via `python-jose` + `passlib[bcrypt]` | Remplace Laravel Sanctum (le frontend a été initialement pensé pour Laravel, mais le backend cible est désormais FastAPI — adapte toute mention de "Sanctum" côté client à un flux JWT classique : login → access_token + refresh_token) |
| Tâches asynchrones | **Celery + Redis** (ou `BackgroundTasks` FastAPI si tu juges le volume trop faible pour justifier Celery — à toi de trancher et de le justifier en commentaire) | Traitement OCR non bloquant |
| Stockage fichiers | Stockage disque local sous `/storage/uploads` en dev, interface abstraite (`StorageService`) pour permettre un swap vers S3-compatible plus tard | Images MRZ, signatures |
| Tests | **pytest** + **pytest-asyncio** + **httpx.AsyncClient** | Tests unitaires et d'intégration |
| Conteneurisation | **Docker** + **docker-compose** (services : `api`, `db`, `redis`, `worker`) | Environnement reproductible |
| Documentation API | OpenAPI auto-générée par FastAPI (`/docs`) | — |

---

## 3. Modèles de données à créer

Base-toi sur cette structure (déduite de l'inventaire du frontend Flutter). Adapte les types SQLAlchemy en conséquence (UUID pour les clés primaires, `Enum` Python natif pour les champs à choix fixes).

### `User` (compte agent de contrôle — authentification backend)
```
id: UUID
nom: str
identifiant: str (unique, login)
mot_de_passe_hash: str
poste: str            # ex: "Poste principal"
role: enum(AGENT_CONTROLE, SUPERVISEUR, ADMIN)   # voir note ci-dessous
is_active: bool
created_at, updated_at: datetime
```
> **Note** : le frontend actuel n'implémente qu'un seul rôle, mais le texte "Accès réservé au personnel autorisé" suggère un besoin futur de rôles. Implémente l'enum dès maintenant avec `AGENT_CONTROLE` par défaut, pour éviter une migration douloureuse plus tard, mais ne bloque aucune route sur un rôle autre que `AGENT_CONTROLE` pour le moment (pas de règles de permission fines à ce stade).

### `Visitor`
```
id: UUID
prenom: str
nom: str
type_document: enum(CNI, PASSEPORT, PERMIS)
numero_document: str          # NIN pour CNI, n° passeport sinon
nationalite: str | None
date_naissance: date | None
sexe: enum(M, F) | None
date_expiration_document: date | None
telephone: str | None
email: str | None
provenance: str | None         # entreprise/provenance
immatriculation_vehicule: str | None
mrz_image_url: str | None      # chemin vers l'image scannée, conservée pour audit
created_at, updated_at: datetime
```

### `Visit`
```
id: UUID
visitor_id: FK -> Visitor
service_id: FK -> Service
agent_id: FK -> Agent
purpose_id: FK -> Purpose (nullable si motif texte libre saisi hors référentiel)
motif_libre: str | None
badge_number: str | None
signature_url: str | None
statut: enum(PRESENT, SORTI)
checked_in_at: datetime
checked_out_at: datetime | None
checked_in_by: FK -> User
checked_out_by: FK -> User | None
created_at, updated_at: datetime
```

### `Service`
```
id: UUID
name: str
code: str
floor: str | None
parent_id: FK -> Service | None   # hiérarchie optionnelle
```

### `Agent` (personnel du ministère, distinct de `User`)
```
id: UUID
name: str
role: str        # fonction, ex "Directeur des RH"
office: str
service_id: FK -> Service
```

### `Purpose` (référentiel des motifs)
```
id: UUID
libelle: str
```

---

## 4. Pipeline OCR MRZ — spécification technique détaillée

C'est le cœur métier de ce backend. Implémente-le comme un service isolé et testable : `app/services/mrz_ocr_service.py`.

### 4.1 Étapes du pipeline

1. **Réception de l'image** (`mrz_image`, formats acceptés : jpg, jpeg, png, heic — convertir HEIC en JPEG si besoin via `pillow-heif`).
2. **Preprocessing OpenCV** :
   - Conversion en niveaux de gris.
   - Détection des contours/de la zone MRZ (le MRZ est une bande de texte monospace dans le tiers inférieur du document — utilise une heuristique de détection de lignes de texte horizontales denses, ou un simple crop paramétrable si l'app impose déjà un cadrage guidé côté Flutter — **précise ce point si l'UI de scan impose un cadre de capture fixe, ce qui simplifierait beaucoup cette étape**).
   - Redressement (deskew) si angle détecté.
   - Amélioration du contraste (CLAHE) et binarisation adaptative pour maximiser la lisibilité par l'OCR.
3. **OCR avec PaddleOCR** :
   - Charger le modèle PaddleOCR en mode CPU par défaut (paramétrable via variable d'environnement `OCR_USE_GPU`).
   - Extraire le texte ligne par ligne de la zone MRZ recadrée.
   - Nettoyer les caractères mal reconnus fréquents dans le contexte MRZ (ex : confusion `O`/`0`, `I`/`1`) — le format MRZ n'utilise que des majuscules `A-Z`, des chiffres `0-9` et le caractère `<` comme remplisseur, ce qui permet un post-traitement de correction ciblé avant parsing.
4. **Détection du type de document** :
   - MRZ de CNI (format **TD1**, 3 lignes de 30 caractères) vs MRZ de passeport (format **TD3**, 2 lignes de 44 caractères) — détecte via le nombre de lignes valides extraites et leur longueur.
5. **Parsing avec la bibliothèque `mrz`** :
   - Passer les lignes brutes nettoyées au parser `mrz.checker.td1.TD1CodeChecker` ou `mrz.checker.td3.TD3CodeChecker` selon le type détecté.
   - Récupérer tous les champs structurés : nom, prénom(s), numéro de document, nationalité, date de naissance, sexe, date d'expiration, pays émetteur.
   - Récupérer le **résultat de validation des checksums** (`check_document_number`, `check_date_of_birth`, `check_expiration_date`, `check_composite`) — c'est essentiel pour donner un niveau de confiance sur l'extraction.
6. **Construction de la réponse structurée** (voir schéma JSON en 5.5).

### 4.2 Gestion des échecs

- Si aucune zone MRZ n'est détectée après preprocessing → retourner une erreur `422` explicite (`MRZ_NOT_DETECTED`), **jamais** une exception non gérée.
- Si le MRZ est détecté mais que les checksums échouent → retourner quand même les champs extraits, avec un champ `"mrz_valid": false` et le détail des checks qui ont échoué dans `"checksum_details"`, pour laisser l'agent de contrôle corriger manuellement côté Flutter plutôt que de bloquer le flux.
- Logger (niveau `WARNING`) chaque échec de parsing avec un identifiant de requête, sans logger l'image elle-même en clair dans les logs applicatifs (RGPD/protection des données personnelles).

### 4.3 Performance

- Le traitement complet (upload → réponse) doit rester sous **3 secondes** dans le cas nominal sur CPU. Si tu constates que PaddleOCR dépasse ce budget au chargement du modèle, mets en place un chargement du modèle **au démarrage de l'application** (singleton en mémoire), jamais à chaque requête.

---

## 5. Contrat API complet

Toutes les routes sont préfixées par `/api/v1`. Réponses en JSON. Utilise des schémas Pydantic dédiés request/response pour chaque route (pas de dict brut).

### 5.1 Authentification

| Méthode | Route | Description |
|---|---|---|
| POST | `/auth/login` | `{identifiant, mot_de_passe}` → `{access_token, refresh_token, token_type, user}` |
| POST | `/auth/refresh` | `{refresh_token}` → nouveau `access_token` |
| POST | `/auth/logout` | Invalide le refresh token courant (blacklist en Redis ou table dédiée) |
| POST | `/auth/forgot-password` | Envoie un lien/code de réinitialisation (stub acceptable si pas de service mail configuré — documente ce choix) |
| GET | `/me` | Retourne le profil de l'utilisateur authentifié (à partir du JWT) |

### 5.2 OCR MRZ

| Méthode | Route | Description |
|---|---|---|
| POST | `/ocr/scan` | `multipart/form-data` avec champ `mrz_image` → retourne les champs d'identité extraits (voir 5.5) |

### 5.3 Visites

| Méthode | Route | Description |
|---|---|---|
| POST | `/visits` | Crée une visite complète (visiteur + service + agent + motif + signature) |
| GET | `/visits` | Liste paginée, filtres query params : `statut`, `date_from`, `date_to`, `search` (nom/prénom/numéro doc), `sort=asc|desc` sur `checked_in_at` |
| GET | `/visits/{id}` | Détail d'une visite |
| PUT | `/visits/{id}/checkout` | Marque la sortie (statut → `SORTI`, `checked_out_at` = now, `checked_out_by` = user courant) |
| POST | `/visits/sync` | Synchronisation batch de visites créées hors-ligne (accepte une liste de payloads `POST /visits`, retourne le statut individuel de chaque insertion — succès/conflit/erreur) |

### 5.4 Référentiels

| Méthode | Route | Description |
|---|---|---|
| GET | `/services` | Liste des services (avec hiérarchie si `parent_id` renseigné) |
| GET | `/services/{id}/agents` | Agents rattachés à un service |
| GET | `/agents?service_id=` | Liste des agents, filtrable |
| GET | `/purposes` | Liste des motifs de visite |
| GET | `/dashboard/stats` | `{visites_du_jour, presents_actuellement, ...}` — alimente le `statCard` actuellement non branché côté Flutter |

### 5.5 Schéma de réponse `POST /ocr/scan`

```json
{
  "document_type": "CNI",
  "mrz_format": "TD1",
  "mrz_valid": true,
  "fields": {
    "nom": "DIOP",
    "prenom": "AMINATA",
    "numero_document": "1234567890123",
    "nin": "1234567890123",
    "nationalite": "SEN",
    "date_naissance": "1990-05-14",
    "sexe": "F",
    "date_expiration": "2030-05-14",
    "pays_emetteur": "SEN"
  },
  "checksum_details": {
    "document_number": true,
    "date_of_birth": true,
    "expiration_date": true,
    "composite": true
  },
  "raw_mrz_lines": [
    "I<SENDIOP<<AMINATA<<<<<<<<<<<<",
    "1234567890123SEN9005148F3005148<<<<<<<<<<<<02",
    "DIOP<<AMINATA<<<<<<<<<<<<<<<<<"
  ]
}
```

> Adapte le nom du champ NIN si les CNI sénégalaises encodent le NIN directement dans le champ `numero_document` du MRZ (à vérifier avec un échantillon réel de CNI — signale-le si ce n'est pas le cas, car cela peut nécessiter un champ séparé non couvert par le MRZ standard).

### 5.6 Format d'erreur standard (toutes les routes)

```json
{
  "error_code": "MRZ_NOT_DETECTED",
  "message": "Aucune zone MRZ n'a pu être détectée sur l'image fournie.",
  "details": null
}
```

Codes HTTP à respecter : `400` (requête malformée), `401` (non authentifié), `403` (interdit), `404` (introuvable), `409` (conflit, ex. doublon de visite lors d'une synchro), `422` (échec métier type MRZ non détecté), `500` (erreur serveur générique, jamais de stacktrace exposée au client).

---

## 6. Architecture des dossiers imposée

```
app/
├── main.py                      # instanciation FastAPI, montage des routers, lifespan (chargement modèle PaddleOCR)
├── core/
│   ├── config.py                # Settings (pydantic-settings), variables d'environnement
│   ├── security.py              # hashing, JWT
│   └── database.py              # engine async SQLAlchemy, session
├── models/                      # modèles SQLAlchemy
├── schemas/                      # schémas Pydantic (request/response), séparés par domaine
├── routers/                     # un fichier par domaine : auth.py, ocr.py, visits.py, referentiels.py
├── services/
│   ├── mrz_ocr_service.py       # pipeline décrit en section 4
│   ├── storage_service.py       # abstraction stockage fichiers
│   └── visit_service.py         # logique métier visites
├── repositories/                # accès DB (pattern repository, isolé des routers)
└── tests/
    ├── unit/
    └── integration/
alembic/
docker-compose.yml
Dockerfile
requirements.txt
.env.example
```

Ne mélange jamais logique métier et logique HTTP : les routers appellent des services, les services appellent des repositories.

---

## 7. Exigences non-fonctionnelles

- **CORS** : autoriser explicitement l'origine de l'app Flutter (à paramétrer via variable d'environnement, ne jamais mettre `*` en production).
- **Validation stricte** de tous les payloads entrants via Pydantic — aucune donnée non validée ne doit atteindre la couche DB.
- **Logging structuré** (JSON) avec un identifiant de corrélation par requête.
- **Variables sensibles** (clé JWT, credentials DB) exclusivement via variables d'environnement, jamais en dur.
- **Migrations Alembic** obligatoires pour toute évolution de schéma — pas de `create_all()` en production.
- **Pagination** obligatoire sur `GET /visits` (paramètres `page`, `page_size`, réponse enveloppée `{items, total, page, page_size}`).

---

## 8. Exigences de tests

- Tests unitaires du service `mrz_ocr_service` avec des **lignes MRZ de test fixes** (pas besoin d'image réelle pour tester le parsing — teste le parsing/validation isolément du pipeline OCV/PaddleOCR).
- Utilise ces échantillons de test (données factices, checksums valides à vérifier/générer avec la lib `mrz` elle-même) :
  - **TD3 (passeport)**, 2 lignes de 44 caractères.
  - **TD1 (CNI)**, 3 lignes de 30 caractères.
- Tests d'intégration sur les routes `/visits` (création, checkout, listing filtré) avec une base de test (SQLite en mémoire ou conteneur Postgres de test — au choix, mais documente-le).
- Couverture minimale attendue : logique métier des services à 80%+.

---

## 9. Plan de développement (procède dans cet ordre)

1. Scaffolding du projet (structure de dossiers, `requirements.txt`, config, Docker Compose avec Postgres + Redis).
2. Modèles SQLAlchemy + première migration Alembic.
3. Authentification JWT complète (`/auth/*`, `/me`) + tests.
4. Endpoints référentiels (`/services`, `/agents`, `/purposes`) en lecture seule avec données de seed.
5. Service `mrz_ocr_service` : d'abord la partie parsing/validation avec la lib `mrz` (testable sans image), puis le pipeline OpenCV + PaddleOCR complet, puis l'endpoint `/ocr/scan`.
6. Endpoints `/visits` (CRUD partiel : create, list+filtres, get, checkout) + tests.
7. Endpoint `/visits/sync` (batch offline).
8. Endpoint `/dashboard/stats`.
9. Revue finale : vérifie que chaque route du contrat en section 5 existe, que les erreurs suivent le format 5.6, et que le `docker-compose up` démarre l'ensemble sans intervention manuelle.

Ne passe à l'étape suivante que lorsque l'étape courante a ses tests qui passent.

---

## 10. Critères d'acceptation (Definition of Done)

- [ ] `docker-compose up` lance l'API, la DB et le worker sans erreur.
- [ ] `/docs` (Swagger) liste toutes les routes de la section 5 avec des schémas de requête/réponse complets.
- [ ] `POST /ocr/scan` retourne une réponse conforme au schéma 5.5 pour un MRZ TD1 et un MRZ TD3 de test.
- [ ] Le format d'erreur 5.6 est respecté sur au moins 3 cas d'échec différents testés manuellement (auth invalide, MRZ non détecté, visite introuvable).
- [ ] `pytest` passe intégralement en CI locale.
- [ ] Aucune donnée sensible (mot de passe, token) n'apparaît en clair dans les logs.

---

## 11. Instructions de style pour Claude Code

- Code en **Python idiomatique, typé** (type hints partout, `from __future__ import annotations` si utile).
- Commentaires en français pour la logique métier, en anglais acceptable pour les commentaires purement techniques/génériques.
- Pas de `print()` de debug laissé dans le code final — utilise le logger configuré.
- Ne génère pas de fichier `.env` avec de vraies valeurs secrètes — uniquement `.env.example`.
- Si une décision d'architecture non tranchée dans ce document se présente (ex. Celery vs BackgroundTasks), fais un choix raisonnable, applique-le, et **documente-le explicitement dans un commentaire ou un `ARCHITECTURE_DECISIONS.md`** plutôt que de me demander de trancher à chaque micro-détail.
- Si tu identifies une incohérence entre ce document et le comportement réel du frontend Flutter existant (fourni en annexe ci-dessous si besoin), signale-la avant de l'implémenter silencieusement.

---

## Annexe — Points ouverts à clarifier avant/pendant le développement

1. Confirmer si les CNI sénégalaises encodent le NIN directement dans le champ `numero_document` du MRZ (probable mais à vérifier sur un échantillon réel).
2. Décider si le mode offline-first (`/visits/sync`) est prioritaire pour la V1 ou reportable en V2.
3. Décider du système de rôles définitif (`SUPERVISEUR`/`ADMIN`) — actuellement implémenté en base mais non exploité en permissions.
4. Confirmer le format d'image que l'app Flutter enverra réellement pour le MRZ (photo libre vs cadre de capture guidé) — cela impacte directement la complexité de l'étape de détection de zone en section 4.1.
