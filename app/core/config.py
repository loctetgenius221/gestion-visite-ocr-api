from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Valeur livrée dans `.env.example` : utilisable en développement, jamais en production.
DEV_JWT_SECRET = "changeme-dev-secret-key"
MIN_JWT_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """Configuration centralisée de l'application, lue depuis l'environnement (.env en dev)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Général ---
    PROJECT_NAME: str = "Fp-visites Backend"
    ENVIRONMENT: str = "development"
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # --- Base de données ---
    DATABASE_URL: str = "postgresql+asyncpg://sigv:sigv@localhost:5432/sigv"
    DB_ECHO: bool = False
    # Connexions ouvertes par **chaque worker** : le total vaut
    # `nœuds × WEB_CONCURRENCY × (DB_POOL_SIZE + DB_MAX_OVERFLOW)` et doit rester
    # sous `max_connections` de PostgreSQL (ou sous le pool de PgBouncer).
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    # Délai d'attente d'une connexion libre avant erreur, en secondes.
    DB_POOL_TIMEOUT: int = 30
    # Recycle les connexions plus vieilles que ce délai : un pare-feu ou un
    # load balancer coupe tôt ou tard une connexion TCP inactive.
    DB_POOL_RECYCLE_SECONDS: int = 1800
    # Une requête qui dépasse ce délai est annulée par PostgreSQL, au lieu de
    # monopoliser une connexion et un worker. `0` désactive la borne.
    DB_STATEMENT_TIMEOUT_MS: int = 30_000
    # `true` derrière PgBouncer en mode transaction : asyncpg ne doit alors ni
    # préparer ni mettre en cache de requêtes, et les paramètres de session
    # (`statement_timeout`) se règlent côté PgBouncer.
    DB_USE_PGBOUNCER: bool = False

    # --- Redis / Celery ---
    # Non utilisés pour le moment : voir ARCHITECTURE_DECISIONS.md (ADR-002).
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"

    # --- Sécurité / JWT ---
    JWT_SECRET_KEY: str = "changeme-dev-secret-key"
    # Clé précédente, acceptée **en vérification seulement** le temps d'une
    # rotation : les jetons signés avec elle restent valides jusqu'à leur
    # expiration, les nouveaux sont signés avec `JWT_SECRET_KEY`. À vider une
    # fois `REFRESH_TOKEN_EXPIRE_DAYS` écoulés.
    JWT_SECRET_KEY_PREVIOUS: str = ""
    JWT_ALGORITHM: str = "HS256"
    # Court volontairement : l'access token accompagne chaque requête, donc c'est
    # lui qui fuite en premier. Sa brièveté est invisible pour l'agent tant que le
    # client renouvelle en silence via `/auth/refresh`.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    # Durée d'une session sur l'appareil. Le poste d'accueil tourne toute la
    # journée : une reconnexion hebdomadaire était une gêne inutile.
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    # Renouvellement glissant : passé la moitié de sa vie, le refresh token est
    # remplacé lors d'un rafraîchissement. Un agent qui utilise l'application
    # régulièrement n'est donc jamais déconnecté ; un appareil laissé de côté voit
    # sa session expirer d'elle-même. Voir ADR-015.
    REFRESH_TOKEN_SLIDING: bool = True

    # --- CORS ---
    # Origines autorisées, séparées par des virgules (ex: "http://localhost:3000,https://app.example.com")
    CORS_ORIGINS: str = "http://localhost:3000"

    # --- Déploiement / reverse proxy ---
    # Préfixe ajouté par le proxy quand l'API n'est pas montée à la racine
    # (ex: "/fp-visites"). Laissé vide dans la configuration nginx fournie.
    ROOT_PATH: str = ""
    # Noms d'hôte acceptés, séparés par des virgules. "*" désactive le contrôle ;
    # renseignez le domaine réel en production pour couper les attaques par
    # en-tête Host falsifié.
    TRUSTED_HOSTS: str = "*"
    # `None` = automatique : documentation exposée hors production, masquée en production.
    ENABLE_DOCS: bool | None = None
    # `None` = automatique : l'API sert `storage/uploads` hors production ; en
    # production ces fichiers sont servis par le reverse proxy.
    SERVE_STORAGE: bool | None = None

    # --- Stockage fichiers ---
    STORAGE_DIR: str = "storage/uploads"
    STORAGE_PUBLIC_BASE_URL: str = "/storage/uploads"
    # Clé partagée avec nginx (`secure_link_md5`) pour signer les URLs des
    # photos de pièces et des signatures. Vide : URLs non signées (développement).
    # Obligatoire en production — voir DEPLOYMENT.md, « Accès aux fichiers ».
    STORAGE_URL_SIGNING_KEY: str = ""
    # Durée de validité d'une URL signée. Le client la reçoit à chaque lecture
    # d'une visite : il suffit qu'elle couvre le temps d'affichage d'un écran.
    STORAGE_URL_TTL_SECONDS: int = 3600
    MAX_UPLOAD_SIZE_MB: int = 10
    # Un logo est une image d'interface, pas une photo de pièce : le plafond des
    # uploads MRZ y serait dix fois trop large, et un fichier accepté est un
    # fichier stocké puis servi à chaque chargement du dashboard.
    MAX_LOGO_SIZE_MB: int = 2
    # Côté le plus long du logo après normalisation. Au-delà, le dashboard
    # redimensionnerait de toute façon à l'affichage, en payant la bande passante.
    LOGO_MAX_DIMENSION_PX: int = 512

    # --- OCR ---
    OCR_USE_GPU: bool = False
    OCR_LANG: str = "en"
    # oneDNN est cassé dans PaddlePaddle 3.3 sous Windows (voir ADR-013).
    OCR_ENABLE_MKLDNN: bool = False
    # Modèles « mobile » : 3x plus rapides que les « medium » sur CPU, pour une
    # lecture du MRZ strictement identique sur photo réelle (voir ADR-013).
    OCR_DET_MODEL: str = "PP-OCRv5_mobile_det"
    OCR_REC_MODEL: str = "PP-OCRv5_mobile_rec"
    # Charge le modèle PaddleOCR au démarrage (lifespan). Désactivé en test pour ne pas
    # payer le coût d'initialisation du modèle.
    OCR_PRELOAD_MODEL: bool = True
    # Bande verticale du document dans laquelle chercher le MRZ (ratio de la hauteur).
    OCR_MRZ_BAND_TOP_RATIO: float = 0.5

    @field_validator("ENABLE_DOCS", "SERVE_STORAGE", mode="before")
    @classmethod
    def _blank_means_auto(cls, value: object) -> object:
        """Traite `VARIABLE=` (présente mais vide) comme « valeur automatique ».

        Sans cela, un `.env` recopié depuis l'exemple sans être complété ferait
        échouer le démarrage sur une erreur de parsing booléen.
        """
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def max_upload_size_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @property
    def max_logo_size_bytes(self) -> int:
        return self.MAX_LOGO_SIZE_MB * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}

    @property
    def trusted_hosts_list(self) -> list[str]:
        return [host.strip() for host in self.TRUSTED_HOSTS.split(",") if host.strip()]

    @property
    def docs_enabled(self) -> bool:
        """Expose-t-on `/docs` et `/openapi.json` ? Masqués en production par défaut."""
        return self.ENABLE_DOCS if self.ENABLE_DOCS is not None else not self.is_production

    @property
    def serve_storage(self) -> bool:
        """L'API sert-elle elle-même les fichiers déposés ?

        Non en production : c'est le rôle du reverse proxy, qui sait le faire sans
        mobiliser un worker applicatif pour chaque image.
        """
        return self.SERVE_STORAGE if self.SERVE_STORAGE is not None else not self.is_production

    @model_validator(mode="after")
    def _reject_unsafe_production_settings(self) -> Settings:
        """Refuse de démarrer en production avec une configuration de développement.

        Mieux vaut un conteneur qui ne démarre pas — et le dit — qu'une API en
        ligne signant ses jetons avec la clé publiée dans `.env.example`.
        """
        if not self.is_production:
            return self

        problems: list[str] = []
        if self.JWT_SECRET_KEY == DEV_JWT_SECRET:
            problems.append(
                "JWT_SECRET_KEY porte encore la valeur d'exemple ; générez-en une avec "
                "`python -c \"import secrets; print(secrets.token_urlsafe(64))\"`"
            )
        elif len(self.JWT_SECRET_KEY) < MIN_JWT_SECRET_LENGTH:
            problems.append(
                f"JWT_SECRET_KEY doit faire au moins {MIN_JWT_SECRET_LENGTH} caractères"
            )
        if "*" in self.cors_origins_list:
            problems.append(
                'CORS_ORIGINS ne peut pas valoir "*" : listez les origines réellement autorisées'
            )
        if self.DATABASE_URL.startswith("sqlite"):
            problems.append("DATABASE_URL doit pointer sur PostgreSQL, pas sur SQLite")
        if self.DB_ECHO:
            problems.append("DB_ECHO=true déverse le SQL — et les données — dans les logs")
        cle_precedente = self.JWT_SECRET_KEY_PREVIOUS
        if cle_precedente and len(cle_precedente) < MIN_JWT_SECRET_LENGTH:
            problems.append(
                f"JWT_SECRET_KEY_PREVIOUS doit faire au moins {MIN_JWT_SECRET_LENGTH} caractères"
            )
        if len(self.STORAGE_URL_SIGNING_KEY) < MIN_JWT_SECRET_LENGTH:
            problems.append(
                "STORAGE_URL_SIGNING_KEY est obligatoire (au moins "
                f"{MIN_JWT_SECRET_LENGTH} caractères) : sans elle, les photos de "
                "pièces d'identité sont accessibles à quiconque possède leur URL"
            )

        if problems:
            raise ValueError(
                "Configuration de production invalide :\n"
                + "\n".join(f"  - {problem}" for problem in problems)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Singleton des settings, mis en cache pour ne pas reparser l'environnement à chaque appel."""
    return Settings()


settings = get_settings()
