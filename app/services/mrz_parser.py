"""Parsing et validation des lignes MRZ (norme ICAO 9303).

Ce module est volontairement **indépendant de l'OCR et d'OpenCV** : il ne prend que
des lignes de texte en entrée. C'est ce qui permet de le tester sans image réelle
(spec §8) et de le réutiliser si l'extraction du texte change un jour de moteur.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from mrz.checker.td1 import TD1CodeChecker
from mrz.checker.td2 import TD2CodeChecker
from mrz.checker.td3 import TD3CodeChecker

from app.core.errors import MrzNotDetectedError, MrzParsingError
from app.core.logging import get_logger
from app.models.enums import DocumentType, MrzFormat, Sexe
from app.schemas.ocr import ChecksumDetails, MrzFields, MrzScanResponse

logger = get_logger(__name__)

# Le MRZ n'admet que A-Z, 0-9 et '<' : tout le reste est du bruit OCR.
_ALLOWED_CHARS = re.compile(r"[^A-Z0-9<]")

# Longueurs de ligne normalisées par format ICAO 9303.
_FORMAT_SPECS: dict[MrzFormat, tuple[int, int]] = {
    MrzFormat.TD1: (3, 30),
    MrzFormat.TD2: (2, 36),
    MrzFormat.TD3: (2, 44),
}

# Longueur du plus court format normalisé : sert de seuil de repli pour distinguer
# une ligne MRZ d'un libellé imprimé quand aucun '<' n'a survécu à l'OCR.
_MIN_MRZ_LINE_LENGTH = min(length for _, length in _FORMAT_SPECS.values())

# Corrections ciblées des confusions OCR les plus fréquentes, appliquées **par zone** :
# une position numérique ne peut pas contenir de lettre, et inversement.
_TO_DIGIT = str.maketrans(
    {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"}
)
_TO_ALPHA = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G"})

_CHECKER_BY_FORMAT = {
    MrzFormat.TD1: TD1CodeChecker,
    MrzFormat.TD2: TD2CodeChecker,
    MrzFormat.TD3: TD3CodeChecker,
}

# Pondération du calcul des chiffres de contrôle ICAO 9303.
_CHECK_WEIGHTS = (7, 3, 1)

# Le NIN sénégalais compte 13 chiffres (ex. imprimé « 1 895 2003 00511 »).
_NIN_LENGTH = 13

# Contrôles du rapport de la lib `mrz` que nous recalculons nous-mêmes : ils sont
# exclus de l'évaluation structurelle pour ne pas être comptés deux fois.
_RECOMPUTED_CHECKS = frozenset({"document number hash"})


@dataclass(frozen=True, slots=True)
class _DocNumberLayout:
    """Position du numéro de document, de son checksum et de la zone optionnelle."""

    line: int
    start: int
    end: int
    check: int
    optional_start: int
    optional_end: int


# Emplacements normalisés par format (ICAO 9303 parties 3 à 6).
_DOC_NUMBER_LAYOUT: dict[MrzFormat, _DocNumberLayout] = {
    MrzFormat.TD1: _DocNumberLayout(
        line=0, start=5, end=14, check=14, optional_start=15, optional_end=30
    ),
    MrzFormat.TD2: _DocNumberLayout(
        line=1, start=0, end=9, check=9, optional_start=28, optional_end=35
    ),
    MrzFormat.TD3: _DocNumberLayout(
        line=1, start=0, end=9, check=9, optional_start=28, optional_end=42
    ),
}

# Le premier caractère du MRZ code le type de document (ICAO 9303 §4).
_DOC_TYPE_BY_PREFIX = {
    "P": DocumentType.PASSEPORT,
    "I": DocumentType.CNI,
    "A": DocumentType.CNI,
    "C": DocumentType.CNI,
    "D": DocumentType.PERMIS,
}


@dataclass(frozen=True, slots=True)
class MrzCandidate:
    """Lignes MRZ normalisées et format identifié."""

    lines: list[str]
    mrz_format: MrzFormat


def sanitize_line(line: str) -> str:
    """Nettoie une ligne brute issue de l'OCR : majuscules, caractères MRZ uniquement."""
    cleaned = line.strip().upper().replace(" ", "").replace("«", "<").replace("К", "K")
    return _ALLOWED_CHARS.sub("", cleaned)


