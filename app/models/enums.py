from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    """Rôles applicatifs.

    Seul `AGENT_CONTROLE` est exploité aujourd'hui (aucune route n'est restreinte sur
    un autre rôle) ; l'enum est posé dès maintenant pour éviter une migration ultérieure.
    """

    AGENT_CONTROLE = "AGENT_CONTROLE"
    SUPERVISEUR = "SUPERVISEUR"
    ADMIN = "ADMIN"


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


class MrzFormat(StrEnum):
    TD1 = "TD1"
    TD2 = "TD2"
    TD3 = "TD3"
