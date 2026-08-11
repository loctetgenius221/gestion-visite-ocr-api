"""Création d'un compte en ligne de commande.

Usage :

    python -m app.create_user test001
    python -m app.create_user admin002 --role ADMIN --nom "Awa Ndiaye"
    python -m app.create_user agent007 --mot-de-passe "MotDePasseSolide2026"

Complémentaire de `app.seeds`, qui pose un jeu de démonstration complet : ce
module ne crée **qu'un compte**, sans référentiels. C'est ce qu'il faut pour
ouvrir un accès de test, ou pour créer le tout premier administrateur d'une
installation de production — où les comptes de démonstration n'ont rien à faire.

Sans `--mot-de-passe`, un mot de passe fort est généré et affiché **une seule
fois** : seul son hash bcrypt part en base, il sera irrécupérable ensuite.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import SessionLocal, dispose_engine
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user import MIN_PASSWORD_LENGTH
from app.services.user_admin_service import generate_password

logger = get_logger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.create_user",
        description="Crée un compte utilisateur dans la base configurée par DATABASE_URL.",
    )
    parser.add_argument("identifiant", help="Identifiant de connexion, unique.")
    parser.add_argument(
        "--nom",
        default=None,
        help="Nom affiché. Par défaut, l'identifiant est repris tel quel.",
    )
    parser.add_argument(
        "--role",
        default=UserRole.AGENT_CONTROLE.value,
        choices=[role.value for role in UserRole],
        help="Rôle du compte. ADMIN ouvre les routes du dashboard web.",
    )
    parser.add_argument("--poste", default=None, help="Poste de rattachement (facultatif).")
    parser.add_argument(
        "--mot-de-passe",
        dest="mot_de_passe",
        default=None,
        help=(
            f"Mot de passe en clair, {MIN_PASSWORD_LENGTH} caractères minimum. "
            "Omis, un mot de passe fort est généré et affiché."
        ),
    )
    return parser


async def create_user(
    identifiant: str,
    *,
    nom: str | None = None,
    role: UserRole = UserRole.AGENT_CONTROLE,
    poste: str | None = None,
    mot_de_passe: str | None = None,
) -> tuple[User, str]:
    """Crée le compte et retourne `(compte, mot de passe en clair)`.

    Lève `ValueError` si l'identifiant est déjà pris ou le mot de passe trop court.
    """
    if mot_de_passe is not None and len(mot_de_passe) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Le mot de passe doit faire au moins {MIN_PASSWORD_LENGTH} caractères."
        )

    secret = mot_de_passe or generate_password()

    async with SessionLocal() as session:
        repository = UserRepository(session)
        if await repository.get_by_identifiant(identifiant) is not None:
            raise ValueError(f"L'identifiant « {identifiant} » est déjà attribué.")

        user = User(
            nom=nom or identifiant,
            identifiant=identifiant,
            poste=poste,
            role=role,
            mot_de_passe_hash=hash_password(secret),
            is_active=True,
        )
        try:
            await repository.add(user)
            await session.commit()
        except IntegrityError as exc:
            # Course entre deux exécutions simultanées : la contrainte d'unicité
            # tranche, on la traduit dans le même message que le contrôle ci-dessus.
            await session.rollback()
            raise ValueError(f"L'identifiant « {identifiant} » est déjà attribué.") from exc

        return user, secret


async def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _parser().parse_args(argv)

    try:
        user, secret = await create_user(
            args.identifiant,
            nom=args.nom,
            role=UserRole(args.role),
            poste=args.poste,
            mot_de_passe=args.mot_de_passe,
        )
    except ValueError as exc:
        print(f"Échec : {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()

    # Écrit sur la sortie standard et non dans les logs : `mot_de_passe` fait
    # partie des clés masquées par le formateur, et un secret n'a de toute façon
    # rien à faire dans un journal conservé.
    print(f"Compte créé sur {settings.ENVIRONMENT}")
    print(f"  identifiant   : {user.identifiant}")
    print(f"  rôle          : {user.role.value}")
    print(f"  mot de passe  : {secret}")
    if args.mot_de_passe is None:
        print()
        print("Ce mot de passe n'est affiché qu'une fois : notez-le maintenant.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
