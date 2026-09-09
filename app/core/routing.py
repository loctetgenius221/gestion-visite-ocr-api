"""Routes qui refusent les paramètres de requête qu'elles ne déclarent pas.

Starlette laisse passer en silence tout paramètre inconnu, et FastAPI ne le
regarde pas davantage. `GET /visits?organization_id=<autre structure>` répondait
donc `200` avec le registre **entier**, le filtre ayant été purement et
simplement perdu en route. L'appelant croyait lire une organisation, il lisait la
plateforme, et rien dans la réponse ne le lui disait.

C'est le défaut exact que le cloisonnement combat ailleurs :
`TenantScope.resolve()` refuse déjà de réécrire en douce une organisation
demandée, au motif qu'« un filtre ignoré sans le dire donnerait à l'appelant
l'illusion d'avoir consulté ce qu'il demandait ». Un paramètre entièrement ignoré
est la même illusion, une couche plus tôt — avant même d'atteindre le périmètre.

Un paramètre inconnu est donc traité comme n'importe quelle donnée invalide : il
lève le `RequestValidationError` habituel, que le handler global rend en `400`
`VALIDATION_ERROR`. Pas `422` : la spec §5.6 le réserve aux échecs **métier**, et
une faute de frappe dans une query string n'en est pas un. Le retour porte le nom
du paramètre fautif et la liste de ceux que la route accepte, de quoi trancher
entre le paramètre mal orthographié et celui que le backend n'a pas encore.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Response
from fastapi.dependencies.models import Dependant
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.requests import Request


def declared_query_params(dependant: Dependant) -> frozenset[str]:
    """Noms sous lesquels une route accepte un paramètre de requête.

    L'arbre des dépendances est parcouru en entier : les paramètres d'une route
    viennent rarement de sa signature seule, ils remontent de `get_visit_filters`,
    de `get_pagination` ou d'une dépendance de niveau routeur. Ce sont les
    **alias** qui sont collectés, puisque c'est sous l'alias que le client les
    envoie — `status`, `format`, `date`.

    FastAPI n'expose pas d'aplatisseur qui distingue query, header et cookie
    (`get_flat_params` les concatène), et le nom de ses fonctions internes a déjà
    bougé d'une version à l'autre. Le parcours est donc fait ici, sur la seule
    structure publique et stable qu'est `Dependant`.
    """
    noms: set[str] = set()
    vus: set[int] = set()
    a_visiter = [dependant]
    while a_visiter:
        courant = a_visiter.pop()
        if id(courant) in vus:
            continue
        vus.add(id(courant))
        for field in courant.query_params:
            annotation = field.field_info.annotation
            # Un modèle Pydantic en paramètre de requête vaut pour tous ses champs.
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                noms.update(
                    sous_champ.alias or nom
                    for nom, sous_champ in annotation.model_fields.items()
                )
            else:
                noms.add(field.alias)
        a_visiter.extend(courant.dependencies)
    return frozenset(noms)


class StrictQueryParamsRoute(APIRoute):
    """`APIRoute` qui rejette la query string qu'elle ne saurait pas honorer.

    Les paramètres acceptés sont ceux de `declared_query_params`, calculés une
    seule fois au montage de la route : la vérification par requête se réduit à
    une différence d'ensembles.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        acceptes = declared_query_params(self.dependant)

        async def strict_handler(request: Request) -> Response:
            inconnus = sorted(set(request.query_params) - acceptes)
            if inconnus:
                raise RequestValidationError(
                    [
                        {
                            "type": "unexpected_query_parameter",
                            "loc": ("query", nom),
                            "msg": "Paramètre de requête inconnu pour cette route.",
                            "input": request.query_params[nom],
                            "ctx": {"parametres_acceptes": sorted(acceptes)},
                        }
                        for nom in inconnus
                    ]
                )
            return await handler(request)

        return strict_handler
