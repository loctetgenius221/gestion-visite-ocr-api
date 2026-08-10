"""Données de seed pour le développement (spec §9.4).

Usage : `python -m app.seeds`

Le script est idempotent : il ne réinsère rien s'il détecte des données existantes,
et peut donc être relancé sans risque après une migration.
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import SessionLocal, dispose_engine
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.referentiel import Agent, Purpose, Service
from app.models.user import User

logger = get_logger(__name__)

# Mot de passe de l'agent de démonstration : surchargeable par variable
# d'environnement, jamais destiné à un déploiement réel.
DEMO_AGENT_PASSWORD = "Sigv@2026"
MIN_SEED_PASSWORD_LENGTH = 12
# `or` plutôt qu'un défaut de `getenv` : une variable présente mais vide — cas
# courant dans un fichier `.env` recopié sans être rempli — ne doit pas créer de
# comptes sans mot de passe.
DEFAULT_AGENT_PASSWORD = os.getenv("SEED_AGENT_PASSWORD") or DEMO_AGENT_PASSWORD


def _check_seed_password() -> None:
    """Interdit de semer des comptes avec le mot de passe de démonstration en production."""
    if not settings.is_production:
        return
    if (
        DEFAULT_AGENT_PASSWORD == DEMO_AGENT_PASSWORD
        or len(DEFAULT_AGENT_PASSWORD) < MIN_SEED_PASSWORD_LENGTH
    ):
        raise RuntimeError(
            "SEED_AGENT_PASSWORD doit être défini et faire au moins "
            f"{MIN_SEED_PASSWORD_LENGTH} caractères pour semer une base de production."
        )

SERVICES: list[dict[str, str | None]] = [
    {"code": "DRH", "name": "Direction des Ressources Humaines", "floor": "2e étage"},
    {"code": "DAF", "name": "Direction Administrative et Financière", "floor": "1er étage"},
    {"code": "DSI", "name": "Direction des Systèmes d'Information", "floor": "3e étage"},
    {"code": "CAB", "name": "Cabinet du Ministre", "floor": "4e étage"},
    {"code": "SG", "name": "Secrétariat Général", "floor": "4e étage"},
]

# (code du service, nom, fonction, bureau)
AGENTS: list[tuple[str, str, str, str]] = [
    ("DRH", "Aminata Diallo", "Directrice des Ressources Humaines", "B201"),
    ("DRH", "Ousmane Ndiaye", "Chef du service Carrières", "B205"),
    ("DAF", "Fatou Sarr", "Directrice Administrative et Financière", "B105"),
    ("DAF", "Cheikh Fall", "Comptable principal", "B108"),
    ("DSI", "Mamadou Ba", "Directeur des Systèmes d'Information", "B302"),
    ("DSI", "Awa Gueye", "Administratrice réseau", "B304"),
    ("CAB", "Ibrahima Sow", "Chef de Cabinet", "B401"),
    ("SG", "Ndèye Faye", "Secrétaire Générale", "B405"),
]

PURPOSES: list[str] = [
    "Rendez-vous professionnel",
    "Dépôt de dossier",
    "Retrait de document",
    "Réunion de travail",
    "Entretien de recrutement",
    "Livraison / prestation",
    "Visite de courtoisie",
    "Autre",
]

USERS: list[dict[str, object]] = [
    {
        "nom": "Moussa Sagna",
        "identifiant": "agent001",
        "poste": "Poste principal",
        "role": UserRole.AGENT_CONTROLE,
    },
    {
        "nom": "Superviseur Accueil",
        "identifiant": "superviseur001",
        "poste": "Poste principal",
        "role": UserRole.SUPERVISEUR,
    },
    {
        # Compte du dashboard web. Seul rôle habilité sur les routes
        # d'administration : comptes, référentiels en écriture, audit, paramètres.
        "nom": "Administrateur SIGV",
        "identifiant": "admin001",
        "poste": "Administration",
        "role": UserRole.ADMIN,
    },
]


async def _seed_services(session: AsyncSession) -> dict[str, Service]:
    existing = {
        service.code: service
        for service in (await session.execute(select(Service))).scalars().all()
    }
    for data in SERVICES:
        code = str(data["code"])
        if code not in existing:
            service = Service(**data)
            session.add(service)
            existing[code] = service
    await session.flush()
    return existing


async def _seed_agents(session: AsyncSession, services: dict[str, Service]) -> None:
    known = {
        (agent.service_id, agent.name)
        for agent in (await session.execute(select(Agent))).scalars().all()
    }
    for service_code, name, role, office in AGENTS:
        service = services[service_code]
        if (service.id, name) in known:
            continue
        session.add(Agent(name=name, role=role, office=office, service_id=service.id))


async def _seed_purposes(session: AsyncSession) -> None:
    known = set((await session.execute(select(Purpose.libelle))).scalars().all())
    for libelle in PURPOSES:
        if libelle not in known:
            session.add(Purpose(libelle=libelle))


async def _seed_users(session: AsyncSession) -> None:
    known = set((await session.execute(select(User.identifiant))).scalars().all())
    for data in USERS:
        if data["identifiant"] in known:
            continue
        session.add(User(**data, mot_de_passe_hash=hash_password(DEFAULT_AGENT_PASSWORD)))


async def seed(session: AsyncSession) -> None:
    """Insère les référentiels et les comptes de démonstration manquants."""
    _check_seed_password()
    services = await _seed_services(session)
    await _seed_agents(session, services)
    await _seed_purposes(session)
    await _seed_users(session)
    await session.commit()

    counts = {
        "services": (await session.execute(select(func.count(Service.id)))).scalar_one(),
        "agents": (await session.execute(select(func.count(Agent.id)))).scalar_one(),
        "purposes": (await session.execute(select(func.count(Purpose.id)))).scalar_one(),
        "users": (await session.execute(select(func.count(User.id)))).scalar_one(),
    }
    logger.info("Seed terminé", extra=counts)


async def main() -> None:
    configure_logging()
    async with SessionLocal() as session:
        await seed(session)
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
