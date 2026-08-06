"""Tests du parsing MRZ, isolés de l'OCR et d'OpenCV (spec §8).

Les échantillons sont générés par la lib `mrz` elle-même : les checksums sont donc
authentiquement valides, et les tests ne dépendent d'aucune image réelle.
"""

from __future__ import annotations

from datetime import date

import pytest
from mrz.generator.td1 import TD1CodeGenerator
from mrz.generator.td3 import TD3CodeGenerator

from app.core.errors import MrzNotDetectedError
from app.models.enums import DocumentType, MrzFormat, Sexe
from app.services.mrz_parser import (
    build_candidate,
    compute_check_digit,
    detect_format,
    extract_nin,
    parse_mrz_lines,
    sanitize_line,
)

# --- Échantillons de test (données factices, checksums valides) ---

TD3_LINES = str(
    TD3CodeGenerator(
        "P", "SEN", "DIOP", "AMINATA", "123456789", "SEN", "900514", "F", "300514", ""
    )
).splitlines()

TD1_LINES = str(
    TD1CodeGenerator(
        "I", "SEN", "123456789", "900514", "F", "300514", "SEN", "DIOP", "AMINATA",
        "1234567890123", "",
    )
).splitlines()

# CNI sénégalaise : le numéro de carte fait 17 chiffres et déborde donc du champ
# `document_number` (9 caractères max). Structure reproduite à l'identique d'une carte
# réelle, avec des valeurs **fictives** — aucune donnée personnelle n'est versionnée.
CNI_SEN_NUMERO = "20099887766554433"
CNI_SEN_LINES = [
    "I<SEN200998877<665544332<<<<<<",
    "9503124F3301018SEN<<<<<<<<<<<2",
    "NDIAYE<<FATOU<<<<<<<<<<<<<<<<<",
]


def test_echantillons_ont_les_dimensions_icao():
    assert len(TD3_LINES) == 2 and all(len(line) == 44 for line in TD3_LINES)
    assert len(TD1_LINES) == 3 and all(len(line) == 30 for line in TD1_LINES)


class TestSanitize:
    def test_supprime_les_caracteres_hors_alphabet_mrz(self):
        assert sanitize_line("  p<sen diop!! ") == "P<SENDIOP"

    def test_normalise_les_guillemets_pris_pour_des_chevrons(self):
        assert sanitize_line("DIOP«AMINATA") == "DIOP<AMINATA"


class TestDetectFormat:
    def test_deux_lignes_de_44_donnent_td3(self):
        assert detect_format(TD3_LINES) is MrzFormat.TD3

    def test_trois_lignes_de_30_donnent_td1(self):
        assert detect_format(TD1_LINES) is MrzFormat.TD1

    def test_liste_vide_ne_donne_aucun_format(self):
        assert detect_format([]) is None

    def test_nombre_de_lignes_inconnu_ne_donne_aucun_format(self):
        assert detect_format(["A" * 44] * 5) is None


class TestParseTD3:
    @pytest.fixture
    def result(self):
        return parse_mrz_lines(TD3_LINES)

    def test_document_reconnu_comme_passeport(self, result):
        assert result.document_type is DocumentType.PASSEPORT
        assert result.mrz_format is MrzFormat.TD3

    def test_checksums_valides(self, result):
        assert result.mrz_valid is True
        assert result.checksum_details.document_number is True
        assert result.checksum_details.date_of_birth is True
        assert result.checksum_details.expiration_date is True
        assert result.checksum_details.composite is True

    def test_champs_identite_extraits(self, result):
        assert result.fields.nom == "DIOP"
        assert result.fields.prenom == "AMINATA"
        assert result.fields.numero_document == "123456789"
        assert result.fields.nationalite == "SEN"
        assert result.fields.pays_emetteur == "SEN"
        assert result.fields.sexe is Sexe.F

    def test_dates_interpretees_avec_le_bon_siecle(self, result):
        assert result.fields.date_naissance == date(1990, 5, 14)
        assert result.fields.date_expiration == date(2030, 5, 14)

    def test_lignes_brutes_conservees(self, result):
        assert result.raw_mrz_lines == TD3_LINES


