# Guide d'implémentation — construire SIGV de A à Z

Ce document explique **comment cette application a été construite**, brique par
brique, et **pourquoi** chaque choix a été fait. Il est écrit pour être suivi par
un développeur qui découvre FastAPI, l'asynchrone en Python ou la vision par
ordinateur : chaque notion est introduite avant d'être utilisée.

Les trois autres documents du dépôt ont un rôle différent :

| Document | Répond à la question |
|---|---|
| [`SPEC_BACKEND_SIGV_OCR_MRZ.md`](SPEC_BACKEND_SIGV_OCR_MRZ.md) | Que doit faire l'application ? |
| [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md) | Pourquoi telle décision plutôt que telle autre ? (format ADR, court) |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Comment la mettre en production ? |
| **Ce guide** | **Comment la reconstruire depuis une page blanche ?** |

---

## Table des matières

1. [Ce que fait l'application](#1-ce-que-fait-lapplication)
2. [Prérequis et outillage](#2-prérequis-et-outillage)
3. [Le catalogue des dépendances](#3-le-catalogue-des-dépendances)
4. [L'architecture en couches](#4-larchitecture-en-couches)
5. [Construction pas à pas](#5-construction-pas-à-pas)
6. [Le moteur OCR de A à Z](#6-le-moteur-ocr-de-a-à-z)
7. [La stratégie de tests](#7-la-stratégie-de-tests)
8. [Pièges rencontrés](#8-pièges-rencontrés-et-comment-les-éviter)

---

## 1. Ce que fait l'application

SIGV — *Système Intelligent de Gestion des Visites* — équipe le poste d'accueil
d'une administration. Un agent de contrôle enregistre les visiteurs, et pour
éviter la saisie manuelle de l'identité, **il photographie la pièce d'identité :
le serveur en extrait automatiquement le nom, le prénom, la date de naissance, le
numéro de document et le NIN.**

Le parcours complet d'un enregistrement :

```
1. POST /auth/login            l'agent s'authentifie          → access + refresh token
2. GET  /services              il choisit le service visité
3. GET  /services/{id}/agents  puis la personne à rencontrer
4. GET  /purposes              puis le motif
5. POST /ocr/scan              il photographie le verso de la CNI
                               → nom, prénom, date de naissance, n° doc, NIN
6. POST /uploads/signature     le visiteur signe sur l'écran tactile → {url}
7. POST /visits                création de la visite
8. PUT  /visits/{id}/checkout  à la sortie du visiteur
```

Le client est une application Flutter ; le backend ne sert que du JSON (plus les
images déposées). C'est ce qui justifie l'ensemble des choix qui suivent : pas de
templates HTML, pas de sessions à cookie, une authentification par jeton.

---

## 2. Prérequis et outillage

### Python 3.12

Le projet exige `>=3.12`, pour trois apports concrets utilisés dans le code :

- la syntaxe `X | None` au lieu de `Optional[X]` (partout dans les modèles) ;
- `StrEnum` ([`app/models/enums.py`](app/models/enums.py)), une énumération dont
  les membres *sont* des chaînes — pratique pour le JSON et pour SQLAlchemy ;
- `datetime.UTC`, plus court que `timezone.utc`.

### uv, le gestionnaire de paquets

```bash
# Windows PowerShell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Pourquoi uv plutôt que `pip` + `venv`, ou Poetry ?**

`pip install fastapi` installe *une* version compatible aujourd'hui, qui ne sera
pas forcément celle installée demain sur le serveur. uv écrit un fichier
[`uv.lock`](uv.lock) qui fige **l'arbre complet** des dépendances, transitives
comprises, avec leurs empreintes. `uv sync --frozen` reconstruit exactement le
même environnement partout — c'est ce qui rend un déploiement reproductible.

Face à Poetry, qui offre la même garantie, uv gagne sur la vitesse (résolution
écrite en Rust, de l'ordre de la seconde là où Poetry prend des minutes sur un
arbre lourd comme celui-ci) et sait installer Python lui-même.

Commandes utiles :

```bash
uv sync                    # installe les dépendances du lock (dev incluses)
uv sync --frozen --no-dev  # production : versions figées, sans outils de test
uv add fastapi             # ajoute une dépendance et met le lock à jour
uv run pytest              # exécute dans l'environnement du projet
```

### PostgreSQL 16+

```bash
# Debian / Ubuntu
sudo apt install postgresql
sudo -u postgres psql -c "CREATE USER sivg WITH PASSWORD 'motdepasse';"
sudo -u postgres psql -c "CREATE DATABASE sivg OWNER sivg ENCODING 'UTF8';"
```

Sous Windows, l'installateur officiel EnterpriseDB fait le travail. Notez le mot
de passe du superutilisateur `postgres`, il sera redemandé.

---

## 3. Le catalogue des dépendances

Chaque ligne de [`pyproject.toml`](pyproject.toml) est un choix. Voici ce que
chacune apporte, et ce qu'il aurait fallu écrire à la main sans elle.

### Le socle web

**`fastapi[standard]`** — le framework HTTP.

Ce qu'il apporte, et qui explique qu'il ait été retenu plutôt que Flask ou
Django : la **validation automatique** des entrées et sorties à partir des
annotations de type. Cette signature suffit à FastAPI pour rejeter en 400 toute
requête malformée, documenter la route, et garantir la forme de la réponse :

```python
@router.post("", response_model=VisitRead, status_code=201)
async def create_visit(payload: VisitCreate, service: VisitServiceDep) -> VisitRead:
    ...
```

Il apporte aussi l'**injection de dépendances** (`Depends`), utilisée ici pour la
session de base de données et l'utilisateur authentifié, et la génération d'un
schéma OpenAPI — la page `/docs` n'est pas écrite, elle est déduite du code.

L'extra `[standard]` tire `uvicorn` (le serveur ASGI qui exécute réellement
l'application), `python-multipart` et quelques accélérateurs.

**`pydantic-settings`** — la configuration.

Lit les variables d'environnement et le fichier `.env`, les convertit dans le bon
type et refuse de démarrer si une valeur est invalide. C'est plus qu'un confort :
c'est ce qui permet à [`app/core/config.py`](app/core/config.py) de bloquer un
démarrage en production avec une clé JWT d'exemple.

**`python-multipart`** — décodage des corps `multipart/form-data`, c'est-à-dire
des envois de fichiers. Sans lui, `POST /ocr/scan` renverrait une erreur : FastAPI
délègue entièrement le décodage du multipart à cette bibliothèque.

### Base de données

**`sqlalchemy[asyncio]`** — l'ORM.

Un ORM fait correspondre une classe Python et une table SQL. `Visit(...)` devient
un `INSERT`, `select(Visit).where(...)` devient un `SELECT`. On y gagne trois
choses : les requêtes sont **paramétrées** donc immunisées contre l'injection SQL,
le typage statique fonctionne (`visit.checked_in_at` est un `datetime` pour mypy),
et le même code tourne sur PostgreSQL en production comme sur SQLite dans les
tests.

L'extra `[asyncio]` est essentiel ici. En mode synchrone, chaque requête SQL
bloque le thread ; sous FastAPI cela gèlerait la boucle d'événements et donc
toutes les requêtes en cours. Avec `AsyncSession`, l'attente de la base rend la
main aux autres requêtes.

**`asyncpg`** — le pilote PostgreSQL asynchrone.

SQLAlchemy ne parle pas le protocole PostgreSQL, il délègue à un pilote. `psycopg2`
est synchrone ; `asyncpg` est asynchrone et nettement plus rapide. D'où l'URL de
connexion `postgresql+asyncpg://...` : la partie après le `+` désigne le pilote.

**`alembic`** — les migrations.

SQLAlchemy sait créer les tables (`Base.metadata.create_all`), mais pas les faire
**évoluer** sans perdre les données. Alembic génère des scripts de migration
versionnés : ajouter une colonne `nin` sur une base contenant déjà des visiteurs
se fait par un `ALTER TABLE` daté et rejouable, pas par une recréation.

### Sécurité

**`python-jose[cryptography]`** — signature et vérification des JWT.

Un JWT est un jeton contenant l'identité de l'utilisateur, signé par le serveur.
Il évite de stocker des sessions : le serveur vérifie la signature et croit le
contenu. `python-jose` implémente la signature HMAC-SHA256 utilisée ici ; l'extra
`[cryptography]` fournit le backend cryptographique performant.

**`passlib[bcrypt]`** — présent dans les dépendances, mais **le code ne l'utilise
pas**. [`app/core/security.py`](app/core/security.py) appelle `bcrypt`
directement, car passlib 1.7.4 est incompatible avec bcrypt ≥ 4.1 : sa détection
de backend lit un attribut `__about__` qui a disparu (voir ADR-003). La
dépendance reste déclarée uniquement parce qu'elle tire `bcrypt` ; c'est un reste
à nettoyer.

Le rôle de bcrypt : transformer un mot de passe en empreinte irréversible et
**lente**. La lenteur est la fonctionnalité — elle rend le test exhaustif de
millions de mots de passe hors de portée.

### Le pipeline OCR

**`opencv-python-headless`** — traitement d'image.

Tout ce qui précède la lecture du texte : décodage, redressement, détection de la
carte, correction de perspective, contraste, binarisation. La variante `headless`
est sans interface graphique — elle n'installe pas les bibliothèques GTK/Qt, ce
qui évite d'embarquer une pile graphique sur un serveur qui n'affichera jamais
rien.

**`paddleocr` + `paddlepaddle`** — la reconnaissance de texte.

`paddlepaddle` est le moteur d'inférence (l'équivalent de PyTorch ou TensorFlow),
`paddleocr` la collection de modèles pré-entraînés qui tourne dessus. Le choix
face à Tesseract, l'alternative historique : sur du texte photographié — donc
légèrement flou, incliné, inégalement éclairé — les modèles neuronaux de
PaddleOCR sont sensiblement plus fiables que l'approche classique de Tesseract.
Face à EasyOCR, PaddleOCR est plus rapide sur CPU, ce qui compte quand le budget
de latence est de quelques secondes.

**`mrz`** — validation ICAO 9303.

Implémente les règles de la norme : découpage des champs par position, calcul des
chiffres de contrôle, rapport de validation. Réécrire cela serait faisable
(l'algorithme tient en dix lignes, il est reproduit en §6.10) mais la norme compte
de nombreux cas particuliers par format et par pays émetteur.

**`pillow` + `pillow-heif`** — décodage des formats qu'OpenCV ignore.

OpenCV ne lit pas le HEIC, format par défaut des photos iPhone. Sans
`pillow-heif`, un agent sur iPhone verrait toutes ses captures rejetées.

### Déclarées mais non utilisées

**`celery`** et **`redis`** figurent dans les dépendances sans être employés. La
spécification prévoyait de traiter l'OCR en tâche de fond ; l'ADR-002 a tranché
autrement — le client a besoin du résultat immédiatement, une file d'attente
n'apporterait que de la latence et deux services à exploiter. À retirer.

### Outils de développement (`[dependency-groups] dev`)

| Paquet | Rôle |
|---|---|
| `pytest` | exécution des tests |
| `pytest-asyncio` | permet d'écrire des tests `async def` |
| `pytest-cov` | mesure de couverture |
| `httpx` | client HTTP asynchrone ; interroge l'application en mémoire, sans serveur |
| `aiosqlite` | pilote SQLite asynchrone, pour la base de test |
| `ruff` | linter et formateur, en remplacement de flake8 + isort + black |
| `mypy` | vérification des types |

---

## 4. L'architecture en couches

### Le principe

Le code est organisé en **quatre couches**, chacune ne parlant qu'à la suivante :

```
    Requête HTTP
         │
         ▼
 ┌───────────────┐   Traduit HTTP ⇄ Python. Ne contient aucune règle métier,
 │   routers/    │   ne construit aucune requête SQL.
 └───────┬───────┘
         ▼
 ┌───────────────┐   Les règles métier : « un agent doit appartenir au service
 │   services/   │   indiqué », « une visite déjà close ne se referme pas ».
 └───────┬───────┘   C'est ici que les transactions sont validées.
         ▼
 ┌───────────────┐   Les requêtes SQL, et rien d'autre. Aucune décision métier.
 │ repositories/ │
 └───────┬───────┘
         ▼
 ┌───────────────┐   Les tables, en classes Python.
 │    models/    │
 └───────────────┘
```

**Ce que cette discipline achète.** Un exemple concret tiré du code : la règle
« l'agent sélectionné doit appartenir au service indiqué » vit dans
[`visit_service.py:93`](app/services/visit_service.py#L93). Elle s'applique donc
identiquement à `POST /visits` et à `POST /visits/sync`, sans être écrite deux
fois. Si elle vivait dans le router, il aurait fallu la dupliquer — et un jour
l'oublier d'un côté.

Second bénéfice : la testabilité. Un service reçoit une `AsyncSession` en
paramètre de constructeur, jamais une variable globale. Le test lui passe une
session SQLite en mémoire, et tout fonctionne sans PostgreSQL.

### Dossier par dossier

#### `app/core/` — l'infrastructure

Tout ce qui n'est ni métier ni HTTP : les fondations que les autres couches
utilisent.

| Fichier | Contenu |
|---|---|
| [`config.py`](app/core/config.py) | Toute la configuration, en un seul objet `settings`. Aucun `os.getenv` ailleurs dans le code. |
| [`database.py`](app/core/database.py) | Création de l'`engine`, fabrique de sessions, dépendance `get_session`. |
| [`security.py`](app/core/security.py) | Hachage des mots de passe, création et décodage des JWT. Aucune dépendance à la base : purement fonctionnel, donc testable en isolation. |
| [`errors.py`](app/core/errors.py) | La hiérarchie d'exceptions métier. |
| [`handlers.py`](app/core/handlers.py) | Conversion de ces exceptions en réponses JSON. |
| [`logging.py`](app/core/logging.py) | Logs JSON, identifiant de corrélation, masquage des données sensibles. |
| [`middleware.py`](app/core/middleware.py) | Attribution d'un `X-Request-ID` par requête, trace de la durée. |
| [`deps.py`](app/core/deps.py) | Les dépendances FastAPI réutilisables (`CurrentUser`, `SessionDep`, les services). |

#### `app/models/` — les tables

Les classes SQLAlchemy. Une par table, plus [`base.py`](app/models/base.py) qui
porte les éléments communs : la classe `Base`, un mixin de clé primaire UUID, un
mixin `created_at` / `updated_at`.

#### `app/schemas/` — les contrats d'API

Les modèles Pydantic. **Ils sont distincts des modèles SQLAlchemy, et c'est
volontaire.** Un modèle décrit ce qui est stocké, un schéma ce qui circule sur le
réseau. Confondre les deux exposerait `mot_de_passe_hash` dans les réponses de
`GET /me` à la première inattention. La séparation permet aussi de faire évoluer
le schéma de base sans casser le contrat public.

#### `app/repositories/` — l'accès aux données

Toutes les requêtes SQL, et elles seules. L'intérêt apparaît sur les filtres du
listing des visites : `_apply_filters` dans
[`visit_repository.py:67`](app/repositories/visit_repository.py#L67) est partagé
par `count()` et `list_paginated()`, ce qui garantit que le total affiché
correspond bien aux éléments listés.

#### `app/services/` — le métier

Une classe par domaine : `AuthService`, `VisitService`, `DashboardService`,
`MrzOcrService`, `StorageService`. C'est la seule couche autorisée à appeler
`session.commit()` — un router qui committerait pourrait valider une transaction
à moitié construite.

#### `app/routers/` — la couche HTTP

Un fichier par domaine fonctionnel. Les fonctions y sont volontairement courtes :
lire l'entrée, appeler le service, renvoyer le schéma de sortie.

#### `alembic/` — l'historique du schéma

`versions/` contient une migration par changement de schéma, chaînées par
`down_revision`. [`env.py`](alembic/env.py) est configuré pour lire l'URL depuis
`settings`, jamais depuis `alembic.ini` — les secrets ne vivent pas dans un
fichier versionné.

#### `tests/` — `unit/` et `integration/`

Les tests unitaires ne touchent pas la base (parsing MRZ, sécurité, preprocessing).
Les tests d'intégration passent par HTTP sur une base SQLite en mémoire.

### Le trajet complet d'une requête

`POST /api/v1/visits` :

```
1. uvicorn reçoit les octets, les passe à l'application ASGI
2. RequestContextMiddleware      → génère X-Request-ID, démarre le chronomètre
3. CORSMiddleware                → vérifie l'origine (navigateurs uniquement)
4. Routage                       → app/routers/visits.py::create_visit
5. Résolution des dépendances    → get_session() ouvre une AsyncSession
                                 → get_current_user() décode le JWT, charge le User
                                 → get_visit_service() construit le VisitService
6. Validation Pydantic du corps  → VisitCreate ; échec = 400 avant tout code métier
7. VisitService.create_visit()   → vérifie les référentiels, upsert le visiteur,
                                   insère la visite, COMMIT
8. Sérialisation                 → VisitRead.model_validate(visit)
9. Middleware                    → log JSON {méthode, chemin, statut, durée, request_id}
```

Si une `AppError` est levée à l'étape 7, le handler global de
[`handlers.py`](app/core/handlers.py) la convertit en
`{"error_code": ..., "message": ..., "details": ...}` avec le bon code HTTP. Le
client voit **toujours** la même forme d'erreur, et jamais une trace d'exécution.

---

## 5. Construction pas à pas

Cette section reconstruit le projet depuis une page blanche. Chaque étape est
autonome et vérifiable.

### 5.1 Initialiser le projet

```bash
uv init sivg-backend --package
cd sivg-backend
uv add "fastapi[standard]" "sqlalchemy[asyncio]" asyncpg alembic pydantic-settings \
       "python-jose[cryptography]" "passlib[bcrypt]" python-multipart
uv add --dev pytest pytest-asyncio pytest-cov httpx aiosqlite ruff mypy
```

Puis configurez les outils dans `pyproject.toml` :

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
# Sans cette ligne, chaque test async exigerait un décorateur @pytest.mark.asyncio.
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
ignore = [
    # `Depends(...)` en valeur par défaut est l'idiome FastAPI, pas un bug.
    "B008",
]
```

### 5.2 La configuration

Toute la configuration passe par une classe unique. Le principe : **aucun
`os.getenv` ailleurs dans le code**, sinon il devient impossible de savoir ce que
l'application attend de son environnement.

```python
# app/core/config.py
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENVIRONMENT: str = "development"
    DATABASE_URL: str = "postgresql+asyncpg://sigv:sigv@localhost:5432/sigv"
    JWT_SECRET_KEY: str = "changeme-dev-secret-key"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    # ...

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
```

`@lru_cache` garantit un objet unique : le fichier `.env` n'est lu qu'une fois.

Deux détails qui comptent :

- `extra="ignore"` — le `.env` peut contenir des variables destinées à d'autres
  outils sans faire échouer le démarrage ;
- un `model_validator` refuse les réglages de développement quand
  `ENVIRONMENT=production` (clé JWT d'exemple, `CORS_ORIGINS=*`, base SQLite).
  **Échouer au démarrage vaut mieux que servir en ligne avec une clé publique.**

### 5.3 La base de données

```python
# app/core/database.py
engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)

SessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,  # les objets restent lisibles après commit
    autoflush=False,         # on contrôle explicitement quand le SQL part
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
```

Trois réglages méritent une explication.

`pool_pre_ping=True` envoie un `SELECT 1` avant de réutiliser une connexion du
pool. Sans lui, une connexion coupée pendant la nuit par un pare-feu ou par le
serveur produit une erreur à la première requête du matin.

`expire_on_commit=False` : par défaut, SQLAlchemy invalide tous les objets après
un commit, et le prochain accès à un attribut déclenche une relecture en base.
En asynchrone, cette relecture implicite lève une erreur (`MissingGreenlet`). On
la désactive, et les services rechargent explicitement quand c'est nécessaire.

`get_session` **ne committe pas**. Le commit appartient à la couche service, qui
seule sait si l'opération métier est complète.

### 5.4 Les modèles

Le socle commun, dans [`app/models/base.py`](app/models/base.py) :

```python
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

**À ne pas sauter.** Sans convention de nommage, PostgreSQL attribue aux
contraintes des noms générés automatiquement, différents d'une base à l'autre.
Une migration qui voudrait supprimer une contrainte anonyme ne saurait pas quoi
nommer, et échouerait en production alors qu'elle passait en développement.

Les clés primaires sont des **UUID** et non des entiers auto-incrémentés. Deux
raisons : le client mobile crée des visites hors ligne et doit pouvoir générer un
identifiant sans consulter le serveur ; et un identifiant séquentiel exposé dans
une URL laisse deviner le volume d'activité et permet d'énumérer les visites.

### 5.5 Les migrations

```bash
uv run alembic init -t async alembic
```

Dans [`alembic/env.py`](alembic/env.py), deux modifications par rapport au
gabarit généré :

```python
from app.core.config import settings
from app.models import Base          # réexporte TOUS les modèles

# L'URL vient de l'environnement, jamais du .ini versionné.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
target_metadata = Base.metadata
```

L'import de `Base` depuis `app.models` est important : si un modèle n'est pas
importé au moment où Alembic inspecte `metadata`, sa table est **absente** de
`metadata` et l'autogénération produira une migration qui la supprime.

```bash
uv run alembic revision --autogenerate -m "initial schema"
uv run alembic upgrade head
```

⚠️ **Relisez toujours la migration générée avant de l'appliquer.** L'autogénération
détecte mal les renommages : renommer une colonne produit un `DROP` suivi d'un
`ADD`, donc une perte de données.

Une garantie automatisée existe dans ce projet :
[`tests/integration/test_migrations.py`](tests/integration/test_migrations.py)
applique les migrations puis compare le schéma obtenu aux modèles, et échoue si
les deux divergent. Un modèle modifié sans migration ne passe pas la CI.

### 5.6 Les erreurs

Une hiérarchie d'exceptions métier, chacune portant son code HTTP et son
`error_code` :

```python
class AppError(Exception):
    status_code: int = 400
    error_code: str = "BAD_REQUEST"
    message: str = "Requête invalide."


class NotFoundError(AppError):
    status_code = 404
    error_code = "NOT_FOUND"


class VisitNotFoundError(NotFoundError):
    error_code = "VISIT_NOT_FOUND"
    message = "Visite introuvable."
```

Un service lève `VisitNotFoundError()` sans se soucier du HTTP. Un handler global
fait la traduction :

```python
@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error_code": exc.error_code, "message": exc.message, "details": exc.details},
    )
```

Le handler générique sur `Exception` est celui qui protège vraiment : il journalise
la trace complète et ne renvoie au client qu'un message neutre. **Une trace
d'exécution en réponse HTTP est une fuite d'information** — chemins du serveur,
versions de bibliothèques, parfois des valeurs de variables.

Un choix de convention à noter : `422` est réservé aux **échecs métier** (un MRZ
illisible), et `400` aux **payloads invalides**. FastAPI renvoie 422 par défaut
pour une erreur de validation ; le handler `RequestValidationError` le ramène à
400 (voir ADR-008).

### 5.7 Les logs

Des logs JSON, un identifiant de corrélation par requête, et un masquage des
clés sensibles :

```python
_SENSITIVE_KEYS = frozenset({"password", "mot_de_passe", "access_token",
                             "token", "authorization", "signature", "mrz_image"})

def scrub(value):
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in _SENSITIVE_KEYS else scrub(v))
                for k, v in value.items()}
    ...
```

Le masquage est fait **à la source**, dans le formateur, et non à chaque appel de
`logger.info`. C'est la seule approche fiable : elle ne dépend pas de la vigilance
de celui qui écrit la ligne de log.

L'identifiant de corrélation est porté par un `ContextVar`, une variable dont la
valeur est propre à la tâche asynchrone en cours. Une variable globale ordinaire
serait écrasée par les requêtes concurrentes.

### 5.8 L'authentification

**Le hachage des mots de passe :**

```python
def hash_password(password: str) -> str:
    payload = password.encode("utf-8")[:72]   # bcrypt tronque au-delà de 72 octets
    return bcrypt.hashpw(payload, bcrypt.gensalt()).decode("utf-8")
```

La troncature à 72 octets est explicite : bcrypt ignore silencieusement le
surplus, et certaines versions lèvent une exception. Mieux vaut trancher soi-même.

**Les jetons.** Deux types, avec des durées de vie très différentes :

| Jeton | Durée | Rôle |
|---|---|---|
| `access` | 30 min | accompagne chaque requête |
| `refresh` | 7 jours | sert uniquement à obtenir un nouvel access token |

Pourquoi deux ? Un access token volé n'est exploitable que 30 minutes. Un refresh
token vit longtemps mais ne circule que rarement, et il est **révocable** : le
logout inscrit son identifiant `jti` dans une table de révocation. Un JWT étant
par nature autoportant, c'est le seul moyen de l'invalider avant son expiration.

Le `jti` (*JWT ID*) est un identifiant unique par jeton, précisément prévu pour
cet usage.

**La dépendance d'authentification :**

```python
async def get_current_user(token: Annotated[str | None, Depends(oauth2_scheme)],
                           auth_service: AuthServiceDep) -> User:
    if not token:
        raise UnauthorizedError("Token d'authentification manquant.")
    return await auth_service.resolve_access_token(token)


CurrentUser = Annotated[User, Depends(get_current_user)]
```

Protéger une route se réduit alors à déclarer un paramètre :

```python
async def list_visits(current_user: CurrentUser, ...): ...
```

Un détail qui fait gagner une heure de débogage : `oauth2_scheme` est construit
avec `auto_error=False`. Par défaut, FastAPI renvoie lui-même un 401 au format
`{"detail": "..."}`, qui ne respecte pas le contrat d'erreur du projet. En
désactivant ce comportement, l'absence de jeton passe par la même exception que
toutes les autres erreurs.

Une remarque honnête sur les rôles : `UserRole` distingue `AGENT_CONTROLE`,
`SUPERVISEUR` et `ADMIN`, mais **aucune route n'est restreinte par rôle**
aujourd'hui (ADR-011). L'énumération existe pour éviter une migration ultérieure.
Si vous reproduisez l'application avec un besoin de permissions, c'est le premier
endroit à compléter.

### 5.9 Repositories et services

Un repository ne fait que des requêtes :

```python
class VisitorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_document(self, type_document, numero_document) -> Visitor | None:
        stmt = select(Visitor).where(
            Visitor.type_document == type_document,
            Visitor.numero_document == numero_document,
        )
        return (await self.session.execute(stmt)).scalars().first()
```

Un service prend les décisions. Exemple représentatif, l'*upsert* du visiteur :

```python
async def _upsert_visitor(self, data: VisitorInput) -> Visitor:
    existing = await self.visitors.get_by_document(data.type_document, data.numero_document)
    payload = data.model_dump(exclude_unset=False)

    if existing is not None:
        for field, value in payload.items():
            if value is not None:      # ne jamais écraser une valeur connue par un None
                setattr(existing, field, value)
        await self.session.flush()
        return existing

    return await self.visitors.add(Visitor(**payload))
```

La règle métier est la condition `if value is not None` : un client qui n'envoie
pas le téléphone du visiteur ne doit pas effacer celui déjà enregistré.

### 5.10 Assembler l'application

```python
def create_app() -> FastAPI:
    application = FastAPI(
        title=settings.PROJECT_NAME,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
    )
    application.add_middleware(RequestContextMiddleware)
    application.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins_list, ...)
    register_exception_handlers(application)

    for router in (auth.router, ocr.router, visits.router, ...):
        application.include_router(router, prefix=settings.API_V1_PREFIX)

    return application


app = create_app()
```

**Pourquoi une fabrique `create_app()` plutôt qu'un `app = FastAPI()` au niveau du
module ?** Parce que les tests ont besoin d'une instance neuve par test, avec ses
propres surcharges de dépendances. Une instance globale partagerait son état entre
tous les tests.

Le `lifespan` gère le démarrage et l'arrêt. C'est là qu'est préchargé le modèle
OCR :

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    if settings.OCR_PRELOAD_MODEL:
        try:
            await anyio.to_thread.run_sync(get_ocr_engine().load)
        except Exception:
            # Un modèle indisponible ne doit pas empêcher l'API de démarrer :
            # seules les routes OCR en pâtiront.
            logger.exception("Préchargement du modèle OCR impossible")
    yield
    await dispose_engine()
```

Deux points : le chargement passe par `anyio.to_thread.run_sync` parce qu'il est
bloquant, et l'échec est rattrapé — l'authentification et le listing des visites
doivent rester disponibles même si l'OCR ne l'est pas.

---

## 6. Le moteur OCR de A à Z

C'est le cœur du projet. Le problème posé : **à partir d'une photo de carte
d'identité prise à la main avec un téléphone, produire un nom, un prénom, une date
de naissance et un numéro de document fiables.**

### 6.1 Comprendre le MRZ

Le MRZ — *Machine Readable Zone* — est la bande de caractères monospacés en bas
des pièces d'identité. La norme **ICAO 9303** la définit. C'est une aubaine pour
un OCR : la police est standardisée (OCR-B), l'alphabet réduit à `A-Z`, `0-9` et
`<`, et **chaque champ occupe une position fixe**.

Trois formats coexistent :

| Format | Lignes × caractères | Documents |
|---|---|---|
| TD1 | 3 × 30 | cartes d'identité (format carte bancaire) |
| TD2 | 2 × 36 | anciens documents de voyage |
| TD3 | 2 × 44 | passeports |

Un TD3 de passeport ressemble à ceci :

```
P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<
L898902C36UTO7408122F1204159ZE184226B<<<<<10
```

Décomposition de la seconde ligne, position par position :

```
L898902C3   6    UTO   740812  2  F   120415  9    ZE184226B<<<<<  1  0
└────────┘  │    └─┘   └────┘  │  │   └────┘  │    └────────────┘  │  │
n° document │  natio.  naissance│ sexe expir. │       optionnel    │  └ contrôle composite
     contrôle du n° ───┘        │             └ contrôle expiration│
                    contrôle naissance ───────┘        contrôle de la zone optionnelle ┘
```

Les `<` sont des remplisseurs. Le premier caractère (`P`) code le type de
document, `I` ou `A` ou `C` désignant une carte d'identité.

**Les chiffres de contrôle sont ce qui rend l'exercice réalisable.** Chaque champ
important est suivi d'un chiffre calculé à partir de son contenu. Si l'OCR
confond un `8` avec un `B`, le chiffre de contrôle ne tombe plus juste et
**l'erreur est détectée**. C'est aussi ce qui permet, plus loin, de trancher
automatiquement l'orientation d'une photo.

### 6.2 Le découpage en trois modules

Le pipeline est réparti en trois modules qui ne se connaissent pas :

```
   bytes de l'image
        │
        ▼
┌──────────────────────────┐
│ image_preprocessing.py   │  OpenCV uniquement. Aucune notion de MRZ ni d'OCR.
│ octets → images prêtes   │  Testable avec des images de synthèse.
└──────────┬───────────────┘
           ▼
┌──────────────────────────┐
│ ocr_engine.py            │  PaddleOCR uniquement. Aucune notion de MRZ.
│ image → lignes de texte  │  Remplaçable par Tesseract sans toucher au reste.
└──────────┬───────────────┘
           ▼
┌──────────────────────────┐
│ mrz_parser.py            │  Texte uniquement. Aucune dépendance à OpenCV.
│ lignes → champs validés  │  Testable sans une seule image.
└──────────┬───────────────┘
           ▼
┌──────────────────────────┐
│ mrz_ocr_service.py       │  Orchestration : essaie, constate, recommence.
└──────────────────────────┘
```

L'intérêt est immédiat en test : `mrz_parser` se teste avec des chaînes de
caractères, sans image, sans modèle neuronal, en quelques millisecondes. Les
tests les plus nombreux et les plus précis du projet sont là.

### 6.3 Étape 1 — décoder l'image

```python
def decode_image(content: bytes) -> np.ndarray:
    array = np.frombuffer(content, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is not None:
        return image

    # OpenCV ne lit pas le HEIC : on repasse par Pillow.
    with Image.open(io.BytesIO(content)) as pil_image:
        rgb = np.array(pil_image.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
```

Une image est désormais un tableau NumPy de dimensions `(hauteur, largeur, 3)`.
**Attention à l'ordre des canaux** : OpenCV travaille en BGR, Pillow en RGB. La
conversion `cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)` n'est pas optionnelle — l'oublier
donne des couleurs inversées, ce qui passe inaperçu en niveaux de gris mais fausse
tout traitement colorimétrique.

Ensuite, réduction à 1600 px de large :

```python
def resize_to_working_width(image, target_width=1600):
    if image.shape[1] <= target_width:
        return image
    scale = target_width / image.shape[1]
    return cv2.resize(image, (target_width, int(image.shape[0] * scale)),
                      interpolation=cv2.INTER_AREA)
```

Une photo de smartphone fait 4000 px de large. Tout le pipeline sur 4000 px coûte
six fois plus cher qu'à 1600 px sans améliorer la lecture du MRZ. `INTER_AREA` est
l'interpolation à utiliser pour **réduire** : elle moyenne les pixels source, là où
`INTER_LINEAR` crée du crénelage.

### 6.4 Étape 2 — trouver et redresser la carte

C'est l'étape qui a le plus amélioré la fiabilité sur photos réelles (ADR-014).

**Le problème.** Sur une photo prise à la main, la carte est posée sur une table,
photographiée de biais, occupe peut-être 40 % de l'image. Chercher le MRZ
directement dans cette photo, c'est le chercher dans un décor.

**L'idée.** Une carte d'identité est un rectangle clair sur un fond généralement
plus sombre. Un seuillage d'Otsu la sépare du fond, et le plus grand contour à
quatre sommets est un bon candidat.

```python
def detect_card(image: np.ndarray) -> np.ndarray | None:
    gray = cv2.GaussianBlur(to_grayscale(image), (7, 7), 0)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        area_ratio = cv2.contourArea(contour) / (height * width)
        if not 0.10 <= area_ratio <= 0.97:
            continue
        approx = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
        if len(approx) != 4:
            continue
        # ... contrôle des proportions
```

Chaque filtre écarte une fausse piste précise :

- **le seuillage d'Otsu** choisit tout seul le seuil qui sépare le mieux les deux
  populations de pixels — pas de constante magique à régler par éclairage ;
- **`approxPolyDP`** simplifie un contour bruité en polygone ; on ne garde que
  ceux à 4 sommets, donc les quadrilatères ;
- **le filtre de surface** (10 % à 97 %) écarte les miettes et le cadre entier de
  la photo ;
- **le contrôle de proportions** est le plus important. Le format ID-1
  (ISO/IEC 7810) mesure 85,6 × 54 mm, soit un rapport de **1,585**. On accepte
  1,35 à 1,85 pour absorber la perspective. Sans ce contrôle, une feuille de
  papier posée à côté de la carte serait retenue.

Puis on redresse par **correction de perspective** :

```python
def rectify_card(image, corners):
    cible = np.array([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]], dtype="float32")
    matrice = cv2.getPerspectiveTransform(corners, cible)
    return cv2.warpPerspective(image, matrice, (W, H))
```

`getPerspectiveTransform` calcule la matrice 3×3 qui envoie les quatre coins
détectés sur les quatre coins d'un rectangle parfait, et `warpPerspective`
l'applique. Le résultat : **toutes les photos, quel que soit l'angle de prise de
vue, deviennent la même image canonique de 1300 × 820 px.**

C'est ce qui débloque la suite. Sur une image canonique, la bande MRZ est toujours
dans le dernier tiers, et le NIN juste au-dessus. On peut cibler par simples
ratios de hauteur, ce qui est impossible sur une photo quelconque.

Une subtilité : `order_corners` trie les quatre points en haut-gauche,
haut-droit, bas-droit, bas-gauche. `findContours` ne garantit aucun ordre, et
mélanger les coins produit une image retournée ou en miroir. L'astuce employée :
la **somme** `x + y` est minimale en haut-gauche et maximale en bas-droit ; leur
**différence** sépare les deux autres.

### 6.5 Étape 3 — isoler la bande MRZ

La carte est redressée ; reste à trouver la bande de texte. La méthode est
**morphologique** — on exploite la forme du MRZ, pas son contenu.

```python
rect_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(13, width // 60), 5))

blackhat = cv2.morphologyEx(blurred, cv2.MORPH_BLACKHAT, rect_kernel)   # 1
grad = cv2.Sobel(blackhat, ddepth=cv2.CV_32F, dx=1, dy=0, ksize=-1)     # 2
grad = normaliser(np.absolute(grad))
grad = cv2.morphologyEx(grad, cv2.MORPH_CLOSE, rect_kernel)             # 3
_, thresh = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, square_kernel)       # 4
thresh = cv2.erode(thresh, None, iterations=4)                          # 5
```

Étape par étape :

1. **Blackhat** — soustrait à l'image sa fermeture morphologique. Traduction : il
   ne reste que les **détails sombres sur fond clair plus petits que le noyau**.
   Du texte noir sur une carte claire, exactement.
2. **Sobel horizontal** — mesure les variations d'intensité selon x. Une zone de
   texte enchaîne les transitions clair/sombre ; une zone unie en est dépourvue.
3. **Fermeture avec un noyau large et plat** (largeur/60 × 5) — colle
   horizontalement les caractères voisins. Une ligne de MRZ devient **un seul
   blob très allongé**.
4. **Fermeture carrée** (21×21) — bouche les trous à l'intérieur des blobs.
5. **Érosion** — sépare les blobs qui se sont involontairement rejoints.

Il ne reste qu'à retenir les contours dont le rapport largeur/hauteur dépasse 4 et
qui couvrent plus de la moitié de la largeur — la signature d'une ligne de MRZ.

**Deux corrections apprises sur photos réelles**, visibles dans le code :

```python
# Chaque ligne du MRZ ressort souvent comme un blob distinct : on prend l'union
# des candidats, sans quoi seule une des 2 ou 3 lignes serait transmise à l'OCR.
left  = min(box[0] for box in candidates)
top   = min(box[1] for box in candidates)
...
# Marge de sécurité : le contour érodé rogne souvent la première/dernière ligne.
pad_x, pad_y = int(width * 0.02), int(h * 0.35)
```

Et un **repli** indispensable : si aucune bande n'est détectée, on garde la moitié
basse de la carte, où le MRZ se trouve par construction.

```python
top = int(gray.shape[0] * settings.OCR_MRZ_BAND_TOP_RATIO)   # 0.5 par défaut
gray = gray[top:, :]
```

Une détection sophistiquée qui échoue une fois sur dix est pire qu'un repli
grossier qui marche toujours. Ici, on a les deux.

### 6.6 Étape 4 — contraste et binarisation

```python
def enhance_contrast(gray):
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def binarize(gray):
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, 25, 15)
```

**CLAHE** — *Contrast Limited Adaptive Histogram Equalization* — égalise le
contraste **par tuiles** de 8×8 plutôt que globalement. Sur une photo où un coin
est dans l'ombre et l'autre au soleil, une égalisation globale sacrifie l'un des
deux ; CLAHE traite chaque zone selon son propre éclairage. Le `clipLimit` borne
l'amplification pour ne pas transformer le bruit en faux contours.

**Le seuillage adaptatif** suit la même logique : chaque pixel est comparé à la
moyenne pondérée de son voisinage de 25×25, moins une constante de 15. Un seuil
global à 128 noircirait entièrement la zone d'ombre.

Enfin, si la bande fait moins de 1000 px de large, on l'agrandit en `INTER_CUBIC` :
les modèles OCR sont entraînés sur des caractères d'une certaine taille, et
agrandir avant l'inférence améliore nettement la lecture des petits caractères.

### 6.7 Étape 5 — la reconnaissance de texte

```python
class PaddleOcrEngine:
    def __init__(self) -> None:
        self._ocr: Any | None = None
        self._lock = threading.Lock()

    def load(self) -> None:
        if self._ocr is not None:
            return
        with self._lock:
            if self._ocr is not None:      # double vérification sous verrou
                return
            from paddleocr import PaddleOCR
            self._ocr = PaddleOCR(
                lang=settings.OCR_LANG,
                text_detection_model_name=settings.OCR_DET_MODEL,
                text_recognition_model_name=settings.OCR_REC_MODEL,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                device="gpu" if settings.OCR_USE_GPU else "cpu",
                enable_mkldnn=settings.OCR_ENABLE_MKLDNN,
            )
```

**Le patron du singleton verrouillé.** Le modèle pèse plusieurs centaines de Mo et
met des secondes à charger. On le charge une fois ; le verrou évite que deux
requêtes simultanées ne le chargent deux fois, et la seconde vérification à
l'intérieur du verrou évite de payer le coût du verrou dans le cas courant où le
modèle est déjà là.

**Les trois modules désactivés** méritent une explication. PaddleOCR propose une
classification d'orientation du document, un dépliage (*unwarping*) et une
détection d'orientation par ligne. Tous coûtent cher — et **tous font déjà l'objet
d'un traitement OpenCV en amont**, plus rapide et mieux maîtrisé. Les laisser
activés reviendrait à payer deux fois le même travail.

**Les modèles « mobile » plutôt que « medium »** (ADR-013) : mesurés trois fois
plus rapides sur CPU, pour une lecture du MRZ strictement identique. Le MRZ est
une police standardisée et contrastée ; il n'a pas besoin du modèle le plus lourd.

**`enable_mkldnn=False` par défaut** : oneDNN, l'accélérateur CPU d'Intel, est
cassé dans PaddlePaddle 3.3 sous Windows — toute inférence lève
`ConvertPirAttribute2RuntimeAttribute not support`. Sur Linux, il fonctionne et
peut être réactivé.

Un dernier détail, à cause duquel on peut perdre une heure :

```python
def to_bgr(image: np.ndarray) -> np.ndarray:
    """PaddleOCR rejette les tableaux 2D."""
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    return image
```

Le preprocessing produit du niveau de gris binarisé, donc un tableau à deux
dimensions. PaddleOCR attend trois canaux et échoue sinon sur un obscur
`ValueError: not enough values to unpack`.

La sortie est normalisée par `_extract_lines`, qui gère les deux formats de
réponse (PaddleOCR ≥ 3 renvoie des dictionnaires, les versions antérieures des
listes) et **trie les lignes par position verticale** — l'ordre de détection n'est
pas l'ordre de lecture, et l'ordre des lignes est significatif dans un MRZ.

### 6.8 Étape 6 — nettoyer et identifier le format

On entre dans le parsing pur.

```python
_ALLOWED_CHARS = re.compile(r"[^A-Z0-9<]")

def sanitize_line(line: str) -> str:
    cleaned = line.strip().upper().replace(" ", "").replace("«", "<").replace("К", "K")
    return _ALLOWED_CHARS.sub("", cleaned)
```

Les deux remplacements ciblés viennent d'observations réelles : l'OCR rend
fréquemment `<<` en `«`, et confond le **K cyrillique** avec le K latin — deux
caractères visuellement identiques, mais de points de code différents.

Puis on filtre les libellés imprimés captés au passage :

```python
cleaned = [line for line in cleaned
           if (len(line) >= 15 and "<" in line) or len(line) >= _MIN_MRZ_LINE_LENGTH]
```

Une ligne MRZ contient presque toujours un remplisseur `<`, et à défaut atteint la
longueur minimale d'un format normalisé (30 caractères). « RÉPUBLIQUE DU SÉNÉGAL »
ne passe ni l'un ni l'autre.

Le format se déduit du nombre de lignes et de leur longueur :

```python
def detect_format(lines: list[str]) -> MrzFormat | None:
    candidates = [fmt for fmt, (n, _) in _FORMAT_SPECS.items() if n == len(lines)]
    # On compare à la ligne la PLUS LONGUE : l'OCR tronque bien plus souvent
    # qu'il n'allonge, une moyenne ferait basculer un TD3 abîmé vers TD2.
    longest = max(len(line) for line in lines)
    return min(candidates, key=lambda fmt: abs(_FORMAT_SPECS[fmt][1] - longest))
```

Et si rien ne colle, une **seconde chance** : l'OCR a pu fusionner les lignes. On
recolle tout et on redécoupe aux longueurs normalisées.

```python
joined = "".join(cleaned)
for fmt, (count, length) in _FORMAT_SPECS.items():
    if len(joined) == count * length:
        cleaned = [joined[i * length:(i + 1) * length] for i in range(count)]
```

### 6.9 Étape 7 — les corrections positionnelles

Voici où la structure fixe du MRZ devient une arme.

`0`/`O`, `1`/`I`, `5`/`S`, `8`/`B` sont les confusions OCR classiques. On ne peut
pas les corriger globalement — un vrai `O` existe dans les noms. Mais **on connaît
le type de chaque position** : la date de naissance d'un TD3 occupe les positions
13 à 18 de la ligne 2 et ne peut être que numérique.

```python
_TO_DIGIT = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1",
                           "S": "5", "B": "8", "Z": "2", "G": "6"})
_TO_ALPHA = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G"})

if candidate.mrz_format is MrzFormat.TD3:
    fix(1,  9, 10, _TO_DIGIT)   # chiffre de contrôle du n° de document
    fix(1, 10, 13, _TO_ALPHA)   # code nationalité (3 lettres)
    fix(1, 13, 20, _TO_DIGIT)   # date de naissance + son contrôle
    fix(1, 21, 28, _TO_DIGIT)   # date d'expiration + son contrôle
    fix(1, 43, 44, _TO_DIGIT)   # contrôle composite
    fix(0,  2,  5, _TO_ALPHA)   # code pays émetteur
```

Le numéro de document n'est **jamais** retouché : il peut légitimement être
alphanumérique.

### 6.10 Étape 8 — les chiffres de contrôle et le débordement

**L'algorithme ICAO 9303**, en dix lignes :

```python
_CHECK_WEIGHTS = (7, 3, 1)

def compute_check_digit(value: str) -> str:
    total = 0
    for index, char in enumerate(value):
        if char == "<":
            digit = 0
        elif char.isdigit():
            digit = int(char)
        elif "A" <= char <= "Z":
            digit = ord(char) - 55        # A→10, B→11, … Z→35
        else:
            return ""
        total += digit * _CHECK_WEIGHTS[index % 3]
    return str(total % 10)
```

Vérifions sur l'exemple officiel de la norme, `L898902C3`, dont le chiffre de
contrôle est `6` :

| Caractère | L | 8 | 9 | 8 | 9 | 0 | 2 | C | 3 |
|---|---|---|---|---|---|---|---|---|---|
| Valeur | 21 | 8 | 9 | 8 | 9 | 0 | 2 | 12 | 3 |
| Poids | 7 | 3 | 1 | 7 | 3 | 1 | 7 | 3 | 1 |
| Produit | 147 | 24 | 9 | 56 | 27 | 0 | 14 | 36 | 3 |

Somme = 316, et 316 mod 10 = **6**. ✓

**Le cas des CNI sénégalaises** (ADR-005) est le point le plus délicat du projet.

Le champ `document_number` du MRZ est plafonné à **9 caractères**. Or le numéro
d'une CNI sénégalaise en compte **17**. La norme prévoit ce cas — c'est la
*convention de débordement* :

- les 9 premiers caractères dans `document_number` ;
- un `<` **à la place du chiffre de contrôle**, qui signale le débordement ;
- le **reste du numéro** au début de la zone `optional_data`, suivi du chiffre de
  contrôle calculé sur le numéro **entier**, puis d'un `<` de terminaison.

La bibliothèque `mrz` ne gère pas cette convention : elle compare le `<` au chiffre
attendu, échoue, et **déclarerait invalide toute CNI sénégalaise**. D'où ce
traitement propre au projet :

```python
def _resolve_document_number(candidate) -> tuple[str, bool]:
    layout = _DOC_NUMBER_LAYOUT[candidate.mrz_format]
    line = candidate.lines[layout.line]
    head = line[layout.start:layout.end]
    check_char = line[layout.check]
    optional = line[layout.optional_start:layout.optional_end]

    if check_char != "<":
        # Cas nominal : numéro court, chiffre de contrôle en place.
        return head.replace("<", "").strip(), compute_check_digit(head) == check_char

    overflow = optional.split("<")[0]
    if len(overflow) < 2:
        return head.replace("<", "").strip(), False

    numero = (head + overflow[:-1]).replace("<", "").strip()
    return numero, compute_check_digit(numero) == overflow[-1]
```

Conséquence : `bool(checker)` de la bibliothèque `mrz` serait toujours faux dès
qu'il y a débordement. La validité globale est donc réévaluée à partir des
contrôles structurels de la bibliothèque **plus** nos propres checksums :

```python
structural_ok = all(result for label, result in report if label not in _RECOMPUTED_CHECKS)
is_valid = structural_ok and all(dict(checksums).values())
```

Dernier point de conception, important côté métier : **un checksum invalide ne
bloque pas le flux.** La réponse porte `mrz_valid: false` et le détail par champ
dans `checksum_details`, pour que l'agent corrige à la main sur son mobile. Une
API qui refuserait la photo laisserait l'agent sans solution.

### 6.11 Étape 9 — le NIN

Le NIN — Numéro d'Identification National, 13 chiffres — est demandé par la
spécification. Il **n'est pas dans le MRZ** : la zone `optional_data` porte la fin
du numéro de carte, comme on vient de le voir. Vérifié sur deux cartes réelles.

Mais il est **imprimé juste au-dessus de la bande MRZ**. D'où l'idée qui évite
toute saisie manuelle (ADR-014) : élargir la zone envoyée à l'OCR pour englober
cette ligne, et récupérer le NIN **dans la même passe**.

```python
# _bottom_zone : on remonte de 10 % de la hauteur au-dessus de la bande MRZ
top = max(0, region[1] - int(0.10 * height))
```

Puis, à l'extraction, deux stratégies de la plus sûre à la plus permissive :

```python
def extract_nin(ocr_lines: list[str]) -> str | None:
    # 1. Une ligne portant le libellé « NIN ». L'OCR colle souvent le libellé
    #    au premier chiffre (« NIN1 895 … »), d'où le simple filtrage des chiffres.
    for ligne in ocr_lines:
        normalisee = ligne.upper().replace(" ", "")
        if "NIN" in normalisee:
            chiffres = re.sub(r"\D", "", normalisee)
            if len(chiffres) == 13:
                return chiffres

    # 2. À défaut, une ligne réduite à exactement 13 chiffres. La condition sur les
    #    caractères non numériques évite qu'une ligne bavarde totalisant 13 chiffres
    #    ne passe pour un NIN.
    for ligne in ocr_lines:
        chiffres = re.sub(r"\D", "", ligne)
        if len(chiffres) == 13 and len(re.sub(r"[\s\d]", "", ligne)) <= 1:
            return chiffres
    return None
```

Le gain est double : aucune passe OCR supplémentaire, donc aucun coût de latence,
et aucune saisie manuelle pour l'agent.

À noter : le numéro de carte encode lui-même la date de naissance
(`1` + `01` + `AAAAMMJJ` + séquence + clé), ce qui offre un contrôle de cohérence
croisé avec le MRZ, pas encore exploité.

### 6.12 La stratégie d'orientation

Une photo prise à la main peut arriver dans n'importe quel sens, et les
métadonnées EXIF ne sont pas fiables — elles disparaissent souvent après un
recadrage côté mobile.

**L'astuce du projet : ne pas deviner l'orientation, mais la déduire du résultat.**
Un MRZ dont tous les chiffres de contrôle se recomposent ne peut pas être le fruit
du hasard. On essaie donc les orientations candidates et on retient la première
qui se parse.

```python
def preprocess_candidates(content: bytes) -> list[PreprocessedImage]:
    base = resize_to_working_width(decode_image(content))

    card = extract_card(base)
    if card is not None:
        # La carte est déjà redressée et remise en paysage : il ne reste que
        # l'ambiguïté haut/bas, soit DEUX candidats au lieu de quatre.
        return [_bottom_zone(card, rotation=0),
                _bottom_zone(rotate(card, 180), rotation=180)]

    # Carte non localisée : repli sur l'image entière et les quatre quarts de tour.
    ...
```

Puis, côté service :

```python
def _run_blocking_pipeline(self, content: bytes) -> MrzScanResponse | None:
    for candidate in preprocess_candidates(content):
        lines = self._engine.read_lines(candidate.image)
        if not lines:
            continue
        try:
            return parse_mrz_lines(lines)
        except (MrzNotDetectedError, MrzParsingError):
            continue          # cette orientation ne donne rien, on passe à la suivante
    return None
```

Deux effets de bord agréables : **le coût est nul dans le cas nominal** (la photo
droite est essayée en premier et réussit), et la détection de carte divise par
deux le nombre d'essais dans le pire cas.

Un détail subtil dans l'ordre des candidats :

```python
# Les replis restent dans la liste : la détection morphologique produit des faux
# positifs, et s'arrêter à la première orientation « détectée » ferait manquer
# la bonne.
return detectees + replis
```

### 6.13 Ne pas bloquer la boucle d'événements

Le pipeline complet est du calcul CPU pur, et dure plusieurs secondes. Exécuté
directement dans une route `async`, il **gèlerait toutes les autres requêtes** —
la boucle d'événements asyncio est mono-thread.

```python
response = await anyio.to_thread.run_sync(self._run_blocking_pipeline, content)
```

Une seule ligne, mais elle change tout : le scan part dans un thread, la boucle
reste libre de servir les autres requêtes. C'est la règle générale en asynchrone :
**tout appel bloquant de plus de quelques millisecondes doit partir dans un
thread.**

### 6.14 Performance et débogage

Mesuré sur la machine de développement : **~5,5 s par scan**, dont la quasi-totalité
en inférence OCR ; le preprocessing ne pèse que 0,3 s. Le budget de 3 s de la
spécification n'est donc pas tenu (ADR-013). Les leviers, par ordre d'efficacité :

1. **GPU** — `OCR_USE_GPU=true` avec un `paddlepaddle-gpu` ; gain d'un ordre de
   grandeur ;
2. **oneDNN fonctionnel** — `OCR_ENABLE_MKLDNN=true` sur Linux ;
3. **réduire le nombre d'orientations essayées** — un cadre de capture guidé côté
   Flutter garantirait la première.

**Pour déboguer le pipeline**, la méthode la plus efficace est d'écrire les images
intermédiaires sur disque :

```python
cv2.imwrite("debug_01_carte.png", card)
cv2.imwrite("debug_02_bande.png", candidate.image)
print(self._engine.read_lines(candidate.image))
```

Neuf fois sur dix, le problème saute aux yeux : la carte n'a pas été détectée, ou
la bande est coupée à mi-hauteur. Les logs aident aussi — le pipeline trace
`« Carte détectée et redressée »`, `« Orientation écartée »`, `« MRZ lu après
rotation »`.

⚠️ **N'écrivez jamais ces images de débogage en production** : ce sont des données
personnelles. Le code applicatif ne journalise volontairement ni l'image ni son
contenu.

---

## 7. La stratégie de tests

223 tests, répartis en deux familles.

**Les tests unitaires** (`tests/unit/`) ne touchent ni la base ni le réseau. Ils
couvrent le parsing MRZ, les checksums, le preprocessing, la sécurité, le
stockage, la configuration. Ce sont les plus nombreux, parce que ce sont les plus
précis : un cas de MRZ tordu s'exprime en trois lignes de chaîne de caractères.

**Les tests d'intégration** (`tests/integration/`) passent par HTTP, avec `httpx`
branché directement sur l'application ASGI — sans serveur à lancer.

La base de test est **SQLite en mémoire** (ADR-007) :

```python
engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,      # une connexion unique partagée : sinon chaque
)                              # connexion verrait sa propre base vide
```

Aucun service externe n'est requis, la suite tourne en 80 secondes. Le compromis
assumé : SQLite n'est pas PostgreSQL. C'est pourquoi
[`test_migrations.py`](tests/integration/test_migrations.py) vérifie séparément
que les migrations Alembic restent alignées sur les modèles.

Une fixture mérite d'être copiée — la surcharge de dépendance :

```python
@pytest.fixture
async def client(engine, session):
    app = create_app()

    async def override_get_session():
        yield session          # la MÊME session que la fixture `seeded`

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
        yield c
```

C'est le mécanisme d'injection de dépendances de FastAPI retourné au profit du
test : l'application ne sait pas qu'elle parle à SQLite.

---

## 8. Pièges rencontrés, et comment les éviter

Les erreurs qui ont réellement coûté du temps sur ce projet, rassemblées ici pour
qu'elles n'en coûtent pas deux fois.

**PaddleOCR rejette les images à 2 dimensions.** Le preprocessing produit du
niveau de gris ; il faut reconvertir en 3 canaux. L'erreur (`not enough values to
unpack`) ne dit rien de la cause.

**passlib 1.7.4 est incompatible avec bcrypt ≥ 4.1.** Sa détection de backend lit
`bcrypt.__about__`, disparu. Appeler `bcrypt` directement règle le problème.

**`expire_on_commit=True` casse l'asynchrone.** Après un commit, lire un attribut
déclenche une relecture en base, interdite hors greenlet. Symptôme :
`MissingGreenlet`.

**Un rollback expire toutes les instances de la session.** D'où, dans
`sync_visits`, le passage de `user_id` et non de l'objet `User` : après le premier
rollback du batch, `current_user.id` déclencherait une IO synchrone.

**Sans convention de nommage sur `MetaData`**, les migrations Alembic génèrent des
contraintes anonymes impossibles à supprimer proprement plus tard. À poser dès le
premier jour, cela ne se rattrape pas.

**L'autogénération Alembic ne détecte pas les renommages.** Elle produit un `DROP`
puis un `ADD`, donc une perte de données. Relire chaque migration.

**Le `+` dans une query string est interprété comme une espace.** Pour les filtres
de dates, écrire `2026-08-01T00:00:00Z` et non `+00:00`.

**Une variable vide dans un `.env` n'est pas une variable absente.** `ENABLE_DOCS=`
donne la chaîne vide, que Pydantic refuse de convertir en booléen. D'où le
validateur qui traite le vide comme « valeur automatique ».

**Le premier démarrage télécharge plusieurs centaines de Mo de modèles.** Pour
développer sans OCR : `OCR_PRELOAD_MODEL=false`.
