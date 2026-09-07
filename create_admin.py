"""Crée un administrateur global dans la base configurée par DATABASE_URL.

Usage:
    uv run python create_admin.py
    uv run python create_admin.py --identifiant admin001 --mot-de-passe "Passer@123"
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal, dispose_engine
from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user import MIN_PASSWORD_LENGTH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crée un compte ADMIN global.")
    parser.add_argument("--identifiant", default="admin001")
    parser.add_argument("--nom", default="Administrateur plateforme")
    parser.add_argument("--mot-de-passe", default=None)
    return parser.parse_args()


async def create_admin(
    identifiant: str, nom: str, mot_de_passe: str
) -> User:
    if len(mot_de_passe) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Le mot de passe doit contenir au moins {MIN_PASSWORD_LENGTH} caractères."
        )

    async with SessionLocal() as session:
        repository = UserRepository(session)
        if await repository.get_by_identifiant(identifiant) is not None:
            raise ValueError(f"L'identifiant « {identifiant} » existe déjà.")

        admin = User(
            nom=nom,
            identifiant=identifiant,
            mot_de_passe_hash=hash_password(mot_de_passe),
            role=UserRole.ADMIN,
            organization_id=None,
            is_active=True,
            failed_login_attempts=0,
            locked_until=None,
            last_login_at=None,
        )
        try:
            await repository.add(admin)
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError(f"L'identifiant « {identifiant} » existe déjà.") from exc

        return admin


async def main() -> int:
    args = parse_args()
    password = args.mot_de_passe or getpass.getpass("Mot de passe ADMIN : ")

    try:
        admin = await create_admin(args.identifiant, args.nom, password)
    except ValueError as exc:
        print(f"Échec : {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()

    print("Administrateur global créé avec succès.")
    print(f"  identifiant : {admin.identifiant}")
    print(f"  rôle        : {admin.role.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