class TestParseTD1:
    @pytest.fixture
    def result(self):
        return parse_mrz_lines(TD1_LINES)

    def test_document_reconnu_comme_cni(self, result):
        assert result.document_type is DocumentType.CNI
        assert result.mrz_format is MrzFormat.TD1
        assert result.mrz_valid is True

    def test_le_nin_nest_jamais_deduit_du_mrz(self, result):
        # Vérifié sur une CNI sénégalaise réelle : le NIN n'est pas encodé dans le MRZ
        # (il est seulement imprimé au verso). Voir ADR-005.
        assert result.fields.nin is None

    def test_numero_de_document_court_lu_tel_quel(self, result):
        assert result.fields.numero_document == "123456789"

    def test_champs_identite_extraits(self, result):
        assert result.fields.nom == "DIOP"
        assert result.fields.prenom == "AMINATA"
        assert result.fields.date_naissance == date(1990, 5, 14)


class TestCniSenegalaise:
    """Convention de débordement ICAO 9303 du numéro de document (ADR-005).

    Cas réel des CNI sénégalaises : le numéro de carte fait 17 chiffres alors que le
    champ MRZ en accepte 9. La norme place un `<` en guise de chiffre de contrôle et
    reporte la fin du numéro — suivie du vrai checksum — dans la zone `optional_data`.
    """

    @pytest.fixture
    def result(self):
        return parse_mrz_lines(CNI_SEN_LINES)

    def test_le_numero_complet_est_reconstitue(self, result):
        assert result.fields.numero_document == CNI_SEN_NUMERO
        assert len(result.fields.numero_document) == 17

    def test_le_checksum_du_numero_complet_est_valide(self, result):
        # Sans le traitement du débordement, la lib `mrz` compare le `<` au chiffre
        # attendu et invalide à tort toute CNI sénégalaise.
        assert result.checksum_details.document_number is True
        assert result.mrz_valid is True

    def test_le_nin_nest_pas_extrait(self, result):
        # `optional_data` porte la fin du numéro de carte, pas le NIN.
        assert result.fields.nin is None

    def test_les_autres_champs_restent_corrects(self, result):
        assert result.document_type is DocumentType.CNI
        assert result.fields.nom == "NDIAYE"
        assert result.fields.prenom == "FATOU"
        assert result.fields.date_naissance == date(1995, 3, 12)
        assert result.fields.date_expiration == date(2033, 1, 1)
        assert result.fields.nationalite == "SEN"

    def test_sortie_ocr_reelle_avec_ligne_nin_et_bruit(self):
        """Reproduit la sortie brute observée sur une photo réelle de CNI.

        La bande basse de la carte contient la ligne NIN imprimée, les 3 lignes MRZ
        et quelques fragments parasites : tout doit être trié correctement.
        """
        sortie_ocr = [
            "NIN1 895 2003 00511",
            "na",
            *CNI_SEN_LINES,
        ]

        result = parse_mrz_lines(sortie_ocr)

        assert result.mrz_valid is True
        assert result.fields.numero_document == CNI_SEN_NUMERO
        assert result.fields.nin == "1895200300511"
        assert result.fields.nom == "NDIAYE"
        # Les lignes imprimées ne doivent pas polluer le MRZ restitué.
        assert result.raw_mrz_lines == CNI_SEN_LINES

    def test_debordement_annonce_mais_zone_optionnelle_vide(self):
        # `<` en position de checksum sans données de débordement exploitables :
        # on retombe sur le numéro tronqué, signalé comme non vérifié.
        ligne1 = "I<SEN200998877<<<<<<<<<<<<<<<<"
        result = parse_mrz_lines([ligne1, CNI_SEN_LINES[1], CNI_SEN_LINES[2]])

        assert result.fields.numero_document == "200998877"
        assert result.checksum_details.document_number is False
        assert result.mrz_valid is False


