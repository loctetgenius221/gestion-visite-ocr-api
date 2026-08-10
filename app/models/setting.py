from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin


class SystemSetting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Paramètre système modifiable par un administrateur, sans redéploiement.

    Volontairement une table clé/valeur et non une ligne unique à colonnes fixes :
    ajouter un paramètre ne demande alors aucune migration. Les valeurs par défaut
    et le typage vivent côté application (`app/services/setting_service.py`), qui
    reste l'autorité — la base ne stocke que les écarts au défaut.

    Ne mettez ici que ce qui relève du **métier** (délais, seuils). Tout ce qui
    touche à l'infrastructure ou aux secrets reste dans les variables
    d'environnement : une valeur en base est modifiable par une route HTTP.
    """

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    # Encapsulée dans un objet JSON (`{"value": ...}`) plutôt que stockée nue : les
    # scalaires JSON au niveau racine ne sont pas portables entre PostgreSQL et SQLite.
    value: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - aide au debug uniquement
        return f"<SystemSetting {self.key}>"
