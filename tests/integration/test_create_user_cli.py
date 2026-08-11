"""Création de compte en ligne de commande (`python -m app.create_user`).

Le module ouvre sa propre session via `SessionLocal`, sans passer par l'injection
de dépendances de FastAPI : les tests le branchent donc sur une base SQLite en
fichier temporaire plutôt que sur la fixture `session` habituelle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import create_user as cli
from app.core.security import verify_password
from app.models import Base
from app.models.enums import UserRole


@pytest.fixture
async def base_temporaire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Substitue `SessionLocal` du module par une base de fichier jetable."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(
        cli, "SessionLocal", async_sessionmaker(bind=engine, expire_on_commit=False)
    )
    yield
    await engine.dispose()


async def test_cree_un_compte_avec_un_mot_de_passe_fourni(base_temporaire: None) -> None:
    user, secret = await cli.create_user(
        "test001", nom="Compte de test", mot_de_passe="MotDePasseSolide2026"
    )

    assert user.identifiant == "test001"
    assert user.nom == "Compte de test"
    assert user.role is UserRole.AGENT_CONTROLE
    assert user.is_active is True
    assert secret == "MotDePasseSolide2026"
    # Seul le hash part en base, et il valide bien le mot de passe d'origine.
    assert user.mot_de_passe_hash != secret
    assert verify_password(secret, user.mot_de_passe_hash)


async def test_le_mot_de_passe_est_genere_quand_il_est_omis(base_temporaire: None) -> None:
    user, secret = await cli.create_user("test002")

    assert len(secret) >= 12
    assert verify_password(secret, user.mot_de_passe_hash)


async def test_le_nom_reprend_lidentifiant_par_defaut(base_temporaire: None) -> None:
    user, _ = await cli.create_user("test003")

    assert user.nom == "test003"


async def test_role_admin_explicite(base_temporaire: None) -> None:
    """Sert à créer le premier administrateur d'une installation de production."""
    user, _ = await cli.create_user("admin002", role=UserRole.ADMIN)

    assert user.role is UserRole.ADMIN


async def test_identifiant_deja_pris(base_temporaire: None) -> None:
    await cli.create_user("test004")

    with pytest.raises(ValueError, match="déjà attribué"):
        await cli.create_user("test004")


async def test_mot_de_passe_trop_court_refuse(base_temporaire: None) -> None:
    with pytest.raises(ValueError, match="12 caractères"):
        await cli.create_user("test005", mot_de_passe="court")


class TestLigneDeCommande:
    async def test_sortie_et_code_retour_en_cas_de_succes(
        self, base_temporaire: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await cli.main(["test006", "--role", "ADMIN", "--mot-de-passe", "MotDePasse2026!"])

        assert code == 0
        sortie = capsys.readouterr().out
        assert "test006" in sortie
        assert "ADMIN" in sortie
        # Le mot de passe est affiché sur stdout, jamais journalisé.
        assert "MotDePasse2026!" in sortie

    async def test_code_retour_1_sur_doublon(
        self, base_temporaire: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        await cli.main(["test007", "--mot-de-passe", "MotDePasse2026!"])

        code = await cli.main(["test007", "--mot-de-passe", "MotDePasse2026!"])

        assert code == 1
        assert "déjà attribué" in capsys.readouterr().err
