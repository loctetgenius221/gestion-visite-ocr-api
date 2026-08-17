"""Lecture et interprétation du CSV de l'annuaire (`app.import_annuaire`).

Ce fichier ne couvre que la partie sans base de données : dérivation des codes de
service, mise en forme des noms, lecture du CSV et regroupement par service.
L'écriture en base est vérifiée par `tests/integration/test_import_annuaire.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.import_annuaire import (
    CODE_SERVICE_DEFAUT,
    ImportError_,
    LigneAnnuaire,
    Rapport,
    _nom_complet,
    code_service,
    construire_plans,
    lire_csv,
)

ENTETE = "Matricule,nom_et_prenoms,Nom,Téléphone,Email,Fonction,Direction,Département,Sexe"


def ecrit_csv(tmp_path: Path, *lignes: str) -> Path:
    chemin = tmp_path / "annuaire.csv"
    chemin.write_text("\n".join((ENTETE, *lignes)) + "\n", encoding="utf-8")
    return chemin


class TestCodeService:
    @pytest.mark.parametrize(
        ("libelle", "attendu"),
        [
            ("Direction des Systemes D'informations", "DSI"),
            ("Direction de la Gestion des carrières", "DGC"),
            ("Direction de l’Administration Générale et de l’Équipement", "DAGE"),
            ("Sécretariat - Bureau Controle de Gestion", "SBCG"),
            ("Programme d'Appui à la Modernisation de l'Administration", "PAMA"),
            ("Division des Non Fonctionnaires", "DNF"),
        ],
    )
    def test_acronyme_des_mots_significatifs(self, libelle: str, attendu: str) -> None:
        assert code_service(libelle) == attendu

    @pytest.mark.parametrize("sigle", ["SG", "DRH"])
    def test_un_libelle_deja_sigle_est_repris_tel_quel(self, sigle: str) -> None:
        """Un acronyme d'acronyme (« DRH » → « D ») ne voudrait plus rien dire."""
        assert code_service(sigle) == sigle

    def test_les_accents_et_apostrophes_ne_passent_pas_dans_le_code(self) -> None:
        assert code_service("Cellule d'Études") == "CE"

    def test_libelle_sans_lettre(self) -> None:
        assert code_service("---") == "SERVICE"


class TestNomComplet:
    def test_prenoms_capitalises_et_patronyme_en_majuscules(self) -> None:
        assert _nom_complet("Sada Yéro", "Coumé") == "Sada Yéro COUMÉ"

    def test_la_casse_du_csv_est_uniformisee(self) -> None:
        """Le CSV mélange « Wade » et « WADE » d'une ligne à l'autre."""
        assert _nom_complet("daouda", "wade") == "Daouda WADE"
        assert _nom_complet("DAOUDA", "WADE") == "Daouda WADE"

    def test_patronyme_absent(self) -> None:
        assert _nom_complet("Moustapha Ndoye Ndiaye", "") == "Moustapha Ndoye Ndiaye"

    def test_ligne_vide(self) -> None:
        assert _nom_complet("", "") == ""


class TestLireCsv:
    def test_lecture_nominale(self, tmp_path: Path) -> None:
        chemin = ecrit_csv(
            tmp_path,
            "740999F,Aly,NDIAYE,775607812,a@b.sn,Developpeur,,Direction des Systemes,Homme",
        )

        lignes, rapport = lire_csv(chemin)

        assert rapport.lignes_lues == 1
        assert lignes == [LigneAnnuaire(2, "Aly NDIAYE", "Developpeur", "Direction des Systemes")]

    def test_la_direction_sert_de_repli_sans_departement(self, tmp_path: Path) -> None:
        chemin = ecrit_csv(tmp_path, "1,Aly,NDIAYE,,,Developpeur,Direction Informatique,,Homme")

        lignes, _ = lire_csv(chemin)

        assert lignes[0].libelle_service == "Direction Informatique"

    def test_une_ligne_sans_nom_est_ignoree(self, tmp_path: Path) -> None:
        chemin = ecrit_csv(tmp_path, ",,,,,Stagiaire,,DSI,")

        lignes, rapport = lire_csv(chemin)

        assert lignes == []
        assert rapport.ignorees == [(2, "nom et prénoms absents")]

    def test_fonction_vide_donne_none(self, tmp_path: Path) -> None:
        chemin = ecrit_csv(tmp_path, "1,Aly,NDIAYE,,,,,DSI,Homme")

        lignes, _ = lire_csv(chemin)

        assert lignes[0].fonction is None

    def test_valeur_trop_longue_tronquee_a_la_taille_de_colonne(self, tmp_path: Path) -> None:
        """Sans troncature, PostgreSQL romprait l'import entier sur un `DataError`."""
        chemin = ecrit_csv(tmp_path, f"1,Aly,NDIAYE,,,{'x' * 250},,DSI,Homme")

        lignes, rapport = lire_csv(chemin)

        assert lignes[0].fonction is not None
        assert len(lignes[0].fonction) == 200
        assert rapport.tronquees == [(2, "fonction")]

    def test_colonne_manquante_bloque_limport(self, tmp_path: Path) -> None:
        chemin = tmp_path / "vide.csv"
        chemin.write_text("Matricule,Email\n", encoding="utf-8")

        with pytest.raises(ImportError_, match="Colonnes introuvables"):
            lire_csv(chemin)

    def test_fichier_absent(self, tmp_path: Path) -> None:
        with pytest.raises(ImportError_, match="Lecture impossible"):
            lire_csv(tmp_path / "inexistant.csv")


class TestConstruirePlans:
    def _plans(self, *lignes: LigneAnnuaire, **kwargs: object):
        return construire_plans(list(lignes), Rapport(), **kwargs)  # type: ignore[arg-type]

    def test_regroupement_par_service(self) -> None:
        plans = self._plans(
            LigneAnnuaire(2, "Aly NDIAYE", "Developpeur", "Direction des Systemes"),
            LigneAnnuaire(3, "Birima NDIAYE", "Developpeur", "Direction des Systemes"),
            LigneAnnuaire(4, "Fatou FAYE", "Comptable", "Division des Fonctionnaires"),
        )

        assert {plan.code: len(plan.lignes) for plan in plans} == {"DS": 2, "DF": 1}

    def test_deux_libelles_de_meme_acronyme_sont_fusionnes(self) -> None:
        """« SG » et « Sécretariat Général » désignent la même entité."""
        rapport = Rapport()
        plans = construire_plans(
            [
                LigneAnnuaire(2, "Thioro MBAYE", "Secrétaire général", "SG"),
                LigneAnnuaire(3, "Demba GAYE", "Agent", "Sécretariat Général"),
            ],
            rapport,
        )

        assert len(plans) == 1
        # Le libellé le plus long est le plus parlant : c'est lui qui nomme le service.
        assert (plans[0].code, plans[0].nom) == ("SG", "Sécretariat Général")
        assert rapport.fusions == [("SG", ["SG", "Sécretariat Général"])]

    def test_les_lignes_sans_departement_vont_au_service_par_defaut(self) -> None:
        plans = self._plans(LigneAnnuaire(2, "Astel WADE", "Prestataire", None))

        assert [plan.code for plan in plans] == [CODE_SERVICE_DEFAUT]

    def test_ignorer_sans_service(self) -> None:
        rapport = Rapport()
        plans = construire_plans(
            [LigneAnnuaire(2, "Astel WADE", "Prestataire", None)],
            rapport,
            ignorer_sans_service=True,
        )

        assert plans == []
        assert rapport.ignorees == [(2, "département absent")]