def _fit_length(line: str, expected: int) -> str:
    """Ramène une ligne à la longueur attendue : complète avec '<' ou tronque le débord."""
    if len(line) < expected:
        return line.ljust(expected, "<")
    return line[:expected]


def detect_format(lines: list[str]) -> MrzFormat | None:
    """Déduit le format MRZ du nombre de lignes et de leur longueur.

    On tolère un écart de quelques caractères : l'OCR ajoute ou perd fréquemment
    un ou deux `<` en bord de bande.
    """
    if not lines:
        return None
    candidates = [fmt for fmt, (n, _) in _FORMAT_SPECS.items() if n == len(lines)]
    if not candidates:
        return None
    # On compare à la ligne la **plus longue** : l'OCR tronque bien plus souvent
    # qu'il n'allonge, une moyenne ferait basculer un TD3 abîmé vers TD2.
    longest = max(len(line) for line in lines)
    return min(candidates, key=lambda fmt: abs(_FORMAT_SPECS[fmt][1] - longest))


def build_candidate(raw_lines: list[str]) -> MrzCandidate:
    """Transforme des lignes OCR brutes en un candidat MRZ exploitable.

    Lève `MrzNotDetectedError` si aucune structure MRZ plausible ne se dégage.
    """
    cleaned = [sanitize_line(line) for line in raw_lines]
    # Filtre les libellés imprimés du document captés au passage par l'OCR : une
    # ligne MRZ contient presque toujours au moins un remplisseur '<', et à défaut
    # elle atteint la longueur minimale d'un format normalisé.
    cleaned = [
        line
        for line in cleaned
        if (len(line) >= 15 and "<" in line) or len(line) >= _MIN_MRZ_LINE_LENGTH
    ]

    mrz_format = detect_format(cleaned)
    if mrz_format is None:
        # Deuxième chance : l'OCR a pu fusionner ou scinder les lignes. On tente de
        # recomposer une bande unique puis de la redécouper aux longueurs normalisées.
        joined = "".join(cleaned)
        for fmt, (count, length) in _FORMAT_SPECS.items():
            if len(joined) == count * length:
                mrz_format = fmt
                cleaned = [joined[i * length : (i + 1) * length] for i in range(count)]
                break

    if mrz_format is None:
        raise MrzNotDetectedError()

    expected_len = _FORMAT_SPECS[mrz_format][1]
    return MrzCandidate(
        lines=[_fit_length(line, expected_len) for line in cleaned], mrz_format=mrz_format
    )


def _apply_positional_corrections(candidate: MrzCandidate) -> MrzCandidate:
    """Corrige les confusions OCR sur les zones dont le type de caractère est imposé.

    Seules les positions strictement numériques (dates, checksums) ou strictement
    alphabétiques (codes pays) de la norme sont retouchées — on ne touche jamais aux
    zones libres comme le numéro de document, qui peut être alphanumérique.
    """
    lines = list(candidate.lines)

    def fix(line_idx: int, start: int, end: int, table: dict[int, str]) -> None:
        line = lines[line_idx]
        lines[line_idx] = line[:start] + line[start:end].translate(table) + line[end:]

    if candidate.mrz_format is MrzFormat.TD3:
        # Ligne 2 : [0:9] n° doc | 9 check | [10:13] nationalité | [13:19] naissance | 19 check
        #           20 sexe | [21:27] expiration | 27 check | 43 check composite
        fix(1, 9, 10, _TO_DIGIT)
        fix(1, 10, 13, _TO_ALPHA)
        fix(1, 13, 20, _TO_DIGIT)
        fix(1, 21, 28, _TO_DIGIT)
        fix(1, 43, 44, _TO_DIGIT)
        fix(0, 2, 5, _TO_ALPHA)
    elif candidate.mrz_format is MrzFormat.TD1:
        # Ligne 1 : [5:14] n° doc | 14 check ; Ligne 2 : [0:6] naissance | 6 check |
        # 7 sexe | [8:14] expiration | 14 check | [15:18] nationalité | 29 check composite
        fix(0, 14, 15, _TO_DIGIT)
        fix(0, 2, 5, _TO_ALPHA)
        fix(1, 0, 7, _TO_DIGIT)
        fix(1, 8, 15, _TO_DIGIT)
        fix(1, 15, 18, _TO_ALPHA)
        fix(1, 29, 30, _TO_DIGIT)
    elif candidate.mrz_format is MrzFormat.TD2:
        fix(1, 9, 10, _TO_DIGIT)
        fix(1, 10, 13, _TO_ALPHA)
        fix(1, 13, 20, _TO_DIGIT)
        fix(1, 21, 28, _TO_DIGIT)
        fix(1, 35, 36, _TO_DIGIT)
        fix(0, 2, 5, _TO_ALPHA)

    return MrzCandidate(lines=lines, mrz_format=candidate.mrz_format)


