"""Données de seed pour le développement et la recette (spec §9.4).

Usage :

    python -m app.seeds                                   # services et agents fictifs
    python -m app.seeds --annuaire annuaire-mfptrsp.csv   # annuaire réel du ministère

L'organisation est le Ministère de la Fonction Publique, du Travail et de la
Réforme du Service public (`ORGANISATION_PILOTE`). Tous les comptes reçoivent le
même mot de passe, `SEED_AGENT_PASSWORD` : en production, créez-les plutôt un à
un avec `app.create_user`.

Le script est idempotent : il ne réinsère rien s'il détecte des données existantes,
et peut donc être relancé sans risque après une migration.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap import MOTIFS_COMMUNS, ORGANISATION_PILOTE
from app.core.config import settings
from app.core.database import SessionLocal, dispose_engine
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.import_annuaire import ImportError_, importer
from app.models.enums import UserRole
from app.models.organization import Organization
from app.models.referentiel import Agent, Purpose, Service
from app.models.user import User

logger = get_logger(__name__)

# Mot de passe de l'agent de démonstration : surchargeable par variable
# d'environnement, jamais destiné à un déploiement réel.
DEMO_AGENT_PASSWORD = "FpVisites@2026"
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

ORGANIZATION: dict[str, str] = dict(ORGANISATION_PILOTE)

# Jeu **fictif**, utilisé seulement sans `--annuaire` : les services portent les
# libellés réels du ministère, les agents des noms inventés. L'annuaire réel
# (données personnelles) ne vit jamais dans le dépôt.
SERVICES: list[dict[str, str | None]] = [
    {"code": "CM", "name": "Cabinet du Ministre", "floor": None},
    {"code": "SG", "name": "Secrétariat Général", "floor": None},
    {"code": "DGFP", "name": "Direction Générale de la Fonction Publique", "floor": None},
    {
        "code": "DAGE",
        "name": "Direction de l'Administration Générale et de l'Équipement",
        "floor": None,
    },
    {"code": "DSI", "name": "Direction des Systèmes d'Information", "floor": None},
]

# (code du service, nom, fonction, bureau)
AGENTS: list[tuple[str, str, str, str]] = [
    ("CM", "Ibrahima Sow", "Chef de Cabinet", "B401"),
    ("SG", "Ndèye Faye", "Secrétaire Générale", "B405"),
    ("DGFP", "Aminata Diallo", "Directrice Générale", "B201"),
    ("DGFP", "Ousmane Ndiaye", "Chef de division", "B205"),
    ("DAGE", "Fatou Sarr", "Directrice", "B105"),
    ("DAGE", "Cheikh Fall", "Comptable principal", "B108"),
    ("DSI", "Mamadou Ba", "Directeur des Systèmes d'Information", "B302"),
    ("DSI", "Awa Gueye", "Administratrice réseau", "B304"),
]

PURPOSES: list[str] = MOTIFS_COMMUNS

USERS: list[dict[str, object]] = [
    {
        # Compte partagé du poste de contrôle de l'entrée : il désigne le poste,
        # pas une personne.
        "nom": "Poste police",
        "identifiant": "agent001",
        "poste": "Poste de police — entrée",
        "role": UserRole.AGENT_CONTROLE,
    },
    {
        "nom": "Superviseur Accueil",
        "identifiant": "superviseur001",
        "poste": "Poste principal",
        "role": UserRole.SUPERVISEUR,
    },
    {
        # Administrateur de la structure : gère ses comptes, ses référentiels,
        # sa charte graphique et ses réglages d'exploitation.
        "nom": "Administratrice du Ministère",
        "identifiant": "orgadmin001",
        "poste": "Direction",
        "role": UserRole.ORG_ADMIN,
    },
    {
        # Compte du dashboard web. Seul rôle habilité sur les routes
        # d'administration : comptes, référentiels en écriture, audit, paramètres.
        "nom": "Administrateur Fp-visites",
        "identifiant": "admin",
        "poste": "Administration",
        "role": UserRole.ADMIN,
    },
]


async def _seed_organization(session: AsyncSession) -> Organization:
    """Organisation d'accueil du jeu de démonstration.

    Tout le reste s'y rattache : services, agents et comptes appartiennent à une
    structure, et un jeu de seed hors organisation ne serait visible d'aucun
    poste de contrôle.
    """
    stmt = select(Organization).where(Organization.code == ORGANIZATION["code"])
    existante = (await session.execute(stmt)).scalar_one_or_none()
    if existante is not None:
        return existante
    organization = Organization(**ORGANIZATION, is_active=True)
    session.add(organization)
    await session.flush()
    return organization


async def _seed_services(
    session: AsyncSession, organization: Organization
) -> dict[str, Service]:
    existing = {
        service.code: service
        for service in (
            await session.execute(
                select(Service).where(Service.organization_id == organization.id)
            )
        )
        .scalars()
        .all()
    }
    for data in SERVICES:
        code = str(data["code"])
        if code not in existing:
            service = Service(**data, organization_id=organization.id)
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
    """Motifs **communs** (`organization_id` nul) : proposés à toutes les structures.

    Ce sont des libellés génériques — « Dépôt de dossier », « Réunion de travail » —
    qu'aucune organisation n'a de raison de redéfinir. Une structure reste libre
    d'ajouter les siens à côté.
    """
    known = set(
        (
            await session.execute(
                select(Purpose.libelle).where(Purpose.organization_id.is_(None))
            )
        )
        .scalars()
        .all()
    )
    for libelle in PURPOSES:
        if libelle not in known:
            session.add(Purpose(libelle=libelle, organization_id=None))


async def _seed_users(session: AsyncSession, organization: Organization) -> None:
    """Comptes de démonstration. Seul `ADMIN` reste hors organisation."""
    known = set((await session.execute(select(User.identifiant))).scalars().all())
    for data in USERS:
        if data["identifiant"] in known:
            continue
        rattachement = None if data["role"] is UserRole.ADMIN else organization.id
        session.add(
            User(
                **data,
                organization_id=rattachement,
                mot_de_passe_hash=hash_password(DEFAULT_AGENT_PASSWORD),
            )
        )


async def seed(session: AsyncSession, *, avec_demo: bool = True) -> None:
    """Insère l'organisation, les motifs, les comptes et — sauf annuaire réel —
    les services et agents fictifs manquants."""
    _check_seed_password()
    organization = await _seed_organization(session)
    if avec_demo:
        services = await _seed_services(session, organization)
        await _seed_agents(session, services)
    await _seed_purposes(session)
    await _seed_users(session, organization)
    await session.commit()

    counts = {
        "services": (await session.execute(select(func.count(Service.id)))).scalar_one(),
        "agents": (await session.execute(select(func.count(Agent.id)))).scalar_one(),
        "purposes": (await session.execute(select(func.count(Purpose.id)))).scalar_one(),
        "users": (await session.execute(select(func.count(User.id)))).scalar_one(),
    }
    logger.info("Seed terminé", extra=counts)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.seeds", description=__doc__)
    parser.add_argument(
        "--annuaire",
        type=Path,
        default=None,
        help=(
            "CSV de l'annuaire réel (voir `app.import_annuaire`). Fourni, il remplace "
            "les services et agents fictifs."
        ),
    )
    return parser


async def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _parser().parse_args(argv)
    try:
        async with SessionLocal() as session:
            await seed(session, avec_demo=args.annuaire is None)
        if args.annuaire is not None:
            rapport = await importer(args.annuaire, organisation=ORGANIZATION["code"])
            print(
                f"Annuaire importé : {len(rapport.services_crees)} service(s) et "
                f"{rapport.agents_crees} agent(s) créés, "
                f"{rapport.agents_existants} déjà présents."
            )
    except ImportError_ as exc:
        print(f"Échec de l'import de l'annuaire : {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
