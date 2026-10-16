"""Nettoyage des sessions et des tokens révoqués, en ligne de commande.

Usage :

    python -m app.purge_sessions --dry-run     # compte sans rien supprimer
    python -m app.purge_sessions               # supprime

Chaque connexion crée une ligne `user_sessions`, chaque déconnexion ou
révocation une ligne `revoked_tokens`. Sans purge, ces deux tables grossissent
indéfiniment avec le parc de tablettes.

Ce qui part :

- les tokens révoqués **expirés** : un JWT expiré est refusé de toute façon, sa
  présence dans la liste de révocation ne protège plus rien ;
- les sessions expirées depuis plus de `--garder-jours` jours, et les sessions
  révoquées depuis plus longtemps encore — le temps qu'un administrateur puisse
  constater qu'une tablette a bien été coupée.

À brancher sur un timer systemd, sur **un seul** nœud — voir DEPLOYMENT.md.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from app.core.config import settings
from app.core.database import SessionLocal, dispose_engine
from app.core.logging import configure_logging, get_logger
from app.models.refresh_token import RevokedToken
from app.models.user_session import UserSession
from app.repositories.user_repository import RevokedTokenRepository, UserSessionRepository

logger = get_logger(__name__)

GARDER_JOURS_PAR_DEFAUT = 30


@dataclass(frozen=True, slots=True)
class RapportPurgeSessions:
    tokens_revoques: int
    sessions: int
    dry_run: bool


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.purge_sessions",
        description="Supprime les sessions mortes et les tokens révoqués expirés.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Compte ce qui serait supprimé, sans rien effacer."
    )
    parser.add_argument(
        "--garder-jours",
        type=int,
        default=GARDER_JOURS_PAR_DEFAUT,
        help=(
            "Durée pendant laquelle une session expirée ou révoquée reste visible "
            f"dans le dashboard (défaut : {GARDER_JOURS_PAR_DEFAUT} jours)."
        ),
    )
    return parser


async def purge(*, garder_jours: int, dry_run: bool = False) -> RapportPurgeSessions:
    maintenant = datetime.now(UTC)
    seuil = maintenant - timedelta(days=garder_jours)

    async with SessionLocal() as session:
        if dry_run:
            tokens = (
                await session.execute(
                    select(func.count(RevokedToken.id)).where(RevokedToken.expires_at < maintenant)
                )
            ).scalar_one()
            sessions = (
                await session.execute(
                    select(func.count(UserSession.id)).where(
                        or_(UserSession.expires_at < seuil, UserSession.revoked_at < seuil)
                    )
                )
            ).scalar_one()
            return RapportPurgeSessions(tokens, sessions, dry_run=True)

        tokens = await RevokedTokenRepository(session).purge_expired()
        sessions = await UserSessionRepository(session).purge(
            expired_before=seuil, revoked_before=seuil
        )
        await session.commit()

    logger.info(
        "Purge des sessions", extra={"tokens_revoques": tokens, "sessions": sessions}
    )
    return RapportPurgeSessions(tokens, sessions, dry_run=False)


async def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _parser().parse_args(argv)
    if args.garder_jours < 0:
        print("Échec : --garder-jours ne peut pas être négatif.")
        return 1

    try:
        rapport = await purge(garder_jours=args.garder_jours, dry_run=args.dry_run)
    finally:
        await dispose_engine()

    print(f"Purge des sessions sur {settings.ENVIRONMENT}")
    print(f"  tokens révoqués expirés : {rapport.tokens_revoques}")
    print(f"  sessions mortes         : {rapport.sessions}")
    if rapport.dry_run:
        print()
        print("Simulation : rien n'a été supprimé. Relancez sans --dry-run pour appliquer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
