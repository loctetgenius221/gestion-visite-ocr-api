"""Purge des photos de pièces d'identité (ADR-018).

La règle la plus importante n'est pas ce qui est supprimé, mais ce qui **survit** :
le registre doit rester capable de dire qui est venu et quand, des années après que
la photo de la pièce a été effacée.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.visit import Visit
from app.models.visitor import Visitor
from app.services.document_purge_service import DocumentPurgeService
from app.services.storage_service import StorageService


class StockageEspion(StorageService):
    """Stockage en mémoire : on veut vérifier les suppressions, pas toucher au disque."""

    def __init__(self) -> None:
        self.supprimes: list[str] = []

    async def save(self, content: bytes, *, folder: str, extension: str) -> str:
        return f"/storage/uploads/{folder}/fichier{extension}"

    async def delete(self, url: str) -> None:
        self.supprimes.append(url)


async def _visiteur_avec_images(
    session: AsyncSession, seeded: dict, *, derniere_visite_il_y_a_jours: int
) -> Visitor:
    """Visiteur porteur de trois images, dont la dernière visite est datée."""
    visitor = Visitor(
        prenom="Aminata",
        nom="Diop",
        type_document="CNI",
        numero_document=f"CNI{derniere_visite_il_y_a_jours:05d}",
        document_recto_url="/storage/uploads/documents/recto/r.png",
        document_verso_url="/storage/uploads/documents/verso/v.png",
        mrz_image_url="/storage/uploads/mrz/m.png",
    )
    session.add(visitor)
    await session.flush()

    session.add(
        Visit(
            visitor_id=visitor.id,
            service_id=seeded["service"].id,
            agent_id=seeded["agent"].id,
            purpose_id=seeded["purpose"].id,
            checked_in_at=datetime.now(UTC) - timedelta(days=derniere_visite_il_y_a_jours),
            checked_in_by=seeded["user"].id,
        )
    )
    await session.commit()
    return visitor


@pytest.fixture
def stockage() -> StockageEspion:
    return StockageEspion()


class TestPurgeDesImages:
    async def test_les_images_trop_anciennes_sont_supprimees(
        self, session: AsyncSession, seeded: dict, stockage: StockageEspion
    ):
        visitor = await _visiteur_avec_images(session, seeded, derniere_visite_il_y_a_jours=400)

        rapport = await DocumentPurgeService(session, stockage).purge(retention_days=365)

        assert rapport.visiteurs == 1
        assert rapport.images == 3
        assert len(stockage.supprimes) == 3
        await session.refresh(visitor)
        assert visitor.document_recto_url is None
        assert visitor.document_verso_url is None
        assert visitor.mrz_image_url is None

    async def test_une_visite_recente_protege_les_images(
        self, session: AsyncSession, seeded: dict, stockage: StockageEspion
    ):
        """La référence est la dernière venue, pas la date de la photo."""
        visitor = await _visiteur_avec_images(session, seeded, derniere_visite_il_y_a_jours=10)

        rapport = await DocumentPurgeService(session, stockage).purge(retention_days=365)

        assert rapport.visiteurs == 0
        assert stockage.supprimes == []
        await session.refresh(visitor)
        assert visitor.document_recto_url is not None

    async def test_le_registre_survit_a_la_purge(
        self, session: AsyncSession, seeded: dict, stockage: StockageEspion
    ):
        """Ce qui compte : la visite et le visiteur restent, seule l'image part."""
        visitor = await _visiteur_avec_images(session, seeded, derniere_visite_il_y_a_jours=400)

        await DocumentPurgeService(session, stockage).purge(retention_days=365)

        visites = (
            await session.execute(select(Visit).where(Visit.visitor_id == visitor.id))
        ).scalars().all()
        assert len(visites) == 1
        await session.refresh(visitor)
        assert visitor.nom == "Diop"
        assert visitor.numero_document == "CNI00400"

    async def test_dry_run_ne_supprime_rien(
        self, session: AsyncSession, seeded: dict, stockage: StockageEspion
    ):
        visitor = await _visiteur_avec_images(session, seeded, derniere_visite_il_y_a_jours=400)

        rapport = await DocumentPurgeService(session, stockage).purge(
            retention_days=365, dry_run=True
        )

        assert rapport.visiteurs == 1
        assert rapport.images == 3
        assert rapport.dry_run is True
        assert stockage.supprimes == []
        await session.refresh(visitor)
        assert visitor.document_recto_url is not None

    async def test_retention_a_zero_desactive_la_purge(
        self, session: AsyncSession, seeded: dict, stockage: StockageEspion
    ):
        visitor = await _visiteur_avec_images(session, seeded, derniere_visite_il_y_a_jours=4000)

        rapport = await DocumentPurgeService(session, stockage).purge(retention_days=0)

        assert rapport.desactivee is True
        assert rapport.visiteurs == 0
        assert stockage.supprimes == []
        await session.refresh(visitor)
        assert visitor.document_recto_url is not None

    async def test_un_visiteur_sans_image_nest_pas_compte(
        self, session: AsyncSession, seeded: dict, stockage: StockageEspion
    ):
        session.add(
            Visitor(
                prenom="Moussa",
                nom="Sagna",
                type_document="CNI",
                numero_document="SANSIMAGE",
            )
        )
        await session.commit()

        rapport = await DocumentPurgeService(session, stockage).purge(retention_days=1)

        assert rapport.visiteurs == 0

    async def test_la_purge_laisse_une_trace_daudit(
        self, session: AsyncSession, seeded: dict, stockage: StockageEspion
    ):
        """Une suppression de données doit être défendable après coup."""
        await _visiteur_avec_images(session, seeded, derniere_visite_il_y_a_jours=400)

        await DocumentPurgeService(session, stockage).purge(retention_days=365)

        traces = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "visitor.documents_purged")
            )
        ).scalars().all()
        assert len(traces) == 1
        assert traces[0].actor_identifiant == "system:purge"
        assert traces[0].meta["retention_days"] == 365

    async def test_le_parametre_systeme_fait_foi_par_defaut(
        self, session: AsyncSession, seeded: dict, stockage: StockageEspion
    ):
        """Sans durée explicite, le défaut du schéma (365 jours) s'applique."""
        await _visiteur_avec_images(session, seeded, derniere_visite_il_y_a_jours=400)

        rapport = await DocumentPurgeService(session, stockage).purge()

        assert rapport.retention_days == 365
        assert rapport.visiteurs == 1
