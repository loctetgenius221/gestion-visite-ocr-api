"""Écriture en base de l'annuaire importé (`app.import_annuaire`).

La partie lecture du CSV est couverte par `tests/unit/test_import_annuaire.py`.
Le test de bout en bout passe par `main()`, qui ouvre sa propre session via
`SessionLocal` : comme pour `app.create_user`, on la substitue par une base
SQLite de fichier jetable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import import_annuaire as cli
from app.import_annuaire import Rapport, appliquer, construire_plans, lire_csv
from app.models import Base
from app.models.referentiel import Agent, Service

ENTETE = "Matricule,nom_et_prenoms,Nom,Téléphone,Email,Fonction,Direction,Département,Sexe"

LIGNES = [
    "740999F,Aly,NDIAYE,775607812,aly@x.sn,Developpeur,,Direction des Systemes,Homme",
    "741000D,Birima,NDIAYE,774517228,bir@x.sn,Developpeur,,Direction des Systemes,Homme",
    "512520C,Papa Amadou,KAMARA,775641596,pa@x.sn,Chef de Division,,Division des Fonctionnaires,H",
]


def ecrit_csv(tmp_path: Path, *lignes: str) -> Path:
    chemin = tmp_path / "annuaire.csv"
    chemin.write_text("\n".join((ENTETE, *lignes)) + "\n", encoding="utf-8")
    return chemin


async def importe(
    session: AsyncSession, chemin: Path, **kwargs: object
) -> tuple[Rapport, list[Service], list[Agent]]:
    """Joue l'import sur la session de test et relit ce qui est en base."""
    lignes, rapport = lire_csv(chemin)
    plans = construire_plans(lignes, rapport)
    await appliquer(session, plans, rapport, **kwargs)  # type: ignore[arg-type]

    services = list((await session.execute(select(Service).order_by(Service.code))).scalars())
    agents = list((await session.execute(select(Agent).order_by(Agent.name))).scalars())
    return rapport, services, agents


async def test_cree_les_services_et_les_agents(
    session: AsyncSession, tmp_path: Path
) -> None:
    rapport, services, agents = await importe(session, ecrit_csv(tmp_path, *LIGNES))

    assert [(s.code, s.name) for s in services] == [
        ("DF", "Division des Fonctionnaires"),
        ("DS", "Direction des Systemes"),
    ]
    assert [a.name for a in agents] == ["Aly NDIAYE", "Birima NDIAYE", "Papa Amadou KAMARA"]
    assert rapport.agents_crees == 3
    assert len(rapport.services_crees) == 2

    par_code = {service.code: service.id for service in services}
    assert {a.name: a.service_id for a in agents}["Papa Amadou KAMARA"] == par_code["DF"]
    assert {a.name: a.role for a in agents}["Aly NDIAYE"] == "Developpeur"


async def test_relancer_limport_ne_cree_pas_de_doublons(
    session: AsyncSession, tmp_path: Path
) -> None:
    chemin = ecrit_csv(tmp_path, *LIGNES)
    await importe(session, chemin)

    rapport, services, agents = await importe(session, chemin)

    assert (len(services), len(agents)) == (2, 3)
    assert (rapport.agents_crees, rapport.agents_existants) == (0, 3)
    assert rapport.services_crees == []


