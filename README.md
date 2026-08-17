# SIGV — Backend API

API du **Système Intelligent de Gestion des Visites** : contrôle d'accès d'un poste
d'accueil, avec extraction automatique de l'identité des visiteurs par lecture de la
zone MRZ (ICAO 9303) de leur pièce d'identité.

Spécification fonctionnelle : [`SPEC_BACKEND_SIGV_OCR_MRZ.md`](SPEC_BACKEND_SIGV_OCR_MRZ.md)
Décisions techniques : [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md)
Guide d'implémentation, de zéro à l'API : [`GUIDE_IMPLEMENTATION.md`](GUIDE_IMPLEMENTATION.md)
Mise en production : [`DEPLOYMENT.md`](DEPLOYMENT.md)

---

## Stack

FastAPI (Python 3.12) · SQLAlchemy 2.0 async + Alembic · PostgreSQL 16 · Pydantic v2 ·
OpenCV + PaddleOCR + `mrz` · JWT (`python-jose`) · pytest

---

## Démarrage

```bash
# 1. Dépendances
uv sync

# 2. Configuration
cp .env.example .env      # puis renseignez DATABASE_URL et JWT_SECRET_KEY

# 3. Schéma de base
uv run alembic upgrade head

# 4. Données de démonstration (services, agents, motifs, comptes)
uv run python -m app.seeds

# 5. Lancement
uv run uvicorn app.main:app --reload
```

Documentation interactive : <http://localhost:8000/docs>
Sondes : <http://localhost:8000/health> (processus) · <http://localhost:8000/health/ready> (base)

> Le premier démarrage télécharge les modèles PaddleOCR (quelques centaines de Mo).
> Pour démarrer sans OCR pendant le développement : `OCR_PRELOAD_MODEL=false`.

### Comptes de démonstration

Créés par `python -m app.seeds`, mot de passe réglé par `SEED_AGENT_PASSWORD`
(défaut `Sigv@2026`) :

| Identifiant | Rôle | Usage |
|---|---|---|
| `agent001` | `AGENT_CONTROLE` | app mobile |
| `superviseur001` | `SUPERVISEUR` | app mobile — aucun droit d'administration |
| `admin001` | `ADMIN` | dashboard web, accès global à l'API |

### Créer un compte à la demande

`app.seeds` pose un jeu de démonstration complet. Pour n'ajouter **qu'un compte** —
un accès de test, ou le premier administrateur d'une installation de production
où les comptes de démonstration n'ont rien à faire :

```bash
uv run python -m app.create_user test001
uv run python -m app.create_user admin002 --role ADMIN --nom "Awa Ndiaye"
uv run python -m app.create_user agent007 --mot-de-passe "MotDePasseSolide2026"
```

Sans `--mot-de-passe`, un mot de passe fort est généré et affiché **une seule
fois** : seul son hash bcrypt part en base. Le script écrit dans la base désignée
par `DATABASE_URL`, refuse un identifiant déjà pris et un mot de passe de moins de
12 caractères.

> Une fois l'API en ligne, la même opération se fait par `POST /api/v1/users`
> depuis le dashboard. Cette commande sert surtout à l'amorçage, quand aucun
> compte administrateur n'existe encore pour appeler cette route.

### Importer l'annuaire réel (services et agents)

`app.seeds` pose huit agents fictifs, de quoi cliquer dans l'app. Pour remplir les
référentiels avec le personnel réel du ministère, à partir de l'export CSV de
l'annuaire :

```bash
uv run python -m app.import_annuaire data/annuaire.csv --dry-run   # montre le plan
uv run python -m app.import_annuaire data/annuaire.csv             # applique
```

Colonnes attendues — l'ordre est libre, la casse et les accents indifférents :

```
Matricule, nom_et_prenoms, Nom, Téléphone, Email, Fonction, Direction, Département, Sexe
```

Seules `nom_et_prenoms`, `Nom`, `Fonction` et `Département` (à défaut `Direction`)
sont exploitées : la table `agents` ne stocke qu'un nom, une fonction, un bureau et
un service. Matricule, téléphone, e-mail et sexe n'ont pas de colonne en base et
sont ignorés — les importer supposerait une migration, et le registre des visites
ne s'en sert nulle part.

