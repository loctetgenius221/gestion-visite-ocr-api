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
from app.models.organization import Organization

ORGANISATION = "MIN-TEST"


@pytest.fixture
async def base_temporaire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Substitue `SessionLocal` du module par une base de fichier jetable.

    La base porte une organisation : tout rôle autre qu'`ADMIN` doit s'y
    rattacher, faute de quoi le compte créé serait refusé dès sa première requête.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fabrique = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with fabrique() as session:
        session.add(Organization(nom="Ministère de test", code=ORGANISATION, is_active=True))
        await session.commit()

    monkeypatch.setattr(cli, "SessionLocal", fabrique)
    yield
    await engine.dispose()


async def test_cree_un_compte_avec_un_mot_de_passe_fourni(base_temporaire: None) -> None:
    user, secret = await cli.create_user(
        "test001",
        nom="Compte de test",
        mot_de_passe="MotDePasseSolide2026",
        organisation=ORGANISATION,
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
    user, secret = await cli.create_user("test002", organisation=ORGANISATION)

    assert len(secret) >= 12
    assert verify_password(secret, user.mot_de_passe_hash)


async def test_le_nom_reprend_lidentifiant_par_defaut(base_temporaire: None) -> None:
    user, _ = await cli.create_user("test003", organisation=ORGANISATION)

    assert user.nom == "test003"


async def test_role_admin_explicite(base_temporaire: None) -> None:
    """Sert à créer le premier administrateur d'une installation de production."""
    user, _ = await cli.create_user("admin002", role=UserRole.ADMIN)

    assert user.role is UserRole.ADMIN


async def test_identifiant_deja_pris(base_temporaire: None) -> None:
    await cli.create_user("test004", organisation=ORGANISATION)

    with pytest.raises(ValueError, match="déjà attribué"):
        await cli.create_user("test004", organisation=ORGANISATION)


async def test_mot_de_passe_trop_court_refuse(base_temporaire: None) -> None:
    with pytest.raises(ValueError, match="6 caractères"):
        await cli.create_user("test005", mot_de_passe="court", organisation=ORGANISATION)


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
        arguments = [
            "test007",
            "--mot-de-passe",
            "MotDePasse2026!",
            "--organisation",
            ORGANISATION,
        ]
        await cli.main(arguments)

        code = await cli.main(arguments)

        assert code == 1
        assert "déjà attribué" in capsys.readouterr().err


class TestRattachement:
    """`ADMIN` est le seul rôle global : tous les autres exigent une organisation."""

    async def test_un_role_non_admin_sans_organisation_est_refuse(
        self, base_temporaire: None
    ) -> None:
        with pytest.raises(ValueError, match="exige une organisation"):
            await cli.create_user("test008")

    async def test_un_admin_ne_se_rattache_a_aucune_organisation(
        self, base_temporaire: None
    ) -> None:
        with pytest.raises(ValueError, match="rôle global"):
            await cli.create_user("test009", role=UserRole.ADMIN, organisation=ORGANISATION)

    async def test_une_organisation_inconnue_est_refusee(self, base_temporaire: None) -> None:
        with pytest.raises(ValueError, match="Aucune organisation"):
            await cli.create_user("test010", organisation="INEXISTANTE")

    async def test_le_compte_cree_porte_son_organisation(self, base_temporaire: None) -> None:
        user, _ = await cli.create_user("test011", organisation=ORGANISATION)

        assert user.organization_id is not None
