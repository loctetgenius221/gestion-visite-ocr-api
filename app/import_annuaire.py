"""Import de l'annuaire du ministère (services et agents) depuis un fichier CSV.

Usage :

    python -m app.import_annuaire annuaire.csv --dry-run   # montre le plan, n'écrit rien
    python -m app.import_annuaire annuaire.csv             # applique
    python -m app.import_annuaire annuaire.csv --mettre-a-jour

Colonnes attendues (l'ordre est libre, la casse et les accents indifférents) :

    Matricule, nom_et_prenoms, Nom, Téléphone, Email, Fonction, Direction, Département, Sexe

Seules `nom_et_prenoms`, `Nom`, `Fonction` et `Département` (à défaut `Direction`)
sont exploitées : le référentiel `agents` ne stocke qu'un nom, une fonction, un
bureau et un service de rattachement. Matricule, téléphone, e-mail et sexe n'ont
pas de colonne en base et sont ignorés — les importer supposerait une migration,
et le registre des visites ne s'en sert nulle part.

Le service de rattachement est déduit du libellé de département : son **code** est
l'acronyme des mots significatifs du libellé (« Direction des Systemes
D'informations » → `DSI`). Deux libellés qui produisent le même acronyme désignent
la même entité dans la pratique — « SG » et « Sécretariat Général » — et sont
fusionnés ; le rapport le signale toujours explicitement.

Le script est idempotent : un service est réutilisé si son code ou son nom existe
déjà en base, un agent est réutilisé si son nom existe déjà dans le même service.
Le relancer après un correctif du CSV ne crée pas de doublons. Rien n'est jamais
supprimé ni archivé : retirer une entrée de l'annuaire reste une action manuelle
d'administrateur, parce que les visites déjà enregistrées la référencent.

Lancez-le toujours une première fois avec `--dry-run` : la correspondance
libellé → service est une heuristique, et c'est le seul moment où elle se relit
facilement.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import SessionLocal, dispose_engine
from app.core.logging import configure_logging, get_logger
from app.models.referentiel import Agent, Service

logger = get_logger(__name__)

# Longueurs des colonnes correspondantes (`app/models/referentiel.py`). Un CSV
# mal formé ne doit pas faire échouer l'import sur un `DataError` PostgreSQL à la
# 200e ligne : on tronque, et le rapport liste ce qui l'a été.
CODE_MAX = 50
NOM_MAX = 200
FONCTION_MAX = 200

# Service d'accueil des lignes sans département renseigné. Les créer sans
# rattachement est impossible — `agents.service_id` est NOT NULL — et les écarter
# perdrait une trentaine de personnes bien réelles (stagiaires, prestataires).
CODE_SERVICE_DEFAUT = "SANS_SERVICE"
NOM_SERVICE_DEFAUT = "Sans service"

# Mots ignorés dans la construction des acronymes.
MOTS_VIDES = frozenset(
    {"a", "au", "aux", "d", "de", "des", "du", "en", "et", "l", "la", "le", "les", "pour", "sur"}
)

# Libellés de colonnes acceptés, une fois normalisés (sans accent, sans casse,
# séparateurs ramenés à l'espace).
COLONNES: dict[str, tuple[str, ...]] = {
    "prenoms": ("nom et prenoms", "prenoms", "prenom", "prenoms et nom"),
    "nom": ("nom", "nom de famille", "nom de famille "),
    "fonction": ("fonction", "poste"),
    "direction": ("direction",),
    "departement": ("departement", "service"),
}
COLONNES_REQUISES = ("prenoms", "nom", "fonction")


@dataclass(frozen=True)
class LigneAnnuaire:
    """Une personne, telle que lue dans le CSV."""

    numero: int
    nom: str
    fonction: str | None
    libelle_service: str | None


@dataclass
class ServicePlan:
    """Un service à créer ou à réutiliser, et les agents qui s'y rattachent."""

    code: str
    nom: str
    libelles: list[str] = field(default_factory=list)
    lignes: list[LigneAnnuaire] = field(default_factory=list)
    existant: Service | None = None


