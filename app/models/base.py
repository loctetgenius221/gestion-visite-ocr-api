from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, MetaData, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# JSONB sur PostgreSQL (indexable, stockage binaire), JSON ailleurs — SQLite en
# test ne connaît pas JSONB.
JSONType = JSON().with_variant(JSONB(), "postgresql")

# Conventions de nommage explicites : sans elles, Alembic génère des migrations
# avec des contraintes anonymes impossibles à supprimer proprement ensuite.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(moment: datetime) -> datetime:
    """Garantit un `datetime` conscient du fuseau, supposé UTC à défaut.

    PostgreSQL relit les colonnes `TIMESTAMP WITH TIME ZONE` en datetimes
    conscients ; SQLite, utilisé en test, les rend **naïfs**. Comparer une valeur
    relue à une valeur reçue d'un client — toujours consciente — lève alors
    `TypeError: can't compare offset-naive and offset-aware datetimes`.

    Tout ce que l'application écrit est en UTC : la supposition est sûre.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class ArchivableMixin:
    """Archivage logique des référentiels.

    Aucune suppression physique n'est possible sur `services`, `agents` et
    `purposes` : les visites déjà enregistrées les référencent, et un `DELETE`
    ferait perdre l'historique du registre — ou échouerait sur la clé étrangère.
    Archiver retire l'entrée des listes proposées à l'agent sans toucher au passé.
    """

    is_archived: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False, index=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
