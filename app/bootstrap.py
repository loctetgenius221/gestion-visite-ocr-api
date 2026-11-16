"""Démarrage d'une base de production neuve : organisation et motifs communs.

Usage :

    python -m app.bootstrap                     # structure pilote (MFPTRSP)
    python -m app.bootstrap --code MIN-INTERIEUR --nom "Ministère de l'Intérieur"

À l'inverse de `app.seeds`, rien de fictif : ni service ni agent de
démonstration, ni compte au mot de passe connu. La commande pose ce qu'aucun
écran ne permet de créer avant qu'un administrateur existe, et ce qui n'a pas
d'import en masse :

- l'organisation, à laquelle les comptes non-ADMIN doivent être rattachés ;
- les motifs **communs** à toutes les structures, s'il n'y en a encore aucun.

Elle est idempotente : une organisation déjà présente (même code) est reprise,
des motifs communs déjà présents ne sont pas complétés. La suite — comptes,
services, agents — passe par `app.create_user` et l'import `.xlsx` du dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import SessionLocal, dispose_engine
from app.core.logging import configure_logging
from app.models.organization import Organization
from app.models.referentiel import Purpose
from app.schemas.organization import OrganizationCreate

# Première structure cliente, posée par défaut quand `--code` est omis.
ORGANISATION_PILOTE: dict[str, str] = {
    "code": "MFPTRSP",
    "nom": (
        "Ministère de la Fonction Publique, du Travail et de la Réforme du Service public"
    ),
    "localisation": "Dakar",
}

# Libellés génériques, qu'aucune structure n'a de raison de redéfinir. Une
# structure ajoute les siens depuis le dashboard.
MOTIFS_COMMUNS: list[str] = [
    "Rendez-vous professionnel",
    "Dépôt de dossier",
    "Retrait de document",
    "Réunion de travail",
    "Entretien de recrutement",
    "Livraison / prestation",
    "Visite de courtoisie",
    "Autre",
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.bootstrap",
        description="Pose l'organisation et les motifs communs d'une base neuve.",
    )
    parser.add_argument(
        "--code",
        default=None,
        help=f"Code unique. Omis : la structure pilote ({ORGANISATION_PILOTE['code']}).",
    )
    parser.add_argument("--nom", default=None, help="Nom officiel, obligatoire avec --code.")
    parser.add_argument("--localisation", default=None, help="Adresse (facultatif).")
    return parser


async def bootstrap(
    *, code: str, nom: str, localisation: str | None = None
) -> tuple[Organization, bool, int]:
    """Retourne `(organisation, créée ?, nombre de motifs communs créés)`.

    Lève `ValueError` si le nom ou le code ne respecte pas les règles de
    l'API — les mêmes que `POST /organizations`.
    """
    try:
        valide = OrganizationCreate(nom=nom, code=code.strip().upper(), localisation=localisation)
    except ValidationError as exc:
        raise ValueError(
            "; ".join(f"{'.'.join(map(str, e['loc']))} : {e['msg']}" for e in exc.errors())
        ) from exc

    async with SessionLocal() as session:
        stmt = select(Organization).where(func.lower(Organization.code) == valide.code.lower())
        organisation = (await session.execute(stmt)).scalar_one_or_none()
        creee = organisation is None
        if creee:
            organisation = Organization(
                nom=valide.nom,
                code=valide.code,
                localisation=valide.localisation,
                is_active=True,
            )
            session.add(organisation)

        communs = (
            await session.execute(
                select(func.count(Purpose.id)).where(Purpose.organization_id.is_(None))
            )
        ).scalar_one()
        motifs_crees = 0
        if communs == 0:
            for libelle in MOTIFS_COMMUNS:
                session.add(Purpose(libelle=libelle, organization_id=None))
            motifs_crees = len(MOTIFS_COMMUNS)

        await session.commit()
        assert organisation is not None
        return organisation, creee, motifs_crees


async def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _parser().parse_args(argv)
    if args.code is None:
        code, nom = ORGANISATION_PILOTE["code"], ORGANISATION_PILOTE["nom"]
        localisation = args.localisation or ORGANISATION_PILOTE["localisation"]
    elif args.nom is None:
        print("Échec : --nom est obligatoire avec --code.", file=sys.stderr)
        return 2
    else:
        code, nom, localisation = args.code, args.nom, args.localisation
    try:
        organisation, creee, motifs = await bootstrap(
            code=code, nom=nom, localisation=localisation
        )
    except ValueError as exc:
        print(f"Échec : {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()

    etat = "créée" if creee else "déjà présente, reprise"
    print(f"Base {settings.ENVIRONMENT}")
    print(f"  organisation    : {organisation.code} — {organisation.nom} ({etat})")
    print(f"  motifs communs  : {motifs} créé(s)")
    print()
    print("Suite :")
    print("  python -m app.create_user <identifiant> --role ADMIN")
    print(
        f"  python -m app.create_user <identifiant> --role AGENT_CONTROLE "
        f"--organisation {organisation.code}"
    )
    print(
        f"  python -m app.import_annuaire annuaire.csv --organisation {organisation.code} "
        "--dry-run"
    )
    print("  (ou, depuis le dashboard : import des services, puis des agents, en .xlsx)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