@dataclass
class Rapport:
    fichier: str = ""
    lignes_lues: int = 0
    services_crees: list[tuple[str, str, int]] = field(default_factory=list)
    services_reutilises: list[tuple[str, str, int]] = field(default_factory=list)
    services_archives: list[str] = field(default_factory=list)
    fusions: list[tuple[str, list[str]]] = field(default_factory=list)
    agents_crees: int = 0
    agents_existants: int = 0
    agents_mis_a_jour: int = 0
    doublons: list[tuple[int, str]] = field(default_factory=list)
    ignorees: list[tuple[int, str]] = field(default_factory=list)
    tronquees: list[tuple[int, str]] = field(default_factory=list)
    dry_run: bool = False


class ImportError_(Exception):
    """Erreur bloquante : fichier illisible ou colonnes manquantes."""


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

# Apostrophes typographiques : le CSV mélange U+2019 et U+0027 dans les mêmes
# libellés (« l'Administration » / « l’Équipement »).
APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'"})


def _sans_accents(texte: str) -> str:
    decompose = unicodedata.normalize("NFKD", texte.translate(APOSTROPHES))
    return "".join(c for c in decompose if not unicodedata.combining(c))


def normalise(texte: str) -> str:
    """Clé de comparaison : sans accent, sans casse, espaces normalisés."""
    return " ".join(_sans_accents(texte).casefold().split())


def _normalise_entete(texte: str) -> str:
    return " ".join(re.sub(r"[_\-./]+", " ", _sans_accents(texte)).casefold().split())


def code_service(libelle: str) -> str:
    """Acronyme des mots significatifs du libellé, en majuscules.

    « Direction de la Gestion des carrières » → `DGC`. Un libellé déjà sigle
    (« DRH », « SG ») est repris tel quel : un acronyme d'acronyme ne voudrait
    plus rien dire.
    """
    mots = [mot for mot in re.split(r"[^0-9A-Za-z]+", _sans_accents(libelle)) if mot]
    significatifs = [mot for mot in mots if mot.casefold() not in MOTS_VIDES] or mots
    if not significatifs:
        return "SERVICE"
    if len(significatifs) == 1:
        return significatifs[0].upper()[:CODE_MAX]
    return "".join(mot[0] for mot in significatifs).upper()[:CODE_MAX]


def _nom_complet(prenoms: str, nom: str) -> str:
    """« Prénoms NOM » : prénoms capitalisés, patronyme en majuscules.

    Le CSV mélange les deux conventions d'une ligne à l'autre (« Wade » et
    « WADE ») ; la liste déroulante servie à l'agent de contrôle, elle, doit être
    homogène pour rester lisible au premier coup d'œil.
    """
    morceaux = [
        re.sub(r"[^\W\d_]+", lambda m: m.group().capitalize(), prenoms.strip()),
        nom.strip().upper(),
    ]
    return " ".join(morceau for morceau in morceaux if morceau)


def _tronque(valeur: str, limite: int) -> tuple[str, bool]:
    return (valeur[:limite], True) if len(valeur) > limite else (valeur, False)


# --------------------------------------------------------------------------- #
# Lecture du CSV
# --------------------------------------------------------------------------- #


def _resout_colonnes(entetes: list[str]) -> dict[str, str]:
    """Associe chaque champ attendu au nom de colonne réellement présent."""
    presentes = {_normalise_entete(entete): entete for entete in entetes if entete}
    resolues: dict[str, str] = {}
    for champ, alias in COLONNES.items():
        for nom_alias in alias:
            if nom_alias in presentes:
                resolues[champ] = presentes[nom_alias]
                break

    manquantes = [champ for champ in COLONNES_REQUISES if champ not in resolues]
    if manquantes:
        raise ImportError_(
            "Colonnes introuvables dans le CSV : "
            + ", ".join(manquantes)
            + f". Colonnes lues : {', '.join(entetes)}."
        )
    if "departement" not in resolues and "direction" not in resolues:
        raise ImportError_("Le CSV doit contenir une colonne « Département » ou « Direction ».")
    return resolues


