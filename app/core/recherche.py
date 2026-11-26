"""Motifs `LIKE` construits à partir d'une saisie utilisateur."""

from __future__ import annotations

# Caractère d'échappement passé à chaque `like(..., escape=ECHAPPEMENT)`.
ECHAPPEMENT = "\\"


def motif_contient(terme: str) -> str:
    """Motif « contient `terme` », jokers de la saisie neutralisés (audit SEC-013).

    Sans échappement, `%` et `_` tapés par l'utilisateur sont des jokers SQL :
    une recherche `%%%` listait toute la table, et `_` remplaçait n'importe
    quel caractère.
    """
    echappe = (
        terme.replace(ECHAPPEMENT, ECHAPPEMENT * 2)
        .replace("%", f"{ECHAPPEMENT}%")
        .replace("_", f"{ECHAPPEMENT}_")
    )
    return f"%{echappe}%"