Le service de rattachement est **déduit du libellé de département** : son code est
l'acronyme des mots significatifs (« Direction des Systemes D'informations » →
`DSI`). Deux libellés donnant le même acronyme sont fusionnés — « SG » et
« Sécretariat Général » — et le rapport le signale. Un service dont le code ou le
nom existe déjà est réutilisé sans être renommé ; les lignes sans département vont
dans `SANS_SERVICE`.

Le script est **idempotent** : le relancer après un correctif du CSV ne crée pas de
doublons, et ne modifie jamais une fiche existante sauf avec `--mettre-a-jour`
(rafraîchit la seule fonction). Rien n'est jamais supprimé ni archivé : retirer
quelqu'un de l'annuaire reste une action d'administrateur, les visites déjà
enregistrées le référencent. Lancez toujours `--dry-run` d'abord — la
correspondance libellé → service est une heuristique, et c'est le seul moment où
elle se relit facilement.

> Le CSV contient des données personnelles d'agents réels : il est ignoré par git
> (`data/`, `/*.csv`) et n'a pas à être versionné.

---

## Tester l'API avec Postman

Une collection prête à l'emploi est fournie dans [`postman/`](postman/).

1. Dans Postman : **Import** → glissez les deux fichiers
   `postman/SIGV.postman_collection.json` et `postman/SIGV.postman_environment.json`.
2. En haut à droite, sélectionnez l'environnement **SIGV — Local**.
3. Lancez **Auth → Login**. Les tokens sont capturés automatiquement : toutes les
   autres requêtes s'authentifient seules, rien à copier-coller.
4. Pour **OCR MRZ → Scan MRZ**, ouvrez l'onglet **Body → form-data** et sélectionnez
   votre photo sur la ligne `mrz_image` (type *File*). Postman ne conserve pas les
   chemins de fichiers à l'import, c'est la seule action manuelle.

Les requêtes se chaînent : le scan alimente l'identité du visiteur, les référentiels
alimentent `service_id` / `agent_id` / `purpose_id`, et **Créer une visite** réutilise
le tout. Vous pouvez donc lancer la collection entière d'un coup via le *Collection
Runner* — chaque requête embarque ses assertions (format d'erreur, checksums MRZ,
présence du NIN, pagination).

> Ordre conseillé : `Login` → `Services` → `Agents d'un service` → `Motifs` →
> `Scan MRZ` → `Créer une visite` → `Lister` → `Checkout`. Gardez `Logout` pour la
> fin : il révoque le refresh token.

Les six dossiers **Administration — …** couvrent le dashboard web. Ils utilisent
`admin_access_token`, renseigné par **Auth → Login administrateur** : les deux
sessions coexistent dans l'environnement, ce qui permet de vérifier les refus
`403` d'un agent de contrôle sur une route d'administration.

> Pour rejouer la collection entière une seconde fois, changez
> `nouvel_identifiant`, `nouveau_code_service` et `nouveau_motif` : ces trois
> valeurs sont uniques en base, et les requêtes de création répondraient sinon
> `409` à juste titre.

### Points d'attention

- **Le scan prend ~6 s** sur CPU. Augmentez le timeout de Postman si besoin
  (*Settings → Request timeout*).
- **Filtres de dates** : utilisez `2026-08-01T00:00:00Z` plutôt que `+00:00`, le `+`
  étant interprété comme une espace dans une query string.
- **Alternative sans Postman** : voir ci-dessous.

## Brancher l'application Flutter

### 1. Rendre le backend joignable depuis le téléphone

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` est indispensable : par défaut uvicorn n'écoute que sur `127.0.0.1`,
inaccessible depuis un autre appareil.

Puis **ouvrir le port dans le pare-feu Windows** (PowerShell administrateur) :

```powershell
New-NetFirewallRule -DisplayName "SIGV backend 8000" -Direction Inbound `
  -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
```

> `-Profile Private` limite l'ouverture aux réseaux déclarés privés. N'ouvrez pas le
> port sur un profil public.