def lire_csv(
    chemin: Path, *, encoding: str = "utf-8-sig", delimiteur: str = ","
) -> tuple[list[LigneAnnuaire], Rapport]:
    """Lit le fichier et retourne les lignes exploitables, plus un rapport amorcé."""
    rapport = Rapport(fichier=str(chemin))
    lignes: list[LigneAnnuaire] = []

    try:
        contenu = chemin.read_text(encoding=encoding)
    except OSError as exc:
        raise ImportError_(f"Lecture impossible de {chemin} : {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ImportError_(
            f"{chemin} n'est pas encodé en {encoding} : {exc}. Précisez --encoding."
        ) from exc

    lecteur = csv.DictReader(contenu.splitlines(), delimiter=delimiteur)
    if not lecteur.fieldnames:
        raise ImportError_(f"{chemin} est vide ou sans ligne d'en-tête.")
    colonnes = _resout_colonnes(list(lecteur.fieldnames))

    def champ(rangee: dict[str, str | None], nom: str) -> str:
        colonne = colonnes.get(nom)
        return (rangee.get(colonne) or "").strip() if colonne else ""

    for rangee in lecteur:
        numero = lecteur.line_num
        rapport.lignes_lues += 1

        nom = _nom_complet(champ(rangee, "prenoms"), champ(rangee, "nom"))
        if not nom:
            rapport.ignorees.append((numero, "nom et prénoms absents"))
            continue
        nom, tronque = _tronque(nom, NOM_MAX)
        if tronque:
            rapport.tronquees.append((numero, "nom"))

        fonction: str | None = champ(rangee, "fonction") or None
        if fonction is not None:
            fonction, tronque = _tronque(fonction, FONCTION_MAX)
            if tronque:
                rapport.tronquees.append((numero, "fonction"))

        # Le département est l'affectation fine ; la direction ne sert que de
        # repli, elle n'est renseignée que sur une poignée de lignes.
        libelle = champ(rangee, "departement") or champ(rangee, "direction") or None

        lignes.append(LigneAnnuaire(numero, nom, fonction, libelle))

    return lignes, rapport


# --------------------------------------------------------------------------- #
# Construction du plan
# --------------------------------------------------------------------------- #


def construire_plans(
    lignes: list[LigneAnnuaire],
    rapport: Rapport,
    *,
    service_defaut: str = CODE_SERVICE_DEFAUT,
    ignorer_sans_service: bool = False,
) -> list[ServicePlan]:
    """Regroupe les lignes par service, un plan par code de service."""
    plans: dict[str, ServicePlan] = {}

    for ligne in lignes:
        if ligne.libelle_service is None:
            if ignorer_sans_service:
                rapport.ignorees.append((ligne.numero, "département absent"))
                continue
            code, libelle = service_defaut.upper()[:CODE_MAX], NOM_SERVICE_DEFAUT
        else:
            code, libelle = code_service(ligne.libelle_service), ligne.libelle_service

        plan = plans.get(code)
        if plan is None:
            plan = plans[code] = ServicePlan(code=code, nom=libelle)
        if libelle not in plan.libelles:
            plan.libelles.append(libelle)
        plan.lignes.append(ligne)

    for plan in plans.values():
        # Entre libellés fusionnés, le plus long est le plus parlant : « SG » et
        # « Sécretariat Général » désignent le même service, autant afficher le second.
        plan.nom = _tronque(max(plan.libelles, key=len), NOM_MAX)[0]
        if len(plan.libelles) > 1:
            rapport.fusions.append((plan.code, list(plan.libelles)))

    return list(plans.values())


# --------------------------------------------------------------------------- #
# Application en base
# --------------------------------------------------------------------------- #


def _code_libre(code: str, pris: set[str]) -> str:
    """Décline le code jusqu'à en trouver un disponible : DSI, DSI2, DSI3…"""
    if code.casefold() not in pris:
        return code
    for suffixe in range(2, 100):
        candidat = f"{code[: CODE_MAX - len(str(suffixe))]}{suffixe}"
        if candidat.casefold() not in pris:
            return candidat
    raise ImportError_(f"Impossible de dériver un code unique à partir de « {code} ».")