def _parse_mrz_date(value: str | None, *, is_expiry: bool) -> date | None:
    """Convertit une date MRZ `YYMMDD` en `date`.

    Le siècle n'est pas encodé : par convention, une date d'expiration est toujours
    dans le futur proche (20xx) alors qu'une date de naissance postérieure à l'année
    courante appartient au siècle précédent.
    """
    if not value or len(value) != 6 or not value.isdigit():
        return None
    yy, mm, dd = int(value[0:2]), int(value[2:4]), int(value[4:6])
    current_yy = date.today().year % 100
    century = 2000
    if not is_expiry and yy > current_yy:
        century = 1900
    try:
        return date(century + yy, mm, dd)
    except ValueError:
        return None


def _clean_name(value: str | None) -> str:
    return (value or "").replace("<", " ").strip()


def _checksum_details(
    report_fields: list[tuple[str, bool]], *, document_number_valid: bool
) -> ChecksumDetails:
    """Traduit le rapport de la lib `mrz` vers le schéma de réponse de la spec §5.5.

    Le checksum du numéro de document est recalculé par nos soins : la lib ne gère pas
    la convention de débordement et rejetterait à tort toute CNI sénégalaise.
    """
    checks = dict(report_fields)
    return ChecksumDetails(
        document_number=document_number_valid,
        date_of_birth=bool(checks.get("birth date hash", False)),
        expiration_date=bool(checks.get("expiry date hash", False)),
        composite=bool(checks.get("final hash", False)),
    )


def compute_check_digit(value: str) -> str:
    """Chiffre de contrôle ICAO 9303 : pondération 7-3-1, somme modulo 10.

    Valeurs : chiffres à leur valeur, `A`-`Z` de 10 à 35, `<` à 0.
    """
    total = 0
    for index, char in enumerate(value):
        if char == "<":
            digit = 0
        elif char.isdigit():
            digit = int(char)
        elif "A" <= char <= "Z":
            digit = ord(char) - 55
        else:
            return ""
        total += digit * _CHECK_WEIGHTS[index % 3]
    return str(total % 10)


def _resolve_document_number(candidate: MrzCandidate) -> tuple[str, bool]:
    """Extrait le numéro de document complet et valide son chiffre de contrôle.

    Gère la **convention de débordement ICAO 9303** : le champ `document_number` du
    MRZ est plafonné à 9 caractères. Quand le numéro réel est plus long — cas des CNI
    sénégalaises, dont le numéro de carte fait 17 chiffres — la norme impose :

    - les 9 premiers caractères dans le champ `document_number` ;
    - un `<` à la place du chiffre de contrôle, qui signale le débordement ;
    - le **reste du numéro** au début de la zone `optional_data`, suivi du chiffre de
      contrôle calculé sur le numéro **entier**, puis d'un `<` de terminaison.

    Sans ce traitement, le numéro serait tronqué à 9 caractères et son checksum
    déclaré invalide (la lib `mrz` compare le `<` au chiffre attendu).

    Retourne `(numéro complet, checksum valide)`.
    """
    layout = _DOC_NUMBER_LAYOUT[candidate.mrz_format]
    line = candidate.lines[layout.line]
    head = line[layout.start : layout.end]
    check_char = line[layout.check]
    optional = line[layout.optional_start : layout.optional_end]

    if check_char != "<":
        # Cas nominal : numéro court, chiffre de contrôle en place.
        return head.replace("<", "").strip(), compute_check_digit(head) == check_char

    overflow = optional.split("<")[0]
    if len(overflow) < 2:
        # Débordement annoncé mais zone optionnelle inexploitable.
        return head.replace("<", "").strip(), False

    numero = (head + overflow[:-1]).replace("<", "").strip()
    return numero, compute_check_digit(numero) == overflow[-1]


