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
