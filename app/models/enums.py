from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    """Rôles applicatifs.

    Deux rôles portent des droits :

    - `AGENT_CONTROLE` — application mobile, poste de contrôle ;
    - `ADMIN` — dashboard web, accès global à l'API.

    `SUPERVISEUR` est conservé sans droit particulier : il se comporte comme un
    agent de contrôle. Le retirer imposerait une migration du type ENUM et une
    élévation ou une rétrogradation des comptes existants, pour aucun gain
    fonctionnel. Il servira d'ancrage si un rôle intermédiaire scopé par service
    devient nécessaire.

    Les valeurs restent en majuscules : elles sont déjà renvoyées par
    `/auth/login` et `/me` à l'application mobile en production.
    """

    AGENT_CONTROLE = "AGENT_CONTROLE"
    SUPERVISEUR = "SUPERVISEUR"
    ADMIN = "ADMIN"


class UserStatus(StrEnum):
    """Statut d'un compte, exposé par l'API d'administration.

    Projection de la colonne booléenne `users.is_active`, qui reste le stockage :
    le dashboard manipule un statut nommé, l'app mobile continue de lire
    `is_active`. Aucune migration, aucun changement de contrat.
    """

    ACTIVE = "active"
    INACTIVE = "inactive"


class RecordStatus(StrEnum):
    """Statut d'archivage d'un référentiel (service, agent, motif).

    Aucune suppression physique n'est possible : des visites référencent ces
    enregistrements, et un `DELETE` ferait perdre l'historique du registre.
    """

    ACTIVE = "active"
    ARCHIVED = "archived"


class DocumentType(StrEnum):
    CNI = "CNI"
    PASSEPORT = "PASSEPORT"
    PERMIS = "PERMIS"


class Sexe(StrEnum):
    M = "M"
    F = "F"


class VisitStatus(StrEnum):
    PRESENT = "PRESENT"
    SORTI = "SORTI"
    # Annulation logique par un administrateur (erreur de saisie, doublon…). La
    # visite reste en base pour l'audit, avec son motif d'annulation.
    ANNULEE = "ANNULEE"


class MrzFormat(StrEnum):
    TD1 = "TD1"
    TD2 = "TD2"
    TD3 = "TD3"