def extract_nin(ocr_lines: list[str]) -> str | None:
    """Récupère le NIN dans les lignes imprimées lues au-dessus du MRZ (ADR-005).

    Le NIN n'est **pas** encodé dans le MRZ — vérifié sur deux cartes réelles : la
    zone `optional_data` porte la fin du numéro de carte. Il est en revanche imprimé
    juste au-dessus de la bande MRZ, sous la forme `NIN 1 895 2003 00511`, et tombe
    donc dans la même passe OCR.

    Deux stratégies, de la plus sûre à la plus permissive :

    1. une ligne portant le libellé `NIN` — l'OCR colle fréquemment le libellé au
       premier chiffre (`NIN1 895 …`), d'où un simple filtrage des chiffres ;
    2. à défaut, une ligne réduite à exactement 13 chiffres.
    """
    for ligne in ocr_lines:
        normalisee = ligne.upper().replace(" ", "")
        if "NIN" not in normalisee:
            continue
        chiffres = re.sub(r"\D", "", normalisee)
        if len(chiffres) == _NIN_LENGTH:
            return chiffres

    for ligne in ocr_lines:
        chiffres = re.sub(r"\D", "", ligne)
        # Sans le libellé, on n'accepte que des lignes quasi exclusivement numériques :
        # une ligne bavarde qui totaliserait 13 chiffres serait un faux positif.
        if len(chiffres) == _NIN_LENGTH and len(re.sub(r"[\s\d]", "", ligne)) <= 1:
            return chiffres

    return None


def _resolve_document_type(mrz_format: MrzFormat, document_type_code: str) -> DocumentType:
    prefix = (document_type_code or "").strip("<")[:1]
    if prefix in _DOC_TYPE_BY_PREFIX:
        return _DOC_TYPE_BY_PREFIX[prefix]
    return DocumentType.PASSEPORT if mrz_format is MrzFormat.TD3 else DocumentType.CNI


def parse_mrz_lines(raw_lines: list[str]) -> MrzScanResponse:
    """Point d'entrée du parsing : lignes brutes OCR → réponse structurée (spec §5.5).

    Un échec de checksum ne bloque **pas** le flux : les champs sont renvoyés avec
    `mrz_valid=false` pour que l'agent de contrôle corrige manuellement (spec §4.2).
    """
    candidate = _apply_positional_corrections(build_candidate(raw_lines))
    checker_cls = _CHECKER_BY_FORMAT[candidate.mrz_format]
    mrz_code = "\n".join(candidate.lines)

    try:
        # `check_expiry=False` : un document expiré doit rester lisible — c'est une
        # information métier renvoyée à l'agent, pas une erreur de parsing.
        checker = checker_cls(mrz_code, check_expiry=False)
        fields = checker.fields()
        report = checker.report.fields
    except Exception as exc:  # la lib `mrz` lève des erreurs typées non exportées
        logger.warning(
            "Echec du parsing MRZ",
            extra={"mrz_format": candidate.mrz_format.value, "reason": str(exc)},
        )
        raise MrzParsingError(details={"reason": str(exc)}) from exc

    document_type = _resolve_document_type(candidate.mrz_format, fields.document_type)
    numero_document, document_number_valid = _resolve_document_number(candidate)
    checksums = _checksum_details(report, document_number_valid=document_number_valid)

    # `bool(checker)` serait faux dès que la convention de débordement est utilisée :
    # on réévalue la validité globale à partir des contrôles structurels de la lib et
    # de nos propres checksums.
    structural_ok = all(
        result for label, result in report if label not in _RECOMPUTED_CHECKS
    )
    is_valid = structural_ok and all(dict(checksums).values())

    sexe: Sexe | None = None
    if fields.sex in ("M", "F"):
        sexe = Sexe(fields.sex)

    return MrzScanResponse(
        document_type=document_type,
        mrz_format=candidate.mrz_format,
        mrz_valid=is_valid,
        fields=MrzFields(
            nom=_clean_name(fields.surname),
            prenom=_clean_name(fields.name),
            numero_document=numero_document,
            # Le NIN vient de la zone imprimée lue dans la même passe OCR, jamais du
            # MRZ qui ne le contient pas (ADR-005).
            nin=extract_nin(raw_lines) if document_type is DocumentType.CNI else None,
            nationalite=(fields.nationality or "").replace("<", "") or None,
            date_naissance=_parse_mrz_date(fields.birth_date, is_expiry=False),
            sexe=sexe,
            date_expiration=_parse_mrz_date(fields.expiry_date, is_expiry=True),
            pays_emetteur=(fields.country or "").replace("<", "") or None,
        ),
        checksum_details=checksums,
        raw_mrz_lines=candidate.lines,
    )
