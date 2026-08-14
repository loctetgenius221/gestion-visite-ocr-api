"""Purge des photos de pièces d'identité, en ligne de commande.

Usage :

    python -m app.purge_documents --dry-run     # compte sans rien supprimer
    python -m app.purge_documents               # applique le paramètre système
    python -m app.purge_documents --jours 90    # force la durée, pour cette fois

La durée de conservation est le paramètre système `document_images_retention_days`
(365 jours par défaut, `0` désactive la purge), modifiable par un administrateur
depuis le dashboard sans redéploiement.

Seules les **images** sont supprimées : visiteurs, visites et journal d'audit
restent intacts. Le registre doit pouvoir dire qui est venu et quand, des années
après que la photo de la pièce a été effacée (ADR-018).

À brancher sur un timer systemd — voir DEPLOYMENT.md. Volontairement pas de tâche
de fond interne à l'application : elle tournerait une fois par worker uvicorn, et
son échec ne serait visible de personne.
"""

from __future__ import annotations

import argparse
import asyncio

from app.core.config import settings
from app.core.database import SessionLocal, dispose_engine
from app.core.logging import configure_logging, get_logger
from app.services.document_purge_service import DocumentPurgeService, PurgeReport
from app.services.storage_service import get_storage_service

logger = get_logger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.purge_documents",
        description=(
            "Supprime les photos de pièces d'identité dont la durée de conservation "
            "est écoulée, dans la base configurée par DATABASE_URL."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compte ce qui serait supprimé, sans rien effacer.",
    )
    parser.add_argument(
        "--jours",
        type=int,
        default=None,
        help=(
            "Force la durée de conservation pour cette exécution, en jours. "
            "Par défaut, le paramètre système `document_images_retention_days`."
        ),
    )
    return parser


async def purge(*, retention_days: int | None = None, dry_run: bool = False) -> PurgeReport:
    async with SessionLocal() as session:
        service = DocumentPurgeService(session, get_storage_service())
        return await service.purge(retention_days=retention_days, dry_run=dry_run)


async def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _parser().parse_args(argv)

    if args.jours is not None and args.jours < 0:
        print("Échec : --jours ne peut pas être négatif.")
        return 1

    try:
        rapport = await purge(retention_days=args.jours, dry_run=args.dry_run)
    finally:
        await dispose_engine()

    print(f"Purge des images de pièces d'identité sur {settings.ENVIRONMENT}")
    if rapport.desactivee:
        print("  conservation  : illimitée (document_images_retention_days = 0)")
        print("  Aucune image n'a été examinée.")
        return 0

    print(f"  conservation  : {rapport.retention_days} jours")
    print(f"  seuil         : dernière visite avant {rapport.cutoff:%Y-%m-%d %H:%M} UTC")
    print(f"  visiteurs     : {rapport.visiteurs}")
    print(f"  images        : {rapport.images}")
    if rapport.dry_run:
        print()
        print("Simulation : rien n'a été supprimé. Relancez sans --dry-run pour appliquer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