### 2. URL de base selon la cible

| Cible | `baseUrl` |
|---|---|
| Téléphone physique (même Wi-Fi) | `http://<IP-DU-PC>:8000` |
| Émulateur Android | `http://10.0.2.2:8000` |
| Simulateur iOS | `http://localhost:8000` |
| Flutter Web | `http://localhost:8000` + ajouter l'origine à `CORS_ORIGINS` |

L'IP du PC s'obtient avec `ipconfig` (carte Wi-Fi, « Adresse IPv4 »).

### 3. Autoriser le HTTP en clair

En développement l'API est en HTTP, or les plateformes le bloquent par défaut.

- **Android** — dans `android/app/src/main/AndroidManifest.xml`, sur la balise
  `<application>` : `android:usesCleartextTraffic="true"`.
- **iOS** — dans `ios/Runner/Info.plist`, ajouter une exception `NSAppTransportSecurity`.

À retirer en production, où l'API doit être derrière HTTPS.

### 4. Points d'attention côté client

- **Timeout du scan** : `POST /ocr/scan` prend ~6 à 15 s selon la machine. Réglez le
  `receiveTimeout` de Dio à **60 s minimum**, sinon le scan échouera côté client alors
  que le serveur répond correctement.
- **CORS ne concerne que Flutter Web.** Sur mobile (Android/iOS), la politique CORS
  n'est pas appliquée : inutile d'y toucher.
- **Taille des photos** : limite à 10 Mo (`MAX_UPLOAD_SIZE_MB`). Une photo de
  smartphone pèse 2 à 6 Mo, mais compressez côté Flutter avant envoi — cela réduit
  aussi le temps de transfert.
- **Formats acceptés** : jpg, jpeg, png, heic/heif. Le HEIC des iPhone est décodé
  côté serveur, aucune conversion nécessaire côté client.
- **URLs de fichiers** : `mrz_image_url` et `signature_url` sont **relatives**
  (`/storage/uploads/...`). Préfixez-les par votre `baseUrl` pour les afficher.
- **Renouvellement du token** : l'access token expire au bout de 30 min. Sur un `401`,
  appelez `POST /auth/refresh` puis rejouez la requête (intercepteur Dio).

### 5. Parcours type d'un enregistrement de visite

```
POST /auth/login                 → access_token + refresh_token
GET  /services                   → l'agent choisit le service
GET  /services/{id}/agents       → puis la personne à rencontrer
GET  /purposes                   → puis le motif
POST /ocr/scan                   → scan du verso : identité + NIN pré-remplis
POST /uploads/signature          → dépôt de la signature manuscrite → {url}
POST /visits                     → création, avec signature_url et mrz_image_url
PUT  /visits/{id}/checkout       → à la sortie du visiteur
```

## Tester avec Swagger

<http://localhost:8000/docs> permet tout, y compris l'upload de photo.

Cliquez **Authorize**, saisissez `agent001` / `Sigv@2026` dans *username* / *password*
(laissez `client_id` et `client_secret` vides), puis **Authorize**. Toutes les routes
protégées sont ensuite accessibles.

> Le bouton s'appuie sur `POST /auth/token`, la variante **formulaire** OAuth2 de
> l'authentification. `POST /auth/login`, en JSON, reste le point d'entrée destiné à
> l'app Flutter — les deux partagent la même logique et délivrent les mêmes tokens.

## Tests

```bash
uv run pytest                              # suite complète
uv run pytest tests/unit -q                # unitaires seuls (rapides, sans base)
uv run pytest --cov --cov-report=term-missing
```

La base de test est SQLite en mémoire ([ADR-007](ARCHITECTURE_DECISIONS.md)) : aucun
service externe n'est requis. `tests/integration/test_migrations.py` vérifie que les
migrations Alembic restent alignées sur les modèles.

---

## Routes

Toutes les routes sont préfixées par `/api/v1` et exigent un bearer token, sauf
`/auth/login`, `/auth/refresh` et `/auth/forgot-password`.

