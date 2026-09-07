"""Clôture automatique des visites restées PRESENT, en ligne de commande.

Usage :

    python -m app.auto_close_visits --dry-run          # compte sans clôturer
    python -m app.auto_close_visits                    # applique, organisation par organisation
    python -m app.auto_close_visits --heure 18:00      # force l'heure pour cette exécution
    python -m app.auto_close_visits --organization-id <uuid>

Chaque organisation a **sa** configuration : `automatic_visit_closure_enabled` et
`automatic_visit_closure_time` sont des réglages d'exploitation, surchargeables
structure par structure (voir `app/schemas/setting.py`). Un ministère qui ferme à
17:00 et un port qui ferme à 20:00 ne peuvent pas partager une heure unique — la
commande parcourt donc les organisations actives et applique à chacune la sienne,
à défaut la valeur de plateforme.

À brancher sur un timer systemd — voir DEPLOYMENT.md. Exécution idempotente : une
seconde exécution le même jour, pour la même organisation, est un no-op tracé.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import uuid

from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal, dispose_engine
from app.core.logging import configure_logging, get_logger
from app.models.organization import Organization
from app.services.auto_closure_service import AutoClosureService
from app.services.setting_service import SettingService

logger = get_logger(__name__)

FORMAT_HEURE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _heure(valeur: str) -> str:
    """Valide `HH:MM` dès l'analyse des arguments.

    Le type de cet argument était `uuid.UUID` : toute valeur passée à `--heure`
    était rejetée par argparse avant d'atteindre le programme, et l'option n'a
    donc jamais pu servir.
    """
    if not FORMAT_HEURE.match(valeur):
        raise argparse.ArgumentTypeError(f"heure invalide « {valeur} » : attendu HH:MM (ex: 17:00)")
    return valeur


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.auto_close_visits",
        description=(
            "Clôture automatiquement les visites encore PRESENT à l'heure configurée, "
            "et crée une fiche de pièce oubliée pour chacune."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compte ce qui serait clôturé, sans rien modifier.",
    )
    parser.add_argument(
        "--heure",
        type=_heure,
        default=None,
        help=(
            "Force l'heure de clôture (HH:MM) pour toutes les organisations traitées. "
            "Par défaut, le réglage propre à chacune."
        ),
    )
    parser.add_argument(
        "--organization-id",
        type=uuid.UUID,
        default=None,
        help="Restreint la clôture à une organisation (UUID).",
    )
    return parser


async def _organisations_a_traiter(
    session, organization_id: uuid.UUID | None
) -> list[Organization]:
    """Organisations actives concernées par cette exécution.

    Les organisations désactivées sont écartées : leurs comptes n'ont plus accès
    à l'API, clôturer leurs visites au nom de personne n'aurait pas de sens.
    """
    stmt = select(Organization).where(Organization.is_active.is_(True))
    if organization_id is not None:
        stmt = stmt.where(Organization.id == organization_id)
    return list((await session.execute(stmt)).scalars().all())


async def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _parser().parse_args(argv)
    lignes: list[str] = []

    try:
        async with SessionLocal() as session:
            reglages = SettingService(session)
            organisations = await _organisations_a_traiter(session, args.organization_id)

            if not organisations:
                print("Aucune organisation active à traiter.")
                return 0

            service = AutoClosureService(session)
            for organisation in organisations:
                propres = await reglages.get_settings(organization_id=organisation.id)

                if not propres.automatic_visit_closure_enabled and not args.dry_run:
                    lignes.append(f"  {organisation.code:<20} désactivée dans ses réglages")
                    continue

                heure = args.heure or propres.automatic_visit_closure_time
                resultat = await service.run_automatic_closure(
                    closure_time_str=heure,
                    organization_id=organisation.id,
                    dry_run=args.dry_run,
                )
                visites = resultat.get(
                    "closed_visits_count", resultat.get("visits_to_close", 0)
                )
                lignes.append(
                    f"  {organisation.code:<20} {heure}  {resultat['status']:<12} "
                    f"visites={visites} fiches={resultat.get('cases_created_count', '-')}"
                )
    finally:
        await dispose_engine()

    print(f"Clôture automatique sur {settings.ENVIRONMENT}")
    print(f"  organisations : {len(organisations)}")
    print()
    for ligne in lignes:
        print(ligne)

    if args.dry_run:
        print()
        print("Simulation : rien n'a été modifié. Relancez sans --dry-run pour appliquer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