async def appliquer(
    session: AsyncSession,
    plans: list[ServicePlan],
    rapport: Rapport,
    *,
    mettre_a_jour: bool = False,
    dry_run: bool = False,
) -> Rapport:
    """Crée les services et agents manquants. N'écrit rien si `dry_run`."""
    rapport.dry_run = dry_run

    services_bd = list((await session.execute(select(Service))).scalars().all())
    par_code = {service.code.casefold(): service for service in services_bd}
    par_nom = {normalise(service.name): service for service in services_bd}
    codes_pris = set(par_code)
    revendiques: set[Service] = set()

    for plan in plans:
        existant = par_code.get(plan.code.casefold()) or par_nom.get(normalise(plan.nom))
        if existant is not None and existant not in revendiques:
            plan.existant = existant
            revendiques.add(existant)
            if existant.is_archived:
                rapport.services_archives.append(existant.code)
        else:
            # Soit le service est inconnu, soit un autre plan l'a déjà pris : on
            # en crée un distinct plutôt que d'y verser deux annuaires différents.
            plan.code = _code_libre(plan.code, codes_pris)
            codes_pris.add(plan.code.casefold())

    agents_bd = (await session.execute(select(Agent))).scalars().all()
    connus = {(agent.service_id, normalise(agent.name)): agent for agent in agents_bd}

    a_creer: list[tuple[ServicePlan, Agent]] = []

    for plan in plans:
        vus: set[str] = set()
        nouveaux = 0
        for ligne in plan.lignes:
            cle = normalise(ligne.nom)
            if cle in vus:
                rapport.doublons.append((ligne.numero, ligne.nom))
                continue
            vus.add(cle)

            deja_en_base = connus.get((plan.existant.id, cle)) if plan.existant else None
            if deja_en_base is not None:
                if mettre_a_jour and ligne.fonction and deja_en_base.role != ligne.fonction:
                    if not dry_run:
                        deja_en_base.role = ligne.fonction
                    rapport.agents_mis_a_jour += 1
                else:
                    rapport.agents_existants += 1
                continue

            a_creer.append((plan, Agent(name=ligne.nom, role=ligne.fonction)))
            nouveaux += 1
            rapport.agents_crees += 1

        cible = rapport.services_reutilises if plan.existant else rapport.services_crees
        cible.append((plan.code, plan.nom, nouveaux))

    if dry_run:
        return rapport

    for plan in plans:
        if plan.existant is None:
            plan.existant = Service(code=plan.code, name=plan.nom)
            session.add(plan.existant)
    # Les services doivent porter leur identifiant avant que les agents ne s'y
    # rattachent : `agents.service_id` est NOT NULL.
    await session.flush()

    for plan, agent in a_creer:
        assert plan.existant is not None
        agent.service_id = plan.existant.id
        session.add(agent)

    await session.commit()
    return rapport


async def importer(
    chemin: Path,
    *,
    encoding: str = "utf-8-sig",
    delimiteur: str = ",",
    service_defaut: str = CODE_SERVICE_DEFAUT,
    ignorer_sans_service: bool = False,
    mettre_a_jour: bool = False,
    dry_run: bool = False,
) -> Rapport:
    """Importe l'annuaire dans la base configurée par `DATABASE_URL`."""
    lignes, rapport = lire_csv(chemin, encoding=encoding, delimiteur=delimiteur)
    plans = construire_plans(
        lignes,
        rapport,
        service_defaut=service_defaut,
        ignorer_sans_service=ignorer_sans_service,
    )
    async with SessionLocal() as session:
        return await appliquer(
            session, plans, rapport, mettre_a_jour=mettre_a_jour, dry_run=dry_run
        )


