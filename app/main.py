from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.database import dispose_engine
from app.core.handlers import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware
from app.routers import auth, dashboard, ocr, referentiels, uploads, visits
from app.schemas.common import ErrorResponse
from app.services.ocr_engine import get_ocr_engine

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Démarrage/arrêt de l'application.

    Le modèle PaddleOCR est chargé ici, une fois pour toutes : le charger à la
    première requête ferait exploser le budget de 3 s de la spec §4.3.
    """
    configure_logging()
    logger.info("Démarrage de l'API", extra={"environment": settings.ENVIRONMENT})

    if settings.OCR_PRELOAD_MODEL:
        try:
            await anyio.to_thread.run_sync(get_ocr_engine().load)
        except Exception:
            # Un modèle indisponible ne doit pas empêcher l'API de démarrer : seules
            # les routes OCR en pâtiront, et elles renverront une erreur explicite.
            logger.exception("Préchargement du modèle OCR impossible")

    yield

    await dispose_engine()
    logger.info("Arrêt de l'API")


def create_app() -> FastAPI:
    application = FastAPI(
        title=settings.PROJECT_NAME,
        description=(
            "API du Système Intelligent de Gestion des Visites (SIGV) — "
            "contrôle d'accès, scan MRZ et suivi des visites."
        ),
        version="0.1.0",
        lifespan=lifespan,
        responses={
            400: {"model": ErrorResponse, "description": "Requête malformée"},
            401: {"model": ErrorResponse, "description": "Non authentifié"},
            404: {"model": ErrorResponse, "description": "Ressource introuvable"},
            409: {"model": ErrorResponse, "description": "Conflit"},
            422: {"model": ErrorResponse, "description": "Échec métier"},
            500: {"model": ErrorResponse, "description": "Erreur serveur"},
        },
    )

    application.add_middleware(RequestContextMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    register_exception_handlers(application)

    prefix = settings.API_V1_PREFIX
    application.include_router(auth.router, prefix=prefix)
    application.include_router(auth.me_router, prefix=prefix)
    application.include_router(ocr.router, prefix=prefix)
    application.include_router(visits.router, prefix=prefix)
    application.include_router(referentiels.router, prefix=prefix)
    application.include_router(dashboard.router, prefix=prefix)
    application.include_router(uploads.router, prefix=prefix)

    _mount_storage(application)

    @application.get("/health", tags=["Health"], summary="Sonde de disponibilité")
    async def health() -> dict[str, str]:
        return {"status": "ok", "environment": settings.ENVIRONMENT}

    return application


def _mount_storage(application: FastAPI) -> None:
    """Sert les fichiers uploadés en développement.

    Sans ce montage, les `mrz_image_url` / `signature_url` renvoyés par l'API
    pointeraient vers une 404. En production, ces fichiers doivent être servis par
    le reverse proxy ou un stockage S3 — d'où le montage réservé au hors-production.
    """
    if settings.is_production:
        logger.info("Stockage local non exposé en production (à servir par le proxy).")
        return

    storage_dir = Path(settings.STORAGE_DIR)
    storage_dir.mkdir(parents=True, exist_ok=True)
    application.mount(
        settings.STORAGE_PUBLIC_BASE_URL,
        StaticFiles(directory=storage_dir),
        name="storage",
    )


app = create_app()
