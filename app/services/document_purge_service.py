"""Purge des photos de pièces d'identité arrivées au terme de leur conservation.

Ces images sont la donnée la plus sensible de l'application : une photo de CNI
identifie complètement une personne. Les conserver indéfiniment n'a aucune
justification métier passé un certain délai — le registre, lui, reste complet.

La purge ne touche donc **que les images**. Les visites, les visiteurs et le
journal d'audit restent intacts : le registre doit pouvoir répondre à « qui est
venu, quand, voir qui », des années après que la photo a été effacée (ADR-018).

Déclenchée en ligne de commande (`python -m app.purge_documents`), jamais
automatiquement : une suppression silencieuse dont personne ne voit l'échec est
pire qu'une suppression qu'il faut planifier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.repositories.visit_repository import COLONNES_IMAGES_DOCUMENT, VisitorRepository
from app.services.audit_service import AuditService
from app.services.setting_service import SettingService
from app.services.storage_service import StorageService

logger = get_logger(__name__)

# Traité par lots : une installation ancienne peut porter des dizaines de milliers
# de fiches, et les charger toutes en mémoire pour les parcourir serait gratuit.
TAILLE_LOT = 200


@dataclass(frozen=True, slots=True)
class PurgeReport:
    """Résultat d'une purge, pour l'affichage en ligne de commande."""

    retention_days: int
    cutoff: datetime | None
    visiteurs: int
    images: int
    dry_run: bool

    @property
    def desactivee(self) -> bool:
        return self.cutoff is None


class DocumentPurgeService:
    def __init__(self, session: AsyncSession, storage: StorageService) -> None:
        self.session = session
        self.storage = storage
        self.visitors = VisitorRepository(session)
        self.settings = SettingService(session)
        self.audit = AuditService(session)

    async def purge(
        self, *, retention_days: int | None = None, dry_run: bool = False
    ) -> PurgeReport:
        """Efface les images périmées et vide les colonnes correspondantes.

        `retention_days` force la durée pour cette exécution ; sans lui, le
        paramètre système fait foi. À `0`, la purge est désactivée et ne parcourt
        même pas la base.
        """
        if retention_days is None:
            parametres = await self.settings.get_settings()
            retention_days = parametres.document_images_retention_days

        if retention_days <= 0:
            return PurgeReport(
                retention_days=retention_days,
                cutoff=None,
                visiteurs=0,
                images=0,
                dry_run=dry_run,
            )

        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        visiteurs = 0
        images = 0
        offset = 0

        while True:
            lot = await self.visitors.list_documents_expires(
                cutoff, limit=TAILLE_LOT, offset=offset
            )
            if not lot:
                break

            for visiteur in lot:
                urls = [
                    getattr(visiteur, colonne)
                    for colonne in COLONNES_IMAGES_DOCUMENT
                    if getattr(visiteur, colonne)
                ]
                visiteurs += 1
                images += len(urls)
                if dry_run:
                    continue

                for url in urls:
                    await self.storage.delete(url)
                for colonne in COLONNES_IMAGES_DOCUMENT:
                    setattr(visiteur, colonne, None)

            if dry_run:
                # Rien n'est modifié : sans curseur, le lot suivant ramènerait
                # exactement les mêmes fiches et la boucle ne finirait jamais.
                offset += len(lot)
                continue

            await self.audit.record(
                "visitor.documents_purged",
                entity="visitor",
                actor_identifiant="system:purge",
                metadata={
                    "retention_days": retention_days,
                    "cutoff": cutoff,
                    "visiteurs": len(lot),
                },
            )
            await self.session.commit()

        logger.info(
            "Purge des images de pièces d'identité",
            extra={
                "retention_days": retention_days,
                "visiteurs": visiteurs,
                "images": images,
                "dry_run": dry_run,
            },
        )
        return PurgeReport(
            retention_days=retention_days,
            cutoff=cutoff,
            visiteurs=visiteurs,
            images=images,
            dry_run=dry_run,
        )
