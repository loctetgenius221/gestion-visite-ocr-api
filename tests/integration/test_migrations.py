"""Vérifie que les migrations Alembic décrivent bien le schéma des modèles.

Sans ce test, un modèle modifié sans migration correspondante ne serait détecté
qu'au déploiement (spec §7 : pas de `create_all()` en production).
"""

from __future__ import annotations

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from app.models import Base

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_une_seule_tete_de_migration():
    """Deux têtes signaleraient des branches de migration divergentes à fusionner."""
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    assert len(script.get_heads()) == 1


def test_les_migrations_reproduisent_le_schema_des_modeles(tmp_path: Path):
    """Applique les migrations sur une base vierge et compare au metadata des modèles."""
    from alembic import command

    db_path = tmp_path / "migrations.db"
    # `env.py` monte un engine async : l'upgrade passe par aiosqlite, la relecture
    # de comparaison par le driver sqlite synchrone, sur le même fichier.
    command.upgrade(alembic_config(f"sqlite+aiosqlite:///{db_path}"), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_type": True, "target_metadata": Base.metadata}
            )
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    # `alembic_version` est la table de suivi d'Alembic : absente des modèles, normal.
    ecarts = [
        entry
        for entry in diff
        if not (isinstance(entry, tuple) and "alembic_version" in str(entry))
    ]
    assert ecarts == [], f"Migrations désynchronisées des modèles : {ecarts}"


def test_downgrade_complet_ne_laisse_aucune_table(tmp_path: Path):
    from sqlalchemy import inspect

    from alembic import command

    db_path = tmp_path / "downgrade.db"
    config = alembic_config(f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()

    assert tables == set()


def test_la_fiche_visiteur_commune_est_separee_par_organisation(tmp_path: Path):
    """e9c4b7a2f310 (SEC-001) : une copie par organisation, sans les photos d'autrui."""
    import uuid

    from sqlalchemy import text

    from alembic import command

    db_path = tmp_path / "separation.db"
    config = alembic_config(f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(config, "d8f1b3c5e7a9")

    port, ministere = uuid.uuid4(), uuid.uuid4()
    visiteur, agent = uuid.uuid4().hex, uuid.uuid4().hex
    photo_du_port = f"/storage/uploads/org/{port}/documents/recto.jpg"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connexion:
            for org, code in ((port, "PORT"), (ministere, "MIN")):
                connexion.execute(
                    text("INSERT INTO organizations (id, nom, code) VALUES (:id, :code, :code)"),
                    {"id": org.hex, "code": code},
                )
                connexion.execute(
                    text(
                        "INSERT INTO services (id, name, code, organization_id) "
                        "VALUES (:id, 'Accueil', :code, :org)"
                    ),
                    {"id": org.hex, "code": f"ACC-{code}", "org": org.hex},
                )
            connexion.execute(
                text(
                    "INSERT INTO users (id, nom, identifiant, mot_de_passe_hash, role, is_active) "
                    "VALUES (:id, 'Agent', 'agent', 'x', 'AGENT_CONTROLE', 1)"
                ),
                {"id": agent},
            )
            connexion.execute(
                text(
                    "INSERT INTO visitors (id, prenom, nom, type_document, numero_document, "
                    "nin, document_recto_url) "
                    "VALUES (:id, 'Khady', 'Ndoye', 'CNI', 'PAD0000001', '1751199000123', :photo)"
                ),
                {"id": visiteur, "photo": photo_du_port},
            )
            for jour, org in (("2026-10-01", port), ("2026-11-01", ministere)):
                connexion.execute(
                    text(
                        "INSERT INTO visits (id, visitor_id, service_id, statut, checked_in_at, "
                        "checked_in_by, organization_id) "
                        "VALUES (:id, :visiteur, :org, 'SORTI', :jour, :agent, :org)"
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "visiteur": visiteur,
                        "org": org.hex,
                        "jour": f"{jour} 09:00:00",
                        "agent": agent,
                    },
                )

        command.upgrade(config, "head")

        with engine.connect() as connexion:
            fiches = {
                uuid.UUID(org): (id_, photo)
                for id_, org, photo in connexion.execute(
                    text("SELECT id, organization_id, document_recto_url FROM visitors")
                )
            }
            rattachement = dict(
                (uuid.UUID(org), id_)
                for org, id_ in connexion.execute(
                    text("SELECT organization_id, visitor_id FROM visits")
                )
            )
        assert set(fiches) == {port, ministere}
        # La fiche d'origine revient à la première organisation, avec sa photo.
        assert fiches[port] == (visiteur, photo_du_port)
        # La copie du ministère ne pointe pas vers le dossier du port.
        assert fiches[ministere][1] is None
        assert rattachement == {port: visiteur, ministere: fiches[ministere][0]}

        command.downgrade(config, "d8f1b3c5e7a9")
        with engine.connect() as connexion:
            restantes = connexion.execute(text("SELECT id FROM visitors")).scalars().all()
            visites = set(connexion.execute(text("SELECT visitor_id FROM visits")).scalars())
        assert restantes == [visiteur]
        assert visites == {visiteur}
    finally:
        engine.dispose()
