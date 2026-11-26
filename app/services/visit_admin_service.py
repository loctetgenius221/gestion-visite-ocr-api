"""Administration du registre : correction, annulation, suppression, export."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.errors import (
    AgentNotFoundError,
    ConflictError,
    CrossOrganizationError,
    ExportFormatUnavailableError,
    PurposeNotFoundError,
    ServiceNotFoundError,
    UnprocessableError,
    VisitCancelledError,
)
from app.core.logging import get_logger
from app.models.base import ensure_utc
from app.models.enums import VisitStatus
from app.models.user import User
from app.models.visit import Visit
from app.schemas.visit import (
    VisitFilters,
    VisitUpdate,
)
from app.services.audit_service import AuditAction, diff
from app.services.export_service import ExportFormat, visits_to_csv
from app.services.setting_service import SettingService
from app.services.visit_service import VisitService

logger = get_logger(__name__)


class VisitAdminService(VisitService):
    """Opérations réservées aux administrateurs, toutes tracées au journal."""

    async def update_visit(
        self, visit_id: uuid.UUID, payload: VisitUpdate, current_user: User
    ) -> Visit:
        """Corrige une visite mal saisie.

        Le motif est obligatoire et part au journal avec le diff avant/après :
        c'est ce qui rend la correction défendable lors d'un audit. La visite
        elle-même ne porte pas ce motif — elle n'est pas censée raconter son
        histoire, le journal s'en charge.
        """
        visit = await self.get_visit(visit_id, current_user)
        if visit.statut is VisitStatus.ANNULEE:
            raise VisitCancelledError(details={"visit_id": str(visit_id)})

        modifications = payload.model_dump(exclude={"reason"}, exclude_unset=True)
        await self._verifier_references(visit, modifications)

        champs = [
            "service_id",
            "agent_id",
            "purpose_id",
            "motif_libre",
            "badge_number",
            "checked_in_at",
            "checked_out_at",
        ]
        avant = {champ: getattr(visit, champ) for champ in champs}
        for champ, valeur in modifications.items():
            setattr(visit, champ, valeur)
        apres = {champ: getattr(visit, champ) for champ in champs}

        if visit.checked_out_at is not None and ensure_utc(visit.checked_out_at) < ensure_utc(
            visit.checked_in_at
        ):
            raise UnprocessableError(
                "La sortie ne peut pas précéder l'entrée.",
                error_code="INVALID_VISIT_INTERVAL",
                details={
                    "checked_in_at": visit.checked_in_at,
                    "checked_out_at": visit.checked_out_at,
                },
            )
        # Corriger l'horodatage de sortie d'une visite encore ouverte revient à la
        # clôturer : le statut doit suivre, sinon la visite resterait « présente »
        # avec une date de sortie renseignée.
        if visit.checked_out_at is not None and visit.statut is VisitStatus.PRESENT:
            visit.statut = VisitStatus.SORTI
            visit.checked_out_by = current_user.id

        await self.audit.record(
            AuditAction.VISIT_UPDATED,
            entity="visit",
            entity_id=visit.id,
            actor=current_user,
            metadata={"reason": payload.reason, "changements": diff(avant, apres)},
            context=self.context,
        )
        await self.session.commit()

        refreshed = await self.visits.get_by_id(visit.id)
        if refreshed is None:  # invariant : un `assert` disparaît sous `python -O`
            raise RuntimeError("Invariant violé : refreshed est None.")
        return refreshed

    async def cancel_visit(self, visit_id: uuid.UUID, reason: str, current_user: User) -> Visit:
        """Annulation logique. La visite reste en base, avec son motif d'annulation."""
        visit = await self.get_visit(visit_id, current_user)
        if visit.statut is VisitStatus.ANNULEE:
            raise VisitCancelledError(details={"visit_id": str(visit_id)})

        visit.statut = VisitStatus.ANNULEE
        visit.cancelled_at = datetime.now(UTC)
        visit.cancelled_by = current_user.id
        visit.cancellation_reason = reason

        await self.audit.record(
            AuditAction.VISIT_CANCELLED,
            entity="visit",
            entity_id=visit.id,
            actor=current_user,
            metadata={"reason": reason},
            context=self.context,
        )
        await self.session.commit()

        refreshed = await self.visits.get_by_id(visit.id)
        if refreshed is None:  # invariant : un `assert` disparaît sous `python -O`
            raise RuntimeError("Invariant violé : refreshed est None.")
        return refreshed

    async def delete_visit(self, visit_id: uuid.UUID, current_user: User) -> None:
        """Suppression **définitive** d'une visite.

        À distinguer de `cancel_visit`, qui est la voie normale : une visite
        annulée reste au registre, motif à l'appui, et se relit des années après.
        Ici la ligne disparaît — réservé aux saisies qui n'auraient jamais dû
        exister (test, doublon manifeste), pas à la correction d'une visite réelle.

        Le journal devient alors la **seule** trace restante : l'entrée
        `visit.deleted` emporte donc un instantané complet de ce qui est détruit,
        et non le seul identifiant. Sans lui, la question « que contenait cette
        visite ? » n'aurait plus de réponse nulle part, ce qui retirerait au
        registre la vérifiabilité qui en fait un registre.

        Le visiteur, lui, n'est pas supprimé : sa fiche sert aux autres passages,
        et ses photos de pièce relèvent de la purge (ADR-018).
        """
        visit = await self.get_visit(visit_id, current_user)

        await self.audit.record(
            AuditAction.VISIT_DELETED,
            entity="visit",
            entity_id=visit.id,
            actor=current_user,
            metadata={"visite": self._instantane(visit)},
            context=self.context,
        )
        await self.visits.delete(visit)
        await self.session.commit()

        logger.info(
            "Visite supprimée",
            extra={"visit_id": str(visit_id), "acteur": str(current_user.id)},
        )

    @staticmethod
    def _instantane(visit: Visit) -> dict[str, Any]:
        """Contenu d'une visite, figé pour le journal avant sa suppression.

        Le visiteur est nommé en clair : relire l'entrée des mois plus tard ne doit
        pas dépendre d'un `visitor_id` dont la fiche a pu être purgée depuis.
        """
        return {
            "visitor_id": visit.visitor_id,
            "visiteur": f"{visit.visitor.prenom} {visit.visitor.nom}",
            "numero_document": visit.visitor.numero_document,
            "service_id": visit.service_id,
            "agent_id": visit.agent_id,
            "purpose_id": visit.purpose_id,
            "motif_libre": visit.motif_libre,
            "badge_number": visit.badge_number,
            "statut": visit.statut,
            "checked_in_at": visit.checked_in_at,
            "checked_out_at": visit.checked_out_at,
            "checked_in_by": visit.checked_in_by,
            "checked_out_by": visit.checked_out_by,
            "cancelled_at": visit.cancelled_at,
            "cancellation_reason": visit.cancellation_reason,
            "client_reference": visit.client_reference,
        }

    async def export_visits(
        self, filters: VisitFilters, fmt: ExportFormat, current_user: User
    ) -> tuple[bytes, str]:
        """Exporte le registre filtré. Retourne `(contenu, type MIME)`."""
        if fmt != "csv":
            raise ExportFormatUnavailableError(
                "Seul l'export CSV est disponible pour le moment.",
                details={"format": fmt, "formats_disponibles": ["csv"]},
            )

        parametres = await SettingService(self.session).get_settings()
        plafond = parametres.visits_export_max_rows

        # `plafond + 1` : si la requête ramène une ligne de plus que le plafond,
        # c'est qu'il y en a davantage. On refuse alors, plutôt que de livrer un
        # export tronqué que personne ne remarquerait.
        visits = await self.visits.list_for_export(filters, limit=plafond + 1)
        if len(visits) > plafond:
            raise UnprocessableError(
                "L'export dépasse le nombre de lignes autorisé : affinez les filtres.",
                error_code="EXPORT_TOO_LARGE",
                details={"max_rows": plafond},
            )

        await self.audit.record(
            AuditAction.VISIT_EXPORTED,
            entity="visit",
            actor=current_user,
            metadata={
                "format": fmt,
                "lignes": len(visits),
                "filtres": filters.model_dump(exclude_none=True),
            },
            context=self.context,
        )
        await self.session.commit()
        return visits_to_csv(visits), "text/csv; charset=utf-8"

    async def _verifier_references(self, visit: Visit, modifications: dict[str, Any]) -> None:
        """Contrôle les référentiels d'une correction, y compris leur cohérence croisée.

        Les valeurs proviennent de `VisitUpdate.model_dump()` : Pydantic a déjà
        validé qu'un `service_id` fourni est bien un UUID.

        Le repli passe par `get(clé, défaut)` et non par un `or` : depuis que la
        personne rencontrée est facultative (ADR-019), `{"agent_id": null}` veut
        dire « retire-la ». Un `or` confondrait cet effacement volontaire avec une
        absence de modification, et retomberait sur l'agent actuel.
        """
        service_id: uuid.UUID = modifications.get("service_id", visit.service_id)
        agent_id: uuid.UUID | None = modifications.get("agent_id", visit.agent_id)

        if "service_id" in modifications:
            service = await self.services.get_by_id(service_id)
            if service is None:
                raise ServiceNotFoundError(details={"service_id": str(service_id)})
            # Une correction ne fait pas changer une visite de registre (ADR-023).
            if service.organization_id != visit.organization_id:
                raise CrossOrganizationError(
                    "Ce service appartient à une autre organisation.",
                    details={"service_id": str(service_id)},
                )

        # Sans personne rencontrée, il n'y a plus de cohérence agent/service à
        # vérifier — y compris quand c'est le service qui change.
        if agent_id is not None and ("agent_id" in modifications or "service_id" in modifications):
            agent = await self.agents.get_by_id(agent_id)
            if agent is None:
                raise AgentNotFoundError(details={"agent_id": str(agent_id)})
            if agent.service_id != service_id:
                raise ConflictError(
                    "L'agent sélectionné n'appartient pas au service indiqué.",
                    error_code="AGENT_SERVICE_MISMATCH",
                    details={"agent_id": str(agent_id), "service_id": str(service_id)},
                )

        purpose_id: uuid.UUID | None = modifications.get("purpose_id")
        if purpose_id is not None:
            purpose = await self.purposes.get_by_id(purpose_id)
            if purpose is None:
                raise PurposeNotFoundError(details={"purpose_id": str(purpose_id)})
            if purpose.organization_id not in (None, visit.organization_id):
                raise CrossOrganizationError(
                    "Ce motif appartient à une autre organisation.",
                    details={"purpose_id": str(purpose_id)},
                )