class TestExtractionDuNin:
    """Le NIN vient de la zone imprimée au-dessus du MRZ, lue dans la même passe OCR."""

    def test_ligne_nin_telle_que_lue_par_locr(self):
        # L'OCR colle fréquemment le libellé au premier chiffre.
        assert extract_nin(["NIN1 895 2003 00511"]) == "1895200300511"

    def test_libelle_separe_des_chiffres(self):
        assert extract_nin(["NIN 1 895 2003 00511"]) == "1895200300511"

    def test_nin_trouve_parmi_les_autres_lignes_de_la_carte(self):
        lignes = [
            "Bureau",
            "ECOLE CITE AINOUMADY",
            "NIN 1 895 2003 00511",
            "I<SEN101200302<010005582<<<<<<",
        ]
        assert extract_nin(lignes) == "1895200300511"

    def test_ligne_purement_numerique_sans_libelle(self):
        assert extract_nin(["1 895 2003 00511"]) == "1895200300511"

    def test_le_libelle_prime_sur_une_autre_ligne_de_13_chiffres(self):
        lignes = ["1234567890123", "NIN 1 895 2003 00511"]
        assert extract_nin(lignes) == "1895200300511"

    def test_aucune_ligne_ne_porte_de_nin(self):
        assert extract_nin(["Lieu de vote", "DAKAR", "Bureau 25"]) is None

    def test_nombre_de_chiffres_incorrect_est_rejete(self):
        assert extract_nin(["NIN 1 895 2003"]) is None

    def test_ligne_bavarde_totalisant_13_chiffres_est_rejetee(self):
        # Sans le libellé NIN, une ligne mêlant texte et chiffres est un faux positif.
        assert extract_nin(["KEUR MASSAR 25 SUD 1234567890 1"]) is None

    def test_liste_vide(self):
        assert extract_nin([]) is None


class TestCheckDigit:
    """Algorithme de chiffre de contrôle ICAO 9303 (pondération 7-3-1, modulo 10)."""

    def test_valeur_de_reference_numerique(self):
        assert compute_check_digit(CNI_SEN_NUMERO) == "2"

    def test_les_lettres_valent_10_a_35(self):
        # 'A' = 10 : 10*7 = 70 -> 0
        assert compute_check_digit("A") == "0"

    def test_le_remplisseur_vaut_zero(self):
        assert compute_check_digit("<<<") == "0"

    def test_caractere_invalide_ne_produit_aucun_chiffre(self):
        assert compute_check_digit("12$45") == ""


class TestToleranceOcr:
    def test_les_espaces_et_minuscules_sont_absorbes(self):
        bruite = [line.lower().replace("<", " < ") for line in TD3_LINES]
        result = parse_mrz_lines(bruite)
        assert result.mrz_valid is True
        assert result.fields.nom == "DIOP"

    def test_confusion_o_zero_corrigee_sur_une_position_numerique(self):
        # L'OCR lit 'O' au lieu de '0' dans la date de naissance : la correction
        # positionnelle doit rétablir le checksum.
        ligne2 = TD3_LINES[1]
        bruite = [TD3_LINES[0], ligne2[:13] + "9OO514" + ligne2[19:]]
        result = parse_mrz_lines(bruite)
        assert result.fields.date_naissance == date(1990, 5, 14)
        assert result.checksum_details.date_of_birth is True

    def test_ligne_tronquee_est_completee_au_gabarit(self):
        tronque = [TD3_LINES[0].rstrip("<"), TD3_LINES[1]]
        candidate = build_candidate(tronque)
        assert all(len(line) == 44 for line in candidate.lines)

    def test_bande_fusionnee_en_une_seule_ligne_est_redecoupee(self):
        fusionne = ["".join(TD1_LINES)]
        result = parse_mrz_lines(fusionne)
        assert result.mrz_format is MrzFormat.TD1
        assert result.fields.nom == "DIOP"


class TestEchecs:
    def test_aucune_ligne_exploitable_leve_mrz_not_detected(self):
        with pytest.raises(MrzNotDetectedError) as exc:
            parse_mrz_lines(["Ministère de la Fonction Publique", "Carte d'identité"])
        assert exc.value.error_code == "MRZ_NOT_DETECTED"
        assert exc.value.status_code == 422

    def test_liste_vide_leve_mrz_not_detected(self):
        with pytest.raises(MrzNotDetectedError):
            parse_mrz_lines([])

    def test_checksum_invalide_renvoie_les_champs_avec_mrz_valid_false(self):
        # Altération du chiffre de contrôle du numéro de document : le parsing doit
        # aboutir malgré tout, pour laisser l'agent corriger côté mobile (spec §4.2).
        ligne2 = TD3_LINES[1]
        faux_hash = "0" if ligne2[9] != "0" else "1"
        corrompu = [TD3_LINES[0], ligne2[:9] + faux_hash + ligne2[10:]]

        result = parse_mrz_lines(corrompu)

        assert result.mrz_valid is False
        assert result.checksum_details.document_number is False
        assert result.fields.nom == "DIOP"
        assert result.fields.numero_document == "123456789"
