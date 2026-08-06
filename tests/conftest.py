"""Fixtures de test.

Base de test : **SQLite en mémoire** via `aiosqlite` (choix documenté en ADR-007).
Le schéma est créé depuis `Base.metadata` plutôt que par Alembic, pour garder les
tests rapides ; la conformité des migrations est vérifiée séparément par
`tests/integration/test_migrations.py`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

# Doit précéder tout import de `app.core.config` : les settings sont mis en cache.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key")
os.environ.setdefault("OCR_PRELOAD_MODEL", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.database import get_session  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.models.referentiel import Agent, Purpose, Service  # noqa: E402
from app.models.user import User  # noqa: E402

TEST_PASSWORD = "MotDePasse123!"


@pytest.fixture
async def engine():
    """Engine SQLite en mémoire ; `StaticPool` garde une connexion unique partagée."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session


@pytest.fixture
async def seeded(session: AsyncSession) -> dict[str, object]:
    """Jeu de données minimal : un agent de contrôle, un service, un agent, un motif."""
    user = User(
        nom="Agent Test",
        identifiant="agent001",
        mot_de_passe_hash=hash_password(TEST_PASSWORD),
        poste="Poste principal",
        role=UserRole.AGENT_CONTROLE,
        is_active=True,
    )
    service = Service(name="Direction des RH", code="DRH", floor="2e étage")
    purpose = Purpose(libelle="Rendez-vous professionnel")
    session.add_all([user, service, purpose])
    await session.flush()

    agent = Agent(
        name="Aminata Diallo", role="Directrice des RH", office="B201", service_id=service.id
    )
    other_service = Service(name="Direction Informatique", code="DSI", floor="3e étage")
    session.add_all([agent, other_service])
    await session.flush()

    other_agent = Agent(name="Mamadou Ba", role="DSI", office="B302", service_id=other_service.id)
    session.add(other_agent)
    await session.commit()

    return {
        "user": user,
        "service": service,
        "agent": agent,
        "purpose": purpose,
        "other_service": other_service,
        "other_agent": other_agent,
    }


@pytest.fixture
async def client(engine, session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Client HTTP branché sur l'app, avec la session de test injectée.

    On yield la *même* session que la fixture `seeded` pour que les données créées
    dans le test soient visibles par l'API sans commit intermédiaire.
    """
    app = create_app()

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as http_client:
        yield http_client

    app.dependency_overrides.clear()


@pytest.fixture
async def auth_headers(client: AsyncClient, seeded: dict[str, object]) -> dict[str, str]:
    """En-têtes d'authentification pour l'agent de contrôle de test."""
    response = await client.post(
        "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