async def test_un_service_existant_est_reutilise_par_son_code(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Le seed pose `DSI` = « Direction des Systèmes d'Information » : l'annuaire s'y range."""
    existant = Service(code="DS", name="Direction des Systèmes d'Information", floor="3e")
    session.add(existant)
    await session.commit()

    rapport, services, agents = await importe(session, ecrit_csv(tmp_path, *LIGNES))

    assert len(services) == 2
    # Ni le nom ni l'étage du service déjà administré ne sont écrasés par le CSV.
    assert (existant.name, existant.floor) == ("Direction des Systèmes d'Information", "3e")
    assert [a.service_id for a in agents if a.name == "Aly NDIAYE"] == [existant.id]
    assert [code for code, _, _ in rapport.services_reutilises] == ["DS"]


async def test_un_service_existant_est_reutilise_par_son_nom(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Même service, code différent : la comparaison ignore casse et accents."""
    existant = Service(code="INFO", name="DIRECTION DES SYSTÈMES")
    session.add(existant)
    await session.commit()

    _, services, agents = await importe(session, ecrit_csv(tmp_path, *LIGNES))

    assert len(services) == 2
    assert [a.service_id for a in agents if a.name == "Aly NDIAYE"] == [existant.id]


async def test_un_agent_homonyme_dans_un_autre_service_est_bien_cree(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Deux « Abdoulaye FALL » existent, dans deux directions différentes."""
    chemin = ecrit_csv(
        tmp_path,
        "601981E,Abdoulaye,FALL,,,Agent de bureau,,Direction des Systemes,Homme",
        "738736B,Abdoulaye,FALL,,,Agent de bureau,,Division des Fonctionnaires,Homme",
    )

    _, _, agents = await importe(session, chemin)

    assert len(agents) == 2
    assert agents[0].service_id != agents[1].service_id


async def test_un_doublon_du_csv_ne_donne_quune_fiche(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Même nom, même service : `agents` ne stocke pas le matricule, rien ne les distingue."""
    chemin = ecrit_csv(
        tmp_path,
        "740999F,Aly,NDIAYE,,,Developpeur,,Direction des Systemes,Homme",
        "741000D,ALY,ndiaye,,,Pupitreur,,Direction des Systemes,Homme",
    )

    rapport, _, agents = await importe(session, chemin)

    assert len(agents) == 1
    assert rapport.doublons == [(3, "Aly NDIAYE")]


class TestMiseAJour:
    async def test_la_fonction_dun_agent_existant_nest_pas_touchee_par_defaut(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """Les corrections faites depuis le dashboard priment sur le CSV."""
        await importe(session, ecrit_csv(tmp_path, *LIGNES))
        chemin = ecrit_csv(
            tmp_path, "740999F,Aly,NDIAYE,,,Chef de division,,Direction des Systemes,Homme"
        )

        rapport, _, agents = await importe(session, chemin)

        assert [a.role for a in agents if a.name == "Aly NDIAYE"] == ["Developpeur"]
        assert rapport.agents_mis_a_jour == 0

    async def test_mettre_a_jour_rafraichit_la_fonction(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        await importe(session, ecrit_csv(tmp_path, *LIGNES))
        chemin = ecrit_csv(
            tmp_path, "740999F,Aly,NDIAYE,,,Chef de division,,Direction des Systemes,Homme"
        )

        rapport, _, agents = await importe(session, chemin, mettre_a_jour=True)

        assert [a.role for a in agents if a.name == "Aly NDIAYE"] == ["Chef de division"]
        assert rapport.agents_mis_a_jour == 1


class TestDryRun:
    async def test_rien_nest_ecrit(self, session: AsyncSession, tmp_path: Path) -> None:
        rapport, services, agents = await importe(
            session, ecrit_csv(tmp_path, *LIGNES), dry_run=True
        )

        assert (services, agents) == ([], [])
        # Le rapport décrit malgré tout l'intégralité de ce qui serait créé.
        assert (rapport.agents_crees, len(rapport.services_crees)) == (3, 2)

    async def test_aucune_fonction_nest_modifiee(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        await importe(session, ecrit_csv(tmp_path, *LIGNES))
        chemin = ecrit_csv(
            tmp_path, "740999F,Aly,NDIAYE,,,Chef de division,,Direction des Systemes,Homme"
        )

        rapport, _, agents = await importe(session, chemin, mettre_a_jour=True, dry_run=True)

        assert [a.role for a in agents if a.name == "Aly NDIAYE"] == ["Developpeur"]
        assert rapport.agents_mis_a_jour == 1


async def test_deux_plans_ne_se_partagent_pas_un_meme_service(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Un service déjà revendiqué par son nom ne l'est pas une seconde fois par son code.

    Sans le garde-fou, deux annuaires distincts se déverseraient dans la même
    entrée ; ici le second obtient un code dérivé.
    """
    session.add(Service(code="DS", name="Direction Générale"))
    await session.commit()

    _, services, _ = await importe(
        session,
        ecrit_csv(
            tmp_path,
            "1,Aly,NDIAYE,,,Agent,,Direction Générale,Homme",
            "2,Birima,NDIAYE,,,Agent,,Direction des Systemes,Homme",
        ),
    )

    assert sorted(service.code for service in services) == ["DS", "DS2"]


class TestLigneDeCommande:
    @pytest.fixture
    async def base_temporaire(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> AsyncIterator[None]:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'import.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr(
            cli, "SessionLocal", async_sessionmaker(bind=engine, expire_on_commit=False)
        )
        monkeypatch.setattr(cli, "dispose_engine", _ne_fait_rien)
        yield
        await engine.dispose()

    async def test_sortie_et_code_retour_en_cas_de_succes(
        self, base_temporaire: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        chemin = ecrit_csv(tmp_path, *LIGNES)

        code = await cli.main([str(chemin), "--dry-run"])

        assert code == 0
        sortie = capsys.readouterr().out
        assert "lignes lues        : 3" in sortie
        assert "Direction des Systemes" in sortie
        assert "Simulation" in sortie

    async def test_code_retour_1_sur_fichier_absent(
        self, base_temporaire: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await cli.main([str(tmp_path / "absent.csv")])

        assert code == 1
        assert "Lecture impossible" in capsys.readouterr().err


async def _ne_fait_rien() -> None:
    """`dispose_engine` fermerait l'engine global, sans rapport avec la base de test."""
