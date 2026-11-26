# Fp-visites — API

API de **Fp-visites**, SaaS multi-organisations de gestion des visites aux postes d'accueil.
L'app mobile lit la zone MRZ des pièces d'identité ; un OCR serveur de secours peut être activé.

**Stack** : FastAPI · Python 3.12 · SQLAlchemy 2 async + Alembic · PostgreSQL 16+ · PyJWT · bcrypt · uv

| Document | Contenu |
|---|---|
| [`INTEGRATION_FRONTEND.md`](INTEGRATION_FRONTEND.md) | Contrat d'API pour le web et le mobile |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Installer et mettre à jour un serveur |
| [`PRODUCTION_SAAS.md`](PRODUCTION_SAAS.md) | Architecture cible et feuille de route |
| [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md) | Décisions techniques (ADR) |
| [`audit/AUDIT_2026-11-26.md`](audit/AUDIT_2026-11-26.md) | Audit sécurité et suivi des correctifs |

---

## Démarrer en local

```bash
uv sync
cp .env.example .env              # renseigner DATABASE_URL et JWT_SECRET_KEY
uv run alembic upgrade head
uv run python -m app.seeds        # données et comptes de démonstration
uv run uvicorn app.main:app --reload
```

- Swagger : <http://localhost:8000/docs> (bouton **Authorize** avec un compte ci-dessous)
- Santé : `/health` (processus) · `/health/ready` (base)
- Sans OCR en développement : `OCR_PRELOAD_MODEL=false` (évite le téléchargement de PaddleOCR)

### Comptes de démonstration

Mot de passe : `SEED_AGENT_PASSWORD` (défaut `FpVisites@2026`).

| Identifiant | Rôle | Usage |
|---|---|---|
| `agent001` | `AGENT_CONTROLE` | app mobile |
| `superviseur001` | `SUPERVISEUR` | app mobile + statistiques |
| `orgadmin001` | `ORG_ADMIN` | dashboard de l'organisation `MFPTRSP` |
| `admin` | `ADMIN` | dashboard de toute la plateforme |

Le seed est réservé au développement. En production : `app.bootstrap`, puis `app.create_user` (voir `DEPLOYMENT.md`).

---

## Rôles

`ADMIN` est le seul rôle sans organisation : il voit tout le parc. Les autres ne voient que leur organisation.
Un compte non-`ADMIN` sans organisation est refusé (`403 UNSCOPED_ACCOUNT`).

| | Agent | Superviseur | Org admin | Admin |
|---|---|---|---|---|
| Enregistrer / clôturer des visites | ✓ | ✓ | ✓ | ✓ |
| Statistiques | | ✓ | ✓ | ✓ |
| Comptes, services, agents, motifs, charte, journal d'audit | | | son organisation | tout |
| Réinitialiser le mot de passe d'un org admin | | | | ✓ |
| Sécurité, rétention, OCR serveur, organisations | | | | ✓ |

Le rôle est relu en base à chaque requête, jamais pris dans le JWT.

---

## Commandes utiles

| Besoin | Commande |
|---|---|
| Créer un admin de plateforme | `uv run python -m app.create_user admin002 --role ADMIN --nom "Awa Ndiaye"` |
| Créer un compte d'organisation | `uv run python -m app.create_user agent007 --organisation MFPTRSP [--role ORG_ADMIN]` |
| Importer l'annuaire (simulation) | `uv run python -m app.import_annuaire annuaire.csv --organisation MFPTRSP --dry-run` |
| Importer l'annuaire | même commande sans `--dry-run` (`--mettre-a-jour` rafraîchit les fonctions) |
| Clôture automatique des visites | `uv run python -m app.auto_close_visits [--dry-run]` |
| Purge des photos de pièces | `uv run python -m app.purge_documents [--dry-run]` |
| Purge des sessions expirées | `uv run python -m app.purge_sessions [--dry-run]` |

- `create_user` sans `--mot-de-passe` génère un mot de passe fort et l'affiche **une seule fois**.
- `import_annuaire` ne supprime rien et peut être relancé sans créer de doublons. Le CSV contient des données personnelles : ne pas le versionner.
- Les trois dernières commandes tournent en production via les timers de [`deploy/systemd/`](deploy/systemd/).

