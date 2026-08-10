from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin


class AuditLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Trace immuable d'un événement sensible.

    Un registre de visites d'un ministère doit pouvoir répondre à « qui a annulé
    cette visite, quand, et que contenait-elle avant ? ». Les logs applicatifs ne
    suffisent pas : ils sont volatils et non requêtables par entité.

    Ces lignes ne sont jamais modifiées ni supprimées par l'application — aucune
    route d'écriture n'est exposée, seule la lecture l'est.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        # Le dashboard filtre presque toujours sur une entité précise ou sur un
        # acteur, avec un tri antéchronologique.
        Index("ix_audit_logs_entity_entity_id", "entity", "entity_id"),
        Index("ix_audit_logs_actor_id_created_at", "actor_id", "created_at"),
    )

    # `SET NULL` plutôt que `CASCADE` : supprimer un compte ne doit pas effacer la
    # trace de ce qu'il a fait. Nul aussi pour les échecs de connexion, où l'acteur
    # n'est pas authentifié.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Identifiant saisi, conservé même quand `actor_id` est nul : sur un échec de
    # connexion, c'est la seule information disponible sur qui a tenté.
    actor_identifiant: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Verbe hiérarchique : `visit.cancelled`, `user.created`, `auth.login.failed`.
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Diff avant/après, motif, filtres d'un export… Attribut nommé `meta` car
    # `metadata` est réservé par SQLAlchemy (il désigne l'objet `MetaData`) ;
    # la colonne SQL et le champ d'API, eux, s'appellent bien `metadata`.
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONType, nullable=True)

    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - aide au debug uniquement
        return f"<AuditLog {self.action} {self.entity}:{self.entity_id}>"