# --------------------------------------------------------------------------- #
# Ligne de commande
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.import_annuaire",
        description=(
            "Remplit les référentiels `services` et `agents` depuis l'annuaire du "
            "ministère au format CSV, dans la base configurée par DATABASE_URL."
        ),
    )
    parser.add_argument("csv", type=Path, help="Chemin du fichier CSV de l'annuaire.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche ce qui serait créé, sans rien écrire en base.",
    )
    parser.add_argument(
        "--mettre-a-jour",
        dest="mettre_a_jour",
        action="store_true",
        help=(
            "Met à jour la fonction des agents déjà présents. Par défaut, une "
            "fiche existante n'est jamais modifiée : les corrections faites "
            "depuis le dashboard priment sur le CSV."
        ),
    )
    parser.add_argument(
        "--service-defaut",
        dest="service_defaut",
        default=CODE_SERVICE_DEFAUT,
        help=(
            "Code du service accueillant les lignes sans département "
            f"(défaut : {CODE_SERVICE_DEFAUT}, créé au besoin)."
        ),
    )
    parser.add_argument(
        "--ignorer-sans-service",
        dest="ignorer_sans_service",
        action="store_true",
        help="Ignore les lignes sans département au lieu de les rattacher au service par défaut.",
    )
    parser.add_argument(
        "--encoding", default="utf-8-sig", help="Encodage du CSV (défaut : utf-8-sig)."
    )
    parser.add_argument(
        "--delimiteur", default=",", help="Séparateur de colonnes (défaut : la virgule)."
    )
    return parser


def _affiche(rapport: Rapport) -> None:
    total_services = len(rapport.services_crees) + len(rapport.services_reutilises)
    print(f"Import de l'annuaire sur {settings.ENVIRONMENT}")
    print(f"  fichier            : {rapport.fichier}")
    print(f"  lignes lues        : {rapport.lignes_lues}")
    print(f"  services           : {total_services} ({len(rapport.services_crees)} à créer)")
    print(f"  agents créés       : {rapport.agents_crees}")
    print(f"  agents déjà en base: {rapport.agents_existants}")
    if rapport.agents_mis_a_jour:
        print(f"  fonctions mises à jour : {rapport.agents_mis_a_jour}")

    for titre, services in (
        ("Services créés", rapport.services_crees),
        ("Services réutilisés", rapport.services_reutilises),
    ):
        if not services:
            continue
        print()
        print(f"{titre} :")
        for code, nom, nouveaux in sorted(services):
            print(f"  {code:<14} {nom[:60]:<62} {nouveaux} agent(s)")

    if rapport.fusions:
        print()
        print("Libellés fusionnés — un seul service pour plusieurs intitulés du CSV :")
        for code, libelles in sorted(rapport.fusions):
            print(f"  {code:<14} {' | '.join(libelles)}")

    if rapport.services_archives:
        print()
        print("Services rattachés mais archivés — à désarchiver pour qu'ils réapparaissent :")
        print(f"  {', '.join(sorted(rapport.services_archives))}")

    if rapport.doublons:
        print()
        print(f"Doublons du CSV, une seule fiche créée ({len(rapport.doublons)}) :")
        for numero, nom in rapport.doublons[:20]:
            print(f"  ligne {numero} : {nom}")

    if rapport.tronquees:
        print()
        print(f"Valeurs tronquées à la longueur de colonne ({len(rapport.tronquees)}) :")
        for numero, champ in rapport.tronquees[:20]:
            print(f"  ligne {numero} : {champ}")

    if rapport.ignorees:
        print()
        print(f"Lignes ignorées ({len(rapport.ignorees)}) :")
        for numero, raison in rapport.ignorees[:20]:
            print(f"  ligne {numero} : {raison}")

    if rapport.dry_run:
        print()
        print("Simulation : rien n'a été écrit. Relancez sans --dry-run pour appliquer.")


async def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _parser().parse_args(argv)

    try:
        rapport = await importer(
            args.csv,
            encoding=args.encoding,
            delimiteur=args.delimiteur,
            service_defaut=args.service_defaut,
            ignorer_sans_service=args.ignorer_sans_service,
            mettre_a_jour=args.mettre_a_jour,
            dry_run=args.dry_run,
        )
    except ImportError_ as exc:
        print(f"Échec : {exc}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()

    _affiche(rapport)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
