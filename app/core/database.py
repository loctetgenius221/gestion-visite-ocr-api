from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings


def _build_engine() -> AsyncEngine:
    """Crée l'engine async.

    Le pool est dimensionné explicitement : les défauts de SQLAlchemy (5 + 10
    connexions par worker), multipliés par les workers et les nœuds, épuisent
    vite `max_connections`. `pool_pre_ping` écarte les connexions mortes après
    une longue inactivité.
    """
    kwargs: dict[str, object] = {"echo": settings.DB_ECHO, "future": True}
    if settings.DATABASE_URL.startswith("sqlite"):
        return create_async_engine(settings.DATABASE_URL, **kwargs)

    kwargs.update(
        pool_pre_ping=True,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
    )
    connect_args: dict[str, object] = {}
    if settings.DB_USE_PGBOUNCER:
        # En mode transaction, deux requêtes successives peuvent partir sur deux
        # connexions serveur différentes : une requête préparée sur l'une
        # n'existe pas sur l'autre.
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_cache_size"] = 0
    else:
        server_settings = {"application_name": "fp-visites-api"}
        if settings.DB_STATEMENT_TIMEOUT_MS > 0:
            server_settings["statement_timeout"] = str(settings.DB_STATEMENT_TIMEOUT_MS)
        connect_args["server_settings"] = server_settings
    kwargs["connect_args"] = connect_args
    return create_async_engine(settings.DATABASE_URL, **kwargs)


engine: AsyncEngine = _build_engine()

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dépendance FastAPI : ouvre une session par requête et la referme systématiquement.

    Le commit reste à la charge de la couche service : un router ne doit jamais
    committer implicitement une transaction partiellement construite.
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    await engine.dispose()