| Domaine | Route |
|---|---|
| Santé | `GET /health` · `GET /health/ready` *(hors préfixe `/api/v1`, sans authentification)* |
| Auth | `POST /auth/login` · `POST /auth/token` · `POST /auth/refresh` · `POST /auth/logout` · `POST /auth/forgot-password` · `GET /me` |
| OCR | `POST /ocr/scan` |
| Fichiers | `POST /uploads/signature` |
| Visites | `POST /visits` · `GET /visits` · `GET /visits/{id}` · `PUT /visits/{id}/checkout` · `POST /visits/sync` |
| Référentiels | `GET /services` · `GET /services/{id}/agents` · `GET /agents` · `GET /purposes` |
| Dashboard | `GET /dashboard/stats` |

### Routes d'administration (rôle `ADMIN`)

Ajoutées pour le dashboard web. Elles répondent `403 FORBIDDEN` à tout autre
rôle — le contrôle est fait **côté serveur**, en relisant le rôle en base à
chaque requête et jamais depuis la revendication `role` du JWT.

| Domaine | Route |
|---|---|
| Comptes | `GET/POST /users` · `GET/PUT /users/{id}` · `PATCH /users/{id}/status` · `POST /users/{id}/reset-password` · `POST /users/{id}/unlock` · `GET /users/{id}/sessions` · `DELETE /users/{id}/sessions/{sessionId}` |
| Référentiels | `POST /services` · `PUT /services/{id}` · `PATCH /services/{id}/status` *(idem `agents`, `purposes`)* |
| Visites | `PATCH /visits/{id}` · `POST /visits/{id}/cancel` · `GET /visits/export?format=csv` |
| Statistiques | `GET /dashboard/stats/timeseries` · `/by-service` · `/by-purpose` · `/peak-hours` · `/avg-duration` · `/top-agents` |
| Audit | `GET /audit-logs` · `GET /audit-logs/actions` |
| Paramètres | `GET/PUT /settings` |

Quatre principes gouvernent ces routes :

- **Aucune suppression physique.** Comptes, services, agents, motifs et visites
  sont référencés par le registre : on désactive, on archive, on annule.
  `PATCH .../status` remplace `DELETE`.
- **Tout est tracé.** Connexions, corrections, annulations, changements de
  compte et de référentiel alimentent `audit_logs`, avec le diff avant/après.
- **Rétro-compatibilité.** Aucune route existante ne change de contrat. Les
  filtres `service_id`, `agent_id`, `purpose_id` et `created_by` s'*ajoutent* à
  `GET /visits` ; les valeurs de `role` restent en majuscules.
- **Verrouillage après échecs.** Cinq tentatives ratées bloquent un compte pour
  quinze minutes. Seuil et durée sont réglables dans `/settings`, sans
  redéploiement.

> `GET /visits/export?format=pdf` répond `501 EXPORT_FORMAT_UNAVAILABLE` : la
> route et ses filtres sont en place, le rendu PDF demandera une bibliothèque
> dédiée. Le CSV, lui, est complet (UTF-8 avec BOM, séparateur `;`, prêt pour
> Excel en configuration francophone).

### Scan MRZ

`POST /ocr/scan` attend un `multipart/form-data` avec un **unique** champ fichier
`mrz_image` : la face du document portant le MRZ (verso de CNI, page d'identité du
passeport). Il n'y a pas de capture recto séparée.

Les formats TD1 (CNI, 3×30) et TD3 (passeport, 2×44) sont reconnus automatiquement.
Un échec de checksum ICAO **ne bloque pas** le flux : la réponse porte
`mrz_valid: false` et le détail par champ dans `checksum_details`, pour laisser
l'agent corriger la saisie côté mobile.

#### Cas des CNI sénégalaises

Validé sur une carte CEDEAO réelle ([ADR-005](ARCHITECTURE_DECISIONS.md)) :

- Le **numéro de carte fait 17 chiffres** et déborde du champ MRZ (9 caractères max).
  Le parser applique la convention ICAO 9303 de débordement et reconstitue le numéro
  complet dans `numero_document`, checksum recalculé sur le numéro entier.
