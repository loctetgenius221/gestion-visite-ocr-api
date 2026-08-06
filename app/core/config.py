from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration centralisée de l'application, lue depuis l'environnement (.env en dev)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Général ---
    PROJECT_NAME: str = "SIGV Backend"
    ENVIRONMENT: str = "development"
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # --- Base de données ---
    DATABASE_URL: str = "postgresql+asyncpg://sigv:sigv@localhost:5432/sigv"
    DB_ECHO: bool = False

    # --- Redis / Celery ---
    # Non utilisés pour le moment : voir ARCHITECTURE_DECISIONS.md (ADR-002).
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"

    # --- Sécurité / JWT ---
    JWT_SECRET_KEY: str = "changeme-dev-secret-key"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # --- CORS ---
    # Origines autorisées, séparées par des virgules (ex: "http://localhost:3000,https://app.example.com")
    CORS_ORIGINS: str = "http://localhost:3000"

    # --- Stockage fichiers ---
    STORAGE_DIR: str = "storage/uploads"
    STORAGE_PUBLIC_BASE_URL: str = "/storage/uploads"
    MAX_UPLOAD_SIZE_MB: int = 10

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

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def max_upload_size_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """Singleton des settings, mis en cache pour ne pas reparser l'environnement à chaque appel."""
    return Settings()


settings = get_settings()
