from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    """Rôles applicatifs, du plus restreint au plus large.

    - `AGENT_CONTROLE` — application mobile, poste de contrôle de son organisation ;
    - `SUPERVISEUR` — les mêmes opérations de terrain, plus les statistiques et
      l'annuaire de son organisation ;
    - `ORG_ADMIN` — administre **une** organisation : ses comptes, ses services,
      ses agents, sa charte graphique et ses réglages d'exploitation ;
    - `ADMIN` — administrateur de la plateforme, seul rôle non rattaché à une
      organisation et seul à voir toutes les structures.

    Tout rôle autre qu'`ADMIN` est rattaché à une organisation : c'est ce
    rattachement, et non le rôle, qui borne les données visibles (voir
    `TenantScope`).

    Les valeurs restent en majuscules : elles sont déjà renvoyées par
    `/auth/login` et `/me` à l'application mobile en production.
    """

    AGENT_CONTROLE = "AGENT_CONTROLE"
    SUPERVISEUR = "SUPERVISEUR"
    ORG_ADMIN = "ORG_ADMIN"
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


class CheckoutOrigin(StrEnum):
    """Origine de la clôture d'une visite."""

    MANUAL = "MANUAL"
    AUTOMATIC = "AUTOMATIC"


class ForgottenDocumentStatus(StrEnum):
    """État du suivi d'une pièce d'identité potentiellement oubliée."""

    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    SMS_SENT = "SMS_SENT"
    SMS_FAILED = "SMS_FAILED"
    NO_PHONE = "NO_PHONE"
