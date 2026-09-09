"""Import en masse des référentiels : services et agents, depuis un classeur.

Deux fichiers distincts, et non un seul dont on déduirait les services. Le script
historique (`app/import_annuaire.py`) fait cette déduction, par acronyme du
libellé de département — et sa propre docstring impose un `--dry-run` relu à
l'œil, parce qu'une heuristique se trompe. Pour un import piloté depuis le
dashboard, l'administrateur dit ce qu'il veut : il remplit le modèle « Services »,
puis le modèle « Agents » qui référence les codes qu'il vient de créer.

Trois principes gouvernent ce module :

- **Tout ou rien.** Le plan est calculé en entier avant la moindre écriture, et
  une seule ligne en erreur suffit à tout refuser. C'est l'inverse de
  `sync_visits`, qui commite item par item — et la différence est justifiée :
  là-bas, les visites n'existent que sur la tablette et ne peuvent pas être
  renvoyées ; ici, un classeur se corrige et se retéléverse.
- **Rien n'est jamais supprimé ni archivé.** Un fichier partiel ferait
  disparaître la moitié de l'annuaire. Créer, éventuellement mettre à jour, et
  s'arrêter là.
- **Le plan et le résultat ont la même forme.** Ce que la prévisualisation
  annonce est exactement ce que l'application fait, sans quoi la confirmation de
  l'administrateur ne porterait sur rien.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ImportPlanRejectedError, OrganizationNotFoundError
from app.models.organization import Organization
from app.models.referentiel import Agent, Service
from app.models.user import User
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.referentiel_repository import AgentRepository, ServiceRepository
from app.schemas.referentiel_import import (
    AgentImportRow,
    ImportItem,
    ImportReport,
    ServiceImportRow,
)
from app.services.audit_service import AuditAction, AuditService
from app.services.referentiel_admin_service import organization_for_write
from app.services.setting_service import SettingService
from app.services.spreadsheet import (
    Colonne,
    construire_modele,
    lire_lignes,
    normalise,
    verifier_classeur,
)

# Longueurs des colonnes correspondantes (`app/models/referentiel.py`). Une
# valeur trop longue est refusée **sur sa ligne** plutôt que tronquée en silence :
# contrairement au script CLI, dont le rapport se relit dans un terminal, ici
# l'administrateur peut corriger la cellule et relancer.
CODE_MAX = 50
NOM_MAX = 200
ETAGE_MAX = 50
FONCTION_MAX = 200
BUREAU_MAX = 100


COLONNES_SERVICES: tuple[Colonne, ...] = (
    Colonne(
        cle="code",
        libelle="Code",
        obligatoire=True,
        largeur=18,
        alias=("code service", "sigle", "abreviation"),
        description=(
            f"Identifiant court, unique dans l'organisation ({CODE_MAX} caractères max). "
            "C'est lui que le fichier des agents référencera."
        ),
        exemple="DRH",
    ),
    Colonne(
        cle="nom",
        libelle="Nom",
        obligatoire=True,
        largeur=44,
        alias=("libelle", "intitule", "designation", "nom du service"),
        description=f"Libellé affiché aux agents d'accueil ({NOM_MAX} caractères max).",
        exemple="Direction des Ressources Humaines",
    ),
    Colonne(
        cle="etage",
        libelle="Étage",
        obligatoire=False,
        largeur=16,
        alias=("niveau", "floor"),
        description="Facultatif. Aide le visiteur à s'orienter.",
        exemple="2e étage",
    ),
    Colonne(
        cle="code_parent",
        libelle="Code parent",
        obligatoire=False,
        largeur=18,
        alias=("parent", "code du parent", "service parent", "rattachement"),
        description=(
            "Facultatif. Code du service dont celui-ci dépend. Il peut désigner un "
            "service déjà en base ou un service créé plus bas dans ce même fichier — "
            "l'ordre des lignes est sans importance. Laissez vide pour un service "
            "de premier niveau."
        ),
        exemple="SG",
    ),
)

COLONNES_AGENTS: tuple[Colonne, ...] = (
    Colonne(
        cle="nom",
        libelle="Nom",
        obligatoire=True,
        largeur=36,
        alias=("nom complet", "nom et prenoms", "prenoms et nom", "agent"),
        description=(
            f"Nom complet de la personne ({NOM_MAX} caractères max). Deux personnes "
            "de même nom dans le même service ne peuvent pas coexister."
        ),
        exemple="Aminata DIALLO",
    ),
    Colonne(
        cle="code_service",
        libelle="Code service",
        obligatoire=True,
        largeur=18,
        alias=("service", "code du service", "direction", "departement"),
        description=(
            "Code d'un service **déjà existant** dans l'organisation. Importez "
            "d'abord le fichier des services si besoin."
        ),
        exemple="DRH",
    ),
    Colonne(
        cle="fonction",
        libelle="Fonction",
        obligatoire=False,
        largeur=36,
        alias=("poste", "role", "titre"),
        description=f"Facultatif ({FONCTION_MAX} caractères max).",
        exemple="Directrice des Ressources Humaines",
    ),
    Colonne(
        cle="bureau",
        libelle="Bureau",
        obligatoire=False,
        largeur=16,
        alias=("office", "numero de bureau", "local"),
        description=f"Facultatif ({BUREAU_MAX} caractères max).",
        exemple="B201",
    ),
)

INSTRUCTIONS_SERVICES = [
    "1. Remplissez une ligne par service, à partir de la ligne 2. Ne modifiez pas "
    "la ligne d'en-tête : c'est elle qui identifie les colonnes.",
    "2. L'ordre des lignes est libre, y compris pour les rattachements parent/enfant.",
    "3. Enregistrez au format .xlsx, puis téléversez le fichier depuis le dashboard.",
    "4. L'organisation destinataire se choisit au moment du téléversement, pas dans "
    "le fichier : un même modèle sert donc à toutes les structures.",
    "5. Un service déjà présent (même code) n'est pas dupliqué. Il n'est modifié que "
    "si vous cochez « mettre à jour les entrées existantes ».",
    "6. Rien n'est jamais supprimé ni archivé par un import : un service absent du "
    "fichier reste en place.",
]

INSTRUCTIONS_AGENTS = [
    "1. Remplissez une ligne par personne, à partir de la ligne 2. Ne modifiez pas "
    "la ligne d'en-tête : c'est elle qui identifie les colonnes.",
    "2. La colonne « Code service » doit reprendre le code d'un service existant. "
    "Importez le modèle « Services » d'abord si nécessaire.",
    "3. Enregistrez au format .xlsx, puis téléversez le fichier depuis le dashboard.",
    "4. L'organisation destinataire se choisit au moment du téléversement, pas dans "
    "le fichier.",
    "5. Une personne déjà présente dans le même service n'est pas dupliquée. Elle "
    "n'est modifiée que si vous cochez « mettre à jour les entrées existantes ».",
    "6. Rien n'est jamais supprimé ni archivé par un import.",
]


@dataclass(slots=True)
class _Contexte:
    """Ce que le planificateur doit connaître avant de juger la première ligne."""

    organization: Organization
    mettre_a_jour: bool


def _erreur(
    ligne: ServiceImportRow | AgentImportRow, code: str, message: str
) -> ImportItem:
    return ImportItem(
        ligne=ligne.ligne, statut="error", donnees=ligne, error_code=code, message=message
    )


def _trop_long(valeur: str | None, limite: int) -> bool:
    return valeur is not None and len(valeur) > limite


def _diff(avant: dict[str, object], apres: dict[str, object]) -> dict[str, object]:
    """Champs réellement modifiés. Un `None` entrant ne vaut pas effacement.

    Une cellule laissée vide dans le classeur signifie « je ne renseigne pas »,
    pas « efface ce qui est en base » : l'administrateur qui ne remplit que la
    colonne Fonction ne doit pas vider les bureaux au passage.
    """
    return {
        champ: {"avant": avant[champ], "après": valeur}
        for champ, valeur in apres.items()
        if valeur is not None and valeur != avant[champ]
    }


class ReferentielImportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.services = ServiceRepository(session)
        self.agents = AgentRepository(session)
        self.organizations = OrganizationRepository(session)
        self.audit = AuditService(session)

    # --- Modèles téléchargeables ---------------------------------------------

    @staticmethod
    def modele_services() -> bytes:
        return construire_modele("Services", COLONNES_SERVICES, INSTRUCTIONS_SERVICES)

    @staticmethod
    def modele_agents() -> bytes:
        return construire_modele("Agents", COLONNES_AGENTS, INSTRUCTIONS_AGENTS)

    # --- Lecture d'un classeur -----------------------------------------------

    async def lire_services(
        self, contenu: bytes, *, filename: str | None
    ) -> list[ServiceImportRow]:
        lignes = await self._lire(contenu, COLONNES_SERVICES, filename)
        return [
            ServiceImportRow(
                ligne=int(brut["__ligne__"]),
                code=brut["code"],
                nom=brut["nom"],
                etage=brut["etage"] or None,
                code_parent=brut["code_parent"] or None,
            )
            for brut in lignes
        ]

    async def lire_agents(
        self, contenu: bytes, *, filename: str | None
    ) -> list[AgentImportRow]:
        lignes = await self._lire(contenu, COLONNES_AGENTS, filename)
        return [
            AgentImportRow(
                ligne=int(brut["__ligne__"]),
                nom=brut["nom"],
                code_service=brut["code_service"],
                fonction=brut["fonction"] or None,
                bureau=brut["bureau"] or None,
            )
            for brut in lignes
        ]

    async def _lire(
        self, contenu: bytes, colonnes: tuple[Colonne, ...], filename: str | None
    ) -> list[dict[str, str]]:
        # Le contrôle du contenant est fait ici plutôt que dans le router : deux
        # routes lisent des classeurs, et l'oubli sur l'une d'elles ne se verrait pas.
        verifier_classeur(filename, contenu)
        parametres = await SettingService(self.session).get_settings()
        lignes, _ = lire_lignes(
            contenu, colonnes, max_lignes=parametres.referentiel_import_max_rows
        )
        return lignes

    # --- Services ------------------------------------------------------------

    async def planifier_services(
        self,
        lignes: list[ServiceImportRow],
        actor: User,
        *,
        organization_id: uuid.UUID | None,
        mettre_a_jour: bool,
    ) -> ImportReport[ServiceImportRow]:
        contexte = await self._contexte(organization_id, actor, mettre_a_jour)
        existants = await self.services.list_all(
            organization_id=contexte.organization.id, include_archived=True
        )
        par_code = {normalise(s.code): s for s in existants}

        items: list[ImportItem[ServiceImportRow]] = []
        vus: dict[str, int | None] = {}
        for ligne in lignes:
            item = self._valider_service(ligne, par_code, vus, contexte)
            if item.statut != "error":
                vus[normalise(ligne.code)] = ligne.ligne
            items.append(item)

        _resoudre_parents(items, par_code, mettre_a_jour=contexte.mettre_a_jour)
        return self._rapport(contexte, items, applique=False)

    def _valider_service(
        self,
        ligne: ServiceImportRow,
        par_code: dict[str, Service],
        vus: dict[str, int | None],
        contexte: _Contexte,
    ) -> ImportItem[ServiceImportRow]:
        if not ligne.code:
            return _erreur(ligne, "IMPORT_FIELD_REQUIRED", "Le code du service est obligatoire.")
        if not ligne.nom:
            return _erreur(ligne, "IMPORT_FIELD_REQUIRED", "Le nom du service est obligatoire.")
        for valeur, limite, champ in (
            (ligne.code, CODE_MAX, "Code"),
            (ligne.nom, NOM_MAX, "Nom"),
            (ligne.etage, ETAGE_MAX, "Étage"),
            (ligne.code_parent, CODE_MAX, "Code parent"),
        ):
            if _trop_long(valeur, limite):
                return _erreur(
                    ligne,
                    "IMPORT_VALUE_TOO_LONG",
                    f"« {champ} » dépasse {limite} caractères.",
                )

        cle = normalise(ligne.code)
        if cle in vus:
            return _erreur(
                ligne,
                "IMPORT_DUPLICATE_IN_FILE",
                f"Le code « {ligne.code} » apparaît déjà ligne {vus[cle]} du fichier.",
            )

        existant = par_code.get(cle)
        if existant is None:
            return ImportItem(ligne=ligne.ligne, statut="create", donnees=ligne)

        changements = _diff(
            {"name": existant.name, "floor": existant.floor},
            {"name": ligne.nom, "floor": ligne.etage},
        )
        if contexte.mettre_a_jour and changements:
            return ImportItem(
                ligne=ligne.ligne,
                statut="update",
                donnees=ligne,
                entity_id=existant.id,
                changements=changements,
            )
        return ImportItem(
            ligne=ligne.ligne, statut="unchanged", donnees=ligne, entity_id=existant.id
        )

    async def importer_services(
        self,
        lignes: list[ServiceImportRow],
        actor: User,
        *,
        organization_id: uuid.UUID | None,
        mettre_a_jour: bool,
    ) -> ImportReport[ServiceImportRow]:
        plan = await self.planifier_services(
            lignes, actor, organization_id=organization_id, mettre_a_jour=mettre_a_jour
        )
        _refuser_si_erreurs(plan)

        organization_id_resolue = plan.organization_id
        existants = await self.services.list_all(
            organization_id=organization_id_resolue, include_archived=True
        )
        par_code: dict[str, Service] = {normalise(s.code): s for s in existants}

        # Insertion par vagues : un service n'est créé qu'une fois son parent
        # connu en base, `services.parent_id` référençant une ligne réelle.
        #
        # Les lignes `unchanged` ne sont pas touchées, rattachement compris : c'est
        # ce que « inchangé » veut dire, et le plan que l'administrateur a validé
        # ne promettait rien d'autre. Un déplacement de service demande donc la
        # case « mettre à jour », qui le fait apparaître comme tel dans le plan.
        for item in _par_vagues(plan.items, par_code):
            donnees = item.donnees
            cle = normalise(donnees.code)
            parent = par_code.get(normalise(donnees.code_parent)) if donnees.code_parent else None

            if item.statut == "create":
                service = await self.services.add(
                    Service(
                        code=donnees.code,
                        name=donnees.nom,
                        floor=donnees.etage,
                        parent_id=parent.id if parent else None,
                        organization_id=organization_id_resolue,
                    )
                )
                par_code[cle] = service
                item.entity_id = service.id
                await self.audit.record(
                    AuditAction.SERVICE_CREATED,
                    entity="service",
                    entity_id=service.id,
                    actor=actor,
                    organization_id=organization_id_resolue,
                    metadata={"code": service.code, "name": service.name, "source": "import"},
                )
            elif item.statut == "update":
                service = par_code[cle]
                service.name = donnees.nom
                if donnees.etage is not None:
                    service.floor = donnees.etage
                if parent is not None:
                    service.parent_id = parent.id
                await self.audit.record(
                    AuditAction.SERVICE_UPDATED,
                    entity="service",
                    entity_id=service.id,
                    actor=actor,
                    organization_id=organization_id_resolue,
                    metadata={"changements": item.changements, "source": "import"},
                )

        await self._journaliser_import(
            "service", plan, actor, organization_id=organization_id_resolue
        )
        await self.session.commit()
        return plan.model_copy(update={"applique": True})

    # --- Agents --------------------------------------------------------------

    async def planifier_agents(
        self,
        lignes: list[AgentImportRow],
        actor: User,
        *,
        organization_id: uuid.UUID | None,
        mettre_a_jour: bool,
    ) -> ImportReport[AgentImportRow]:
        contexte = await self._contexte(organization_id, actor, mettre_a_jour)
        services = await self.services.list_all(
            organization_id=contexte.organization.id, include_archived=True
        )
        par_code = {normalise(s.code): s for s in services}
        existants = await self.agents.list_all(
            organization_id=contexte.organization.id, include_archived=True
        )
        connus = {(a.service_id, normalise(a.name)): a for a in existants}

        items: list[ImportItem[AgentImportRow]] = []
        vus: dict[tuple[str, str], int | None] = {}
        for ligne in lignes:
            item = self._valider_agent(ligne, par_code, connus, vus, contexte)
            if item.statut != "error":
                vus[(normalise(ligne.code_service), normalise(ligne.nom))] = ligne.ligne
            items.append(item)

        return self._rapport(contexte, items, applique=False)

    def _valider_agent(
        self,
        ligne: AgentImportRow,
        par_code: dict[str, Service],
        connus: dict[tuple[uuid.UUID, str], Agent],
        vus: dict[tuple[str, str], int | None],
        contexte: _Contexte,
    ) -> ImportItem[AgentImportRow]:
        if not ligne.nom:
            return _erreur(ligne, "IMPORT_FIELD_REQUIRED", "Le nom est obligatoire.")
        if not ligne.code_service:
            return _erreur(
                ligne, "IMPORT_FIELD_REQUIRED", "Le code du service est obligatoire."
            )
        for valeur, limite, champ in (
            (ligne.nom, NOM_MAX, "Nom"),
            (ligne.code_service, CODE_MAX, "Code service"),
            (ligne.fonction, FONCTION_MAX, "Fonction"),
            (ligne.bureau, BUREAU_MAX, "Bureau"),
        ):
            if _trop_long(valeur, limite):
                return _erreur(
                    ligne,
                    "IMPORT_VALUE_TOO_LONG",
                    f"« {champ} » dépasse {limite} caractères.",
                )

        service = par_code.get(normalise(ligne.code_service))
        if service is None:
            return _erreur(
                ligne,
                "SERVICE_NOT_FOUND",
                f"Aucun service ne porte le code « {ligne.code_service} » dans cette "
                "organisation. Importez d'abord le fichier des services.",
            )
        if service.is_archived:
            # Même règle que `create_agent` : rattacher à un service archivé
            # rendrait la personne sélectionnable sans service visible côté mobile.
            return _erreur(
                ligne,
                "ARCHIVED_REFERENTIEL",
                f"Le service « {service.code} » est archivé : désarchivez-le d'abord.",
            )

        cle = (normalise(ligne.code_service), normalise(ligne.nom))
        if cle in vus:
            return _erreur(
                ligne,
                "IMPORT_DUPLICATE_IN_FILE",
                f"« {ligne.nom} » apparaît déjà ligne {vus[cle]} pour ce service.",
            )

        existant = connus.get((service.id, normalise(ligne.nom)))
        if existant is None:
            return ImportItem(ligne=ligne.ligne, statut="create", donnees=ligne)

        changements = _diff(
            {"role": existant.role, "office": existant.office},
            {"role": ligne.fonction, "office": ligne.bureau},
        )
        if contexte.mettre_a_jour and changements:
            return ImportItem(
                ligne=ligne.ligne,
                statut="update",
                donnees=ligne,
                entity_id=existant.id,
                changements=changements,
            )
        return ImportItem(
            ligne=ligne.ligne, statut="unchanged", donnees=ligne, entity_id=existant.id
        )

    async def importer_agents(
        self,
        lignes: list[AgentImportRow],
        actor: User,
        *,
        organization_id: uuid.UUID | None,
        mettre_a_jour: bool,
    ) -> ImportReport[AgentImportRow]:
        plan = await self.planifier_agents(
            lignes, actor, organization_id=organization_id, mettre_a_jour=mettre_a_jour
        )
        _refuser_si_erreurs(plan)

        services = await self.services.list_all(
            organization_id=plan.organization_id, include_archived=True
        )
        par_code = {normalise(s.code): s for s in services}
        existants = await self.agents.list_all(
            organization_id=plan.organization_id, include_archived=True
        )
        connus = {(a.service_id, normalise(a.name)): a for a in existants}

        for item in plan.items:
            donnees = item.donnees
            service = par_code[normalise(donnees.code_service)]

            if item.statut == "create":
                agent = await self.agents.add(
                    Agent(
                        name=donnees.nom,
                        role=donnees.fonction,
                        office=donnees.bureau,
                        service_id=service.id,
                    )
                )
                item.entity_id = agent.id
                await self.audit.record(
                    AuditAction.AGENT_CREATED,
                    entity="agent",
                    entity_id=agent.id,
                    actor=actor,
                    organization_id=plan.organization_id,
                    metadata={
                        "name": agent.name,
                        "service_id": str(service.id),
                        "source": "import",
                    },
                )
            elif item.statut == "update":
                agent = connus[(service.id, normalise(donnees.nom))]
                if donnees.fonction is not None:
                    agent.role = donnees.fonction
                if donnees.bureau is not None:
                    agent.office = donnees.bureau
                await self.audit.record(
                    AuditAction.AGENT_UPDATED,
                    entity="agent",
                    entity_id=agent.id,
                    actor=actor,
                    organization_id=plan.organization_id,
                    metadata={"changements": item.changements, "source": "import"},
                )

        await self._journaliser_import(
            "agent", plan, actor, organization_id=plan.organization_id
        )
        await self.session.commit()
        return plan.model_copy(update={"applique": True})

    # --- Commun --------------------------------------------------------------

    async def _contexte(
        self, organization_id: uuid.UUID | None, actor: User, mettre_a_jour: bool
    ) -> _Contexte:
        """Résout et **charge** l'organisation visée.

        La charger n'est pas superflu : le rapport porte son nom, pour que le
        dashboard écrive « Import dans : Ministère de l'Intérieur » au-dessus du
        bouton de confirmation. Un import massif dans la mauvaise structure est
        exactement le genre d'erreur qu'on ne veut pas découvrir après coup.
        """
        resolue = await organization_for_write(self.session, organization_id, actor)
        assert resolue is not None  # `obligatoire=True` : la fonction a déjà levé sinon.
        organization = await self.organizations.get_by_id(resolue)
        if organization is None:
            raise OrganizationNotFoundError(details={"organization_id": str(resolue)})
        return _Contexte(organization=organization, mettre_a_jour=mettre_a_jour)

    @staticmethod
    def _rapport(
        contexte: _Contexte, items: list[ImportItem], *, applique: bool
    ) -> ImportReport:
        def compte(statut: str) -> int:
            return sum(1 for item in items if item.statut == statut)

        return ImportReport(
            applique=applique,
            organization_id=contexte.organization.id,
            organization_nom=contexte.organization.nom,
            mettre_a_jour=contexte.mettre_a_jour,
            total=len(items),
            a_creer=compte("create"),
            a_mettre_a_jour=compte("update"),
            inchangees=compte("unchanged"),
            erreurs=compte("error"),
            items=items,
        )

    async def _journaliser_import(
        self,
        entite: str,
        plan: ImportReport,
        actor: User,
        *,
        organization_id: uuid.UUID,
    ) -> None:
        """Une trace de synthèse, en plus des traces par entité.

        Les deux servent, et ne se remplacent pas : la trace par entité répond à
        « qui a créé cet agent ? » (`GET /audit-logs?entity=agent&entity_id=…`), la
        trace de synthèse répond à « que s'est-il passé ce jour-là ? » sans avoir à
        recoller quatre cents lignes.
        """
        await self.audit.record(
            AuditAction.REFERENTIEL_IMPORTED,
            entity=entite,
            actor=actor,
            organization_id=organization_id,
            metadata={
                "type": entite,
                "total": plan.total,
                "crees": plan.a_creer,
                "mis_a_jour": plan.a_mettre_a_jour,
                "inchanges": plan.inchangees,
                "mise_a_jour_activee": plan.mettre_a_jour,
            },
        )


def _resoudre_parents(
    items: list[ImportItem[ServiceImportRow]],
    par_code: dict[str, Service],
    *,
    mettre_a_jour: bool,
) -> None:
    """Valide les rattachements, sur le graphe du **lot fusionné à la base**.

    Une ligne peut désigner comme parent un service déjà présent, ou un service
    qu'une autre ligne du même fichier va créer. `_verifier_parent`, côté création
    unitaire, ne sait faire que le premier cas — il interroge la base, où le
    second n'existe pas encore.

    Le graphe part donc de **tous** les services de l'organisation, puis les
    lignes du fichier écrasent les rattachements qu'elles redéfinissent. Le
    construire à partir des seules lignes du fichier laisserait passer un cycle
    qui se referme par la base : `A → B` dans le fichier, `B → A` déjà en place.

    Le cycle n'est pas une coquetterie : `_build_tree` reconstruit la hiérarchie à
    chaque `GET /services`, et une boucle y tournerait sans fin.
    """
    code_par_id = {service.id: normalise(service.code) for service in par_code.values()}
    parents: dict[str, str | None] = {
        normalise(service.code): code_par_id.get(service.parent_id)
        if service.parent_id
        else None
        for service in par_code.values()
    }
    # Codes qu'un rattachement peut viser : la base, plus ce que le lot va créer.
    disponibles = set(parents)
    for item in items:
        if item.statut != "error":
            disponibles.add(normalise(item.donnees.code))
    for item in items:
        if item.statut != "error" and item.donnees.code_parent:
            parents[normalise(item.donnees.code)] = normalise(item.donnees.code_parent)

    for item in items:
        if item.statut == "error" or not item.donnees.code_parent:
            continue
        cle = normalise(item.donnees.code)
        cle_parent = normalise(item.donnees.code_parent)

        if cle_parent not in disponibles:
            _marquer_erreur(
                item,
                "IMPORT_PARENT_UNKNOWN",
                f"Le service parent « {item.donnees.code_parent} » est introuvable : "
                "il doit exister dans l'organisation ou figurer dans ce fichier.",
            )
            continue
        if cle_parent == cle:
            _marquer_erreur(
                item, "SERVICE_HIERARCHY_CYCLE", "Un service ne peut pas être son propre parent."
            )
            continue
        if _forme_un_cycle(cle, parents):
            _marquer_erreur(
                item,
                "SERVICE_HIERARCHY_CYCLE",
                "Ce rattachement créerait un cycle dans la hiérarchie des services.",
            )
            continue

        # Un rattachement qui change est une modification à part entière : sans ce
        # rattrapage, une ligne qui ne touche qu'au parent resterait « inchangée »
        # et le déplacement demandé ne serait jamais appliqué.
        existant = par_code.get(cle)
        if existant is None or item.statut != "unchanged":
            continue
        parent_actuel = code_par_id.get(existant.parent_id) if existant.parent_id else None
        if parent_actuel == cle_parent:
            continue
        if mettre_a_jour:
            item.statut = "update"
            item.changements = {
                "parent": {"avant": parent_actuel, "après": cle_parent}
            }


def _marquer_erreur(item: ImportItem, code: str, message: str) -> None:
    item.statut = "error"
    item.entity_id = None
    item.changements = None
    item.error_code = code
    item.message = message


def _refuser_si_erreurs(plan: ImportReport) -> None:
    if not plan.erreurs:
        return
    raise ImportPlanRejectedError(
        details={
            "erreurs": plan.erreurs,
            "total": plan.total,
            "lignes": [
                {"ligne": item.ligne, "error_code": item.error_code, "message": item.message}
                for item in plan.items
                if item.statut == "error"
            ][:50],
        }
    )


def _forme_un_cycle(depart: str, parents: dict[str, str | None]) -> bool:
    """Remonte la chaîne des parents et signale un retour au point de départ."""
    vus: set[str] = set()
    courant = parents.get(depart)
    while courant is not None:
        if courant == depart:
            return True
        if courant in vus:
            # Cycle qui n'englobe pas `depart` : il sera signalé sur ses propres
            # lignes, inutile de refuser celle-ci deux fois.
            return False
        vus.add(courant)
        courant = parents.get(courant)
    return False


def _par_vagues(
    items: list[ImportItem[ServiceImportRow]], par_code: dict[str, Service]
) -> Iterable[ImportItem[ServiceImportRow]]:
    """Ordonne les lignes pour qu'un parent soit toujours traité avant son enfant.

    L'administrateur remplit son classeur dans l'ordre qui lui vient ; la base,
    elle, exige que `parent_id` désigne une ligne existante. Le tri est fait ici
    plutôt qu'imposé à l'utilisateur.

    Les cycles ayant déjà été refusés par `_resoudre_parents`, la boucle termine.
    Le repli final n'en est pas moins conservé : il vaut mieux rendre les lignes
    restantes dans leur ordre d'origine que boucler indéfiniment si une évolution
    future laissait passer un cycle.
    """
    en_attente = list(items)
    disponibles = set(par_code)
    for item in items:
        if item.statut in ("update", "unchanged"):
            disponibles.add(normalise(item.donnees.code))

    while en_attente:
        prets = [
            item
            for item in en_attente
            if not item.donnees.code_parent
            or normalise(item.donnees.code_parent) in disponibles
        ]
        if not prets:
            yield from en_attente
            return
        for item in prets:
            disponibles.add(normalise(item.donnees.code))
            yield item
        restants = set(map(id, prets))
        en_attente = [item for item in en_attente if id(item) not in restants]
