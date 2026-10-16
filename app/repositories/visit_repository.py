from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.enums import DocumentType, VisitStatus
from app.models.referentiel import Agent, Purpose, Service
from app.models.visit import Visit
from app.models.visitor import Visitor
from app.schemas.visit import VisitFilters

# Colonnes de `Visitor` portant une photo de pièce d'identité. Les signatures n'en
# font pas partie : elles attestent du passage lui-même et appartiennent au registre.
COLONNES_IMAGES_DOCUMENT = ("document_recto_url", "document_verso_url", "mrz_image_url")


class VisitorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_document(
        self, type_document: DocumentType, numero_document: str
    ) -> Visitor | None:
        """Un visiteur est identifié par le couple (type de document, numéro de document)."""
        stmt = select(Visitor).where(
            Visitor.type_document == type_document,
            Visitor.numero_document == numero_document,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def get_by_id(self, visitor_id: uuid.UUID) -> Visitor | None:
        return await self.session.get(Visitor, visitor_id)

    async def add(self, visitor: Visitor) -> Visitor:
        self.session.add(visitor)
        await self.session.flush()
        return visitor

    async def list_documents_expires(
        self, avant: datetime, *, limit: int, offset: int = 0
    ) -> list[Visitor]:
        """Visiteurs dont les images de pièce ont dépassé la durée de conservation.

        La date de référence est la **dernière visite**, pas la date de la photo :
        une personne qui revient chaque mois garde ses images, quelqu'un qui n'est
        plus venu depuis deux ans les perd. À défaut de visite — cas transitoire —
        la création de la fiche fait foi.

        Les visites annulées comptent : elles attestent quand même d'un passage au
        poste, et retenir l'hypothèse la plus prudente évite d'effacer une pièce
        justificative encore utile.
        """
        reference = func.coalesce(func.max(Visit.checked_in_at), Visitor.created_at)
        stmt = (
            select(Visitor)
            .outerjoin(Visit, Visit.visitor_id == Visitor.id)
            .where(or_(*(getattr(Visitor, c).is_not(None) for c in COLONNES_IMAGES_DOCUMENT)))
            .group_by(Visitor.id)
            .having(reference < avant)
            .order_by(Visitor.id)
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    @staticmethod
    def _clause_recherche(terme: str) -> ColumnElement[bool]:
        """Recherche sur l'identité et sur les numéros.

        Les numéros sont comparés **compactés** : le NIN et le numéro de carte sont
        imprimés par blocs (« 2 K05 2012 00108 ») et l'agent les recopie avec leurs
        espaces, alors que la base les stocke sans séparateurs (ADR-016).
        """
        motif = f"%{terme.strip().lower()}%"
        clauses = [
            func.lower(Visitor.nom).like(motif),
            func.lower(Visitor.prenom).like(motif),
        ]

        compacte = re.sub(r"[^a-z0-9]", "", terme.lower())
        # Un terme réduit à de la ponctuation donnerait `%%`, qui remonterait tout
        # le fichier des visiteurs.
        if compacte:
            clauses.append(func.lower(Visitor.numero_document).like(f"%{compacte}%"))
            clauses.append(func.lower(Visitor.nin).like(f"%{compacte}%"))

        return or_(*clauses)

    @staticmethod
    def _clause_organisation(organization_id: uuid.UUID) -> ColumnElement[bool]:
        """Restreint aux visiteurs déjà venus dans l'organisation donnée.

        `Visitor` reste volontairement **global** : une même personne peut se
        présenter dans plusieurs structures, et lui créer une fiche par
        organisation dupliquerait son identité — donc son numéro de pièce et son
        NIN — autant de fois qu'elle a d'interlocuteurs. Le cloisonnement porte
        donc sur le lien de visite, pas sur la fiche : on ne retrouve une personne
        que si elle est déjà venue chez soi.
        """
        venue = aliased(Visit)
        return (
            select(venue.id)
            .where(venue.visitor_id == Visitor.id, venue.organization_id == organization_id)
            .correlate(Visitor)
            .exists()
        )

    async def count_search(self, terme: str, *, organization_id: uuid.UUID | None = None) -> int:
        stmt = select(func.count(Visitor.id)).where(self._clause_recherche(terme))
        if organization_id is not None:
            stmt = stmt.where(self._clause_organisation(organization_id))
        return (await self.session.execute(stmt)).scalar_one()

    async def search(
        self,
        terme: str,
        *,
        limit: int,
        offset: int,
        organization_id: uuid.UUID | None = None,
    ) -> list[tuple[Visitor, datetime | None, uuid.UUID | None]]:
        """Fiches correspondantes, avec dernière venue et visite ouverte éventuelle.

        Les deux informations sont ramenées **dans la même requête** : les calculer
        fiche par fiche côté service produirait un N+1 sur une route appelée à
        chaque frappe de l'agent.

        Les visites annulées sont exclues de la dernière venue — une visite annulée
        est une erreur de saisie, l'afficher comme un passage induirait en erreur.
        """
        ouverte = aliased(Visit)
        conditions_ouverte = [
            ouverte.visitor_id == Visitor.id,
            ouverte.statut == VisitStatus.PRESENT,
        ]
        conditions_derniere = [
            Visit.visitor_id == Visitor.id,
            Visit.statut != VisitStatus.ANNULEE,
        ]
        # La dernière venue et la visite ouverte sont elles aussi restreintes à
        # l'organisation de l'appelant : sans cela, la fiche révélerait qu'une
        # personne s'est présentée ailleurs, et à quelle date.
        if organization_id is not None:
            conditions_ouverte.append(ouverte.organization_id == organization_id)
            conditions_derniere.append(Visit.organization_id == organization_id)

        visite_ouverte_id = (
            select(ouverte.id)
            .where(*conditions_ouverte)
            .order_by(ouverte.checked_in_at.desc())
            .limit(1)
            .correlate(Visitor)
            .scalar_subquery()
        )
        derniere_visite_at = func.max(Visit.checked_in_at)

        stmt = (
            select(Visitor, derniere_visite_at, visite_ouverte_id)
            .outerjoin(Visit, and_(*conditions_derniere))
            .where(self._clause_recherche(terme))
            .group_by(Visitor.id)
            # `nulls_last` explicite : PostgreSQL place les NULL en tête sur un tri
            # descendant, SQLite en queue. Le tri ne doit pas dépendre du moteur.
            .order_by(derniere_visite_at.desc().nulls_last(), Visitor.nom, Visitor.id)
            .limit(limit)
            .offset(offset)
        )
        if organization_id is not None:
            stmt = stmt.where(self._clause_organisation(organization_id))
        return [
            (visiteur, derniere, ouverte_id)
            for visiteur, derniere, ouverte_id in (await self.session.execute(stmt)).all()
        ]


class VisitRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, visit: Visit) -> Visit:
        self.session.add(visit)
        await self.session.flush()
        return visit

    async def delete(self, visit: Visit) -> None:
        """Suppression **physique**. Aucune table ne référence `visits` : rien à
        cascader, et le journal d'audit — dont `entity_id` est une chaîne libre,
        sans clé étrangère — survit à la ligne qu'il décrit."""
        await self.session.delete(visit)
        await self.session.flush()

    async def get_by_id(self, visit_id: uuid.UUID) -> Visit | None:
        # `populate_existing` force le rafraîchissement de l'instance déjà présente
        # dans l'identity map : sans lui, une relation chargée avant modification
        # (ex. `checked_out_user` encore nul avant la clôture) resterait périmée.
        stmt = (
            select(Visit)
            .where(Visit.id == visit_id)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).unique().scalars().first()

    async def get_by_client_reference(
        self, client_reference: str, organization_id: uuid.UUID
    ) -> Visit | None:
        """La clé d'idempotence n'a de sens que dans l'organisation qui l'a émise."""
        stmt = select(Visit).where(
            Visit.client_reference == client_reference,
            Visit.organization_id == organization_id,
        )
        return (await self.session.execute(stmt)).unique().scalars().first()

    async def find_open_visit(
        self, visitor_id: uuid.UUID, organization_id: uuid.UUID
    ) -> Visit | None:
        """Visite encore ouverte pour ce visiteur **dans cette organisation**.

        Une personne présente au ministère A peut se présenter au ministère B :
        ce sont deux registres distincts. Chercher sur tout le parc l'aurait
        refusée chez B, et aurait révélé à B l'existence et l'heure de sa visite
        chez A.
        """
        stmt = select(Visit).where(
            Visit.visitor_id == visitor_id,
            Visit.organization_id == organization_id,
            Visit.statut == VisitStatus.PRESENT,
        )
        return (await self.session.execute(stmt)).unique().scalars().first()

    async def visitor_has_visited(self, visitor_id: uuid.UUID, organization_id: uuid.UUID) -> bool:
        """La personne est-elle déjà venue dans cette organisation ?"""
        stmt = (
            select(Visit.id)
            .where(Visit.visitor_id == visitor_id, Visit.organization_id == organization_id)
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    @staticmethod
    def _apply_filters(stmt: Select, filters: VisitFilters) -> Select:
        if filters.organization_id is not None:
            stmt = stmt.where(Visit.organization_id == filters.organization_id)
        if filters.statut is not None:
            stmt = stmt.where(Visit.statut == filters.statut)
        if filters.checkout_origin is not None:
            stmt = stmt.where(Visit.checkout_origin == filters.checkout_origin)
        if filters.date_from is not None:
            stmt = stmt.where(Visit.checked_in_at >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(Visit.checked_in_at <= filters.date_to)
        # Filtres du dashboard web. L'app mobile ne les envoie pas : à `None`, ils
        # n'ajoutent aucune clause et la requête reste celle d'avant.
        if filters.service_id is not None:
            stmt = stmt.where(Visit.service_id == filters.service_id)
        if filters.agent_id is not None:
            stmt = stmt.where(Visit.agent_id == filters.agent_id)
        if filters.purpose_id is not None:
            stmt = stmt.where(Visit.purpose_id == filters.purpose_id)
        if filters.created_by is not None:
            stmt = stmt.where(Visit.checked_in_by == filters.created_by)
        if filters.search:
            pattern = f"%{filters.search.strip().lower()}%"
            stmt = stmt.join(Visitor, Visit.visitor_id == Visitor.id).where(
                or_(
                    func.lower(Visitor.nom).like(pattern),
                    func.lower(Visitor.prenom).like(pattern),
                    func.lower(Visitor.numero_document).like(pattern),
                )
            )
        return stmt

    async def count(self, filters: VisitFilters) -> int:
        stmt = self._apply_filters(select(func.count(Visit.id)).select_from(Visit), filters)
        return (await self.session.execute(stmt)).scalar_one()

    async def list_paginated(
        self, filters: VisitFilters, *, limit: int, offset: int
    ) -> list[Visit]:
        stmt = self._apply_filters(select(Visit), filters)
        order = Visit.checked_in_at.asc() if filters.sort == "asc" else Visit.checked_in_at.desc()
        stmt = stmt.order_by(order, Visit.id).limit(limit).offset(offset)
        return list((await self.session.execute(stmt)).unique().scalars().all())

    async def list_for_export(self, filters: VisitFilters, *, limit: int) -> list[Visit]:
        """Lignes d'un export, ordonnées chronologiquement.

        Bornée par `limit` : un export sans plafond sur plusieurs années de
        registre chargerait tout en mémoire. Le service refuse au-delà plutôt que
        de tronquer silencieusement — un export incomplet mais silencieux serait
        pire qu'une erreur.
        """
        stmt = self._apply_filters(select(Visit), filters)
        stmt = stmt.order_by(Visit.checked_in_at.asc(), Visit.id).limit(limit)
        return list((await self.session.execute(stmt)).unique().scalars().all())

    # --- Statistiques dashboard ---
    #
    # Les visites annulées sont exclues de tous les agrégats : une visite annulée
    # est une erreur de saisie, la compter fausserait les chiffres.

    async def count_checked_in_between(
        self,
        start: datetime,
        end: datetime,
        organization_id: uuid.UUID | None = None,
    ) -> int:
        stmt = select(func.count(Visit.id)).where(
            Visit.checked_in_at >= start,
            Visit.checked_in_at < end,
            Visit.statut != VisitStatus.ANNULEE,
        )
        if organization_id is not None:
            stmt = stmt.where(Visit.organization_id == organization_id)
        return (await self.session.execute(stmt)).scalar_one()

    async def count_checked_out_between(
        self,
        start: datetime,
        end: datetime,
        organization_id: uuid.UUID | None = None,
    ) -> int:
        stmt = select(func.count(Visit.id)).where(
            Visit.checked_out_at.is_not(None),
            Visit.checked_out_at >= start,
            Visit.checked_out_at < end,
            Visit.statut != VisitStatus.ANNULEE,
        )
        if organization_id is not None:
            stmt = stmt.where(Visit.organization_id == organization_id)
        return (await self.session.execute(stmt)).scalar_one()

    async def count_present(self, organization_id: uuid.UUID | None = None) -> int:
        stmt = select(func.count(Visit.id)).where(Visit.statut == VisitStatus.PRESENT)
        if organization_id is not None:
            stmt = stmt.where(Visit.organization_id == organization_id)
        return (await self.session.execute(stmt)).scalar_one()

    async def closed_visits_durations_seconds(
        self,
        start: datetime,
        end: datetime,
        organization_id: uuid.UUID | None = None,
    ) -> list[float]:
        """Durées des visites clôturées sur la période, pour la moyenne du dashboard.

        Le calcul de la différence de dates est fait en Python plutôt qu'en SQL :
        les fonctions d'intervalle diffèrent entre PostgreSQL et SQLite (base de test).
        """
        stmt = select(Visit.checked_in_at, Visit.checked_out_at).where(
            Visit.checked_out_at.is_not(None),
            Visit.checked_out_at >= start,
            Visit.checked_out_at < end,
            Visit.statut != VisitStatus.ANNULEE,
        )
        if organization_id is not None:
            stmt = stmt.where(Visit.organization_id == organization_id)
        rows = (await self.session.execute(stmt)).all()
        return [(out - inn).total_seconds() for inn, out in rows if out is not None]

    # --- Analytics du dashboard web ---

    async def checkin_checkout_timestamps(
        self, start: datetime, end: datetime, organization_id: uuid.UUID | None = None
    ) -> list[tuple[datetime, datetime | None]]:
        """Horodatages bruts de la période, pour les agrégats calculés en Python.

        Le découpage en tranches (jour/semaine/mois) et l'extraction de l'heure
        s'écrivent différemment en PostgreSQL (`date_trunc`, `EXTRACT`) et en
        SQLite (`strftime`). Plutôt que de maintenir deux dialectes — et de tester
        sur l'un en exécutant l'autre en production — on rapatrie les horodatages
        et on agrège en Python. Le volume s'y prête : un poste d'accueil produit
        quelques milliers de visites par an.
        """
        stmt = select(Visit.checked_in_at, Visit.checked_out_at).where(
            Visit.checked_in_at >= start,
            Visit.checked_in_at < end,
            Visit.statut != VisitStatus.ANNULEE,
        )
        if organization_id is not None:
            stmt = stmt.where(Visit.organization_id == organization_id)
        return [(inn, out) for inn, out in (await self.session.execute(stmt)).all()]

    async def checkout_timestamps(
        self,
        start: datetime,
        end: datetime,
        organization_id: uuid.UUID | None = None,
    ) -> list[datetime]:
        """Sorties de la période, pour la seconde série du graphe temporel."""
        stmt = select(Visit.checked_out_at).where(
            Visit.checked_out_at.is_not(None),
            Visit.checked_out_at >= start,
            Visit.checked_out_at < end,
            Visit.statut != VisitStatus.ANNULEE,
        )
        if organization_id is not None:
            stmt = stmt.where(Visit.organization_id == organization_id)
        return [row for row in (await self.session.execute(stmt)).scalars().all() if row]

    async def count_by_service(
        self,
        start: datetime,
        end: datetime,
        organization_id: uuid.UUID | None = None,
    ) -> list[tuple[Any, str, int]]:
        """`(service_id, nom, nombre)` sur la période, du plus fréquenté au moins."""
        stmt = (
            select(Service.id, Service.name, func.count(Visit.id))
            .join(Service, Visit.service_id == Service.id)
            .where(
                Visit.checked_in_at >= start,
                Visit.checked_in_at < end,
                Visit.statut != VisitStatus.ANNULEE,
            )
            .group_by(Service.id, Service.name)
            .order_by(func.count(Visit.id).desc(), Service.name)
        )
        if organization_id is not None:
            stmt = stmt.where(Visit.organization_id == organization_id)
        return [(sid, name, total) for sid, name, total in (await self.session.execute(stmt)).all()]

    async def count_by_purpose(
        self,
        start: datetime,
        end: datetime,
        organization_id: uuid.UUID | None = None,
    ) -> list[tuple[Any, str, int]]:
        """`(purpose_id, libellé, nombre)`.

        `outerjoin` : `purpose_id` est nullable — l'agent peut saisir un motif
        libre. Ces visites sont regroupées sous un libellé « non renseigné »
        plutôt que disparaître du camembert.
        """
        stmt = (
            select(Purpose.id, Purpose.libelle, func.count(Visit.id))
            .outerjoin(Purpose, Visit.purpose_id == Purpose.id)
            .where(
                Visit.checked_in_at >= start,
                Visit.checked_in_at < end,
                Visit.statut != VisitStatus.ANNULEE,
            )
            .group_by(Purpose.id, Purpose.libelle)
            .order_by(func.count(Visit.id).desc())
        )
        if organization_id is not None:
            stmt = stmt.where(Visit.organization_id == organization_id)
        return [(pid, lib, total) for pid, lib, total in (await self.session.execute(stmt)).all()]

    async def top_agents(
        self,
        start: datetime,
        end: datetime,
        *,
        limit: int,
        organization_id: uuid.UUID | None = None,
    ) -> list[tuple[Any, str, str | None, int]]:
        """`(agent_id, nom, service, nombre)` des personnes les plus visitées.

        `join` et non `outerjoin`, à la différence de `count_by_purpose` : les
        visites sans personne rencontrée (ADR-019) sont exclues du classement, ce
        qui est le propre d'un palmarès de personnes. Le total de ce classement est
        donc inférieur au nombre de visites de la période — c'est voulu, et le
        dashboard doit le présenter comme tel plutôt que comme une répartition.
        """
        stmt = (
            select(Agent.id, Agent.name, Service.name, func.count(Visit.id))
            .join(Agent, Visit.agent_id == Agent.id)
            .outerjoin(Service, Agent.service_id == Service.id)
            .where(
                Visit.checked_in_at >= start,
                Visit.checked_in_at < end,
                Visit.statut != VisitStatus.ANNULEE,
            )
            .group_by(Agent.id, Agent.name, Service.name)
            .order_by(func.count(Visit.id).desc(), Agent.name)
            .limit(limit)
        )
        if organization_id is not None:
            stmt = stmt.where(Visit.organization_id == organization_id)
        return [
            (aid, name, service, total)
            for aid, name, service, total in (await self.session.execute(stmt)).all()
        ]