---

## Tests et qualité

Ce sont les mêmes vérifications que la CI :

```bash
uv run pytest -q          # SQLite en mémoire, aucun service externe
uv run ruff check .
uv run mypy app
```

Couverture : `uv run pytest --cov --cov-report=term-missing`.
La CI lance aussi `pip-audit` et `gitleaks` (job `securite`).

Pour tester à la main : la collection [`postman/`](postman/) (importer les deux fichiers, choisir l'environnement **Fp-visites — Local**, lancer **Auth → Login**). Les tokens sont capturés automatiquement.

---

## Brancher l'app mobile en local

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

```powershell
# PowerShell administrateur, une seule fois
New-NetFirewallRule -DisplayName "Fp-visites 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
```

| Cible | `baseUrl` |
|---|---|
| Téléphone (même Wi-Fi) | `http://<IP-du-PC>:8000` (`ipconfig`) |
| Émulateur Android | `http://10.0.2.2:8000` |
| Simulateur iOS / web | `http://localhost:8000` (web : ajouter l'origine à `CORS_ORIGINS`) |

Le HTTP en clair doit être autorisé côté app en développement seulement.

---

## Règles d'API à connaître

- Toutes les routes sont sous `/api/v1` et demandent un bearer token, sauf `/auth/login`, `/auth/refresh` et `/auth/forgot-password`. La liste complète est sur `/docs`.
- **Erreurs** : toujours `{ "error_code", "message", "details" }`, sans stacktrace.
- **Token** : l'access token dure 30 min. Sur un `401`, appeler `POST /auth/refresh` puis rejouer la requête.
- **Verrouillage** : 5 échecs de connexion bloquent le compte 15 min (réglable dans `/settings`).
- **Pas de suppression physique** des référentiels : on désactive avec `PATCH …/status`. Toute modification est tracée dans `audit_logs`.
- **Fiches visiteurs** : propres à chaque organisation (ADR-026).
- **Synchro hors ligne** (`POST /visits/sync`) : idempotente grâce à `client_reference`. Une visite datée de plus de 7 jours est refusée (`TIMESTAMP_TOO_OLD`, réglable par `VISIT_MAX_BACKDATE_DAYS`).
- **Images** : jpg, png, heic, 10 Mo et 50 mégapixels au maximum. Les URLs de fichiers sont relatives ; en production, elles sont signées et valables 1 h (`410` une fois échues, relire la visite pour en obtenir une nouvelle).
- **Import en masse** (`/services/import`, `/agents/import`) : modèle `.xlsx` → aperçu → application en tout ou rien.
- **OCR serveur** (`POST /ocr/scan`) : désactivé par défaut (`503 SERVER_OCR_DISABLED`). Il compte 5 à 15 s par scan : prévoir un timeout client de 60 s.

---

## Production

Toute la procédure est dans [`DEPLOYMENT.md`](DEPLOYMENT.md). Les fichiers prêts à copier sont dans [`deploy/`](deploy/), et le modèle de configuration dans [`.env.production.example`](.env.production.example).

Avec `ENVIRONMENT=production` :

- `/docs` est masqué ;
- les fichiers sont servis par nginx, avec des URLs signées ;
- l'API refuse de démarrer si la configuration n'est pas sûre : clé JWT d'exemple, `CORS_ORIGINS=*`, base SQLite, `DB_ECHO=true` ou `STORAGE_URL_SIGNING_KEY` absente.

Avant chaque mise à jour, sauvegarder la base, puis lancer :

```bash
uv run alembic upgrade head
sudo nginx -t && sudo systemctl reload nginx
```

---

## Structure

```
app/
├── core/          config, sécurité, base, erreurs, dépendances
├── models/        modèles SQLAlchemy
├── schemas/       schémas Pydantic
├── routers/       couche HTTP, qui appelle les services
├── services/      logique métier
└── repositories/  accès à la base (seul endroit où l'on écrit du SQL)
alembic/           migrations
tests/             unit/ et integration/
deploy/            nginx, systemd, scripts
```