- Le **NIN n'est pas encodé dans le MRZ** : il est imprimé juste au-dessus. Il est
  malgré tout extrait **automatiquement**, dans la même passe OCR
  ([ADR-014](ARCHITECTURE_DECISIONS.md)) — aucune saisie manuelle n'est requise.
- Le numéro de carte encode la **date de naissance** (`1` + `01` + `AAAAMMJJ` +
  séquence + clé), ce qui offre un contrôle de cohérence croisé avec le MRZ.

#### Robustesse aux photos réelles

Validé sur une photo prise au téléphone (4032×3024, carte posée sur une table) :

- **Détection et redressement de la carte** — seuillage d'Otsu, contrôle des
  proportions ID-1, correction de perspective. Le fond de la photo est éliminé et
  toutes les zones se ciblent ensuite par ratios, quel que soit le cadrage.
- **Orientation** — le pipeline essaie les orientations restantes et retient celle
  dont les checksums ICAO se recomposent ([ADR-012](ARCHITECTURE_DECISIONS.md)), sans
  surcoût quand la photo est droite.
- Si l'app Flutter impose plus tard un cadre de capture guidé, rien n'est à changer :
  la détection de carte se contentera de trouver la carte immédiatement.

> **Latence** : ~5,5 s par scan sur CPU, dont la quasi-totalité en inférence OCR — le
> budget de 3 s de la spec n'est pas tenu sur la machine de développement. Le
> preprocessing ne pèse que 0,3 s. Voir [ADR-013](ARCHITECTURE_DECISIONS.md) pour les
> leviers (GPU, build PaddlePaddle avec oneDNN fonctionnel).

### Format d'erreur

Uniforme sur toutes les routes, sans jamais exposer de stacktrace :

```json
{ "error_code": "MRZ_NOT_DETECTED", "message": "…", "details": null }
```

`400` requête malformée · `401` non authentifié · `403` interdit · `404` introuvable ·
`409` conflit · `422` échec métier · `500` erreur serveur.

---

## Structure

```
app/
├── main.py            # app FastAPI, middlewares, lifespan (préchargement PaddleOCR)
├── core/              # config, sécurité, base de données, logging, erreurs, dépendances
├── models/            # modèles SQLAlchemy
├── schemas/           # schémas Pydantic request/response
├── routers/           # couche HTTP — appelle les services, jamais la base directement
├── services/          # logique métier (auth, visites, dashboard, pipeline OCR MRZ)
├── repositories/      # accès base de données
└── seeds.py
alembic/               # migrations
tests/{unit,integration}/
```

Les routers appellent les services, les services appellent les repositories : aucune
logique métier dans la couche HTTP, aucune requête SQL hors des repositories.

---

## Déploiement

La procédure complète pour un VPS — PostgreSQL, service systemd, Nginx en
terminaison TLS, sauvegardes — est décrite dans [`DEPLOYMENT.md`](DEPLOYMENT.md).
Les fichiers de configuration prêts à copier sont dans [`deploy/`](deploy/) et le
modèle de configuration dans [`.env.production.example`](.env.production.example).

Avec `ENVIRONMENT=production`, l'API applique trois différences notables :

| | Développement | Production |
|---|---|---|
| `/docs`, `/openapi.json` | exposés | masqués (`ENABLE_DOCS=true` les rouvre) |
| `storage/uploads` | servi par l'API | servi par Nginx (`SERVE_STORAGE`) |
| Configuration | tolérante | démarrage refusé si clé JWT d'exemple, `CORS_ORIGINS=*`, base SQLite ou `DB_ECHO=true` |

---

## Reste à faire

- **Conteneurisation** — écartée pour cette version, voir la dernière section de
  [`DEPLOYMENT.md`](DEPLOYMENT.md).
- **Envoi réel** du lien de réinitialisation de mot de passe
  ([ADR-006](ARCHITECTURE_DECISIONS.md)).
- **Test du pipeline OCR sur photo réelle** : le parsing MRZ est validé sur une CNI
  réelle, mais la chaîne OpenCV + PaddleOCR n'a été exercée que sur images de
  synthèse — reste à mesurer le taux de reconnaissance sur photos prises au téléphone.
