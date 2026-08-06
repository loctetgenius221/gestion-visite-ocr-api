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
    """Crée l'engine async. `pool_pre_ping` évite les connexions mortes après un idle long."""
    kwargs: dict[str, object] = {"echo": settings.DB_ECHO, "future": True}
    if not settings.DATABASE_URL.startswith("sqlite"):
        kwargs["pool_pre_ping"] = True
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
