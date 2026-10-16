"""URLs des fichiers déposés : signature, normalisation, rangement par organisation.

Les photos de pièces d'identité et les signatures sont servies par nginx, sans
passer par l'API. Une URL nue suffisait à les lire, pour qui la possédait —
journal de proxy, capture d'écran, appareil perdu. Elles sont désormais
**signées** et **datées** : nginx (`secure_link`) refuse une URL dont la
signature ne correspond pas, ou dont l'échéance est passée (ADR-024).

La base ne stocke jamais que le chemin nu : la signature est calculée à chaque
lecture, et retirée de ce que le client renvoie.
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from urllib.parse import urlsplit

from app.core.config import settings

# Logos d'organisation : affichés sur l'écran de connexion, avant toute
# authentification. Ils restent publics, et ne contiennent aucune donnée
# personnelle.
DOSSIER_PUBLIC = "organizations"

# Préfixe des fichiers rangés par organisation. Les fichiers déposés avant ce
# rangement n'en ont pas, et restent acceptés.
DOSSIER_ORGANISATIONS = "org"

# Fichiers déposés par l'administrateur de plateforme, qui n'a pas d'organisation.
DOSSIER_PLATEFORME = "plateforme"


def _base() -> str:
    return settings.STORAGE_PUBLIC_BASE_URL.rstrip("/")


def dossier_organisation(organization_id: uuid.UUID | None) -> str:
    """Préfixe de rangement des fichiers d'une organisation.

    Ranger par organisation rend possibles l'export et la suppression des
    fichiers d'un client en fin de contrat, et la mesure de son stockage.
    """
    suffixe = str(organization_id) if organization_id is not None else DOSSIER_PLATEFORME
    return f"{DOSSIER_ORGANISATIONS}/{suffixe}"


def sign_storage_url(url: str | None) -> str | None:
    """Ajoute à un chemin de fichier la signature attendue par nginx.

    Format imposé par `secure_link_md5 "$secure_link_expires$uri <clé>"` :
    MD5 en base64url sans remplissage. MD5 n'est pas un choix — c'est le seul
    condensat que le module nginx standard sait vérifier ; la clé secrète et
    l'échéance courte en font une protection suffisante pour cet usage.

    Sans clé configurée (développement), le chemin est rendu tel quel.
    """
    if not url or not settings.STORAGE_URL_SIGNING_KEY:
        return url
    chemin = urlsplit(url).path
    if not chemin.startswith(f"{_base()}/") or chemin.startswith(f"{_base()}/{DOSSIER_PUBLIC}/"):
        return url

    echeance = int(time.time()) + settings.STORAGE_URL_TTL_SECONDS
    materiau = f"{echeance}{chemin} {settings.STORAGE_URL_SIGNING_KEY}".encode()
    condensat = hashlib.md5(materiau).digest()  # noqa: S324 - imposé par nginx secure_link
    signature = base64.urlsafe_b64encode(condensat).rstrip(b"=").decode()
    return f"{chemin}?md5={signature}&expires={echeance}"


def normalize_storage_url(valeur: str | None) -> str | None:
    """Ramène une URL de fichier envoyée par un client au chemin stocké.

    Le client renvoie l'`url` reçue de `/uploads` : signée, et parfois préfixée
    de son `baseUrl`. On ne garde que le chemin, et on refuse tout ce qui ne
    désigne pas un fichier du stockage — URL externe, remontée de répertoire —
    plutôt que d'enregistrer au registre un lien que personne ne contrôle.
    """
    if valeur is None:
        return None
    chemin = urlsplit(valeur.strip()).path
    segments = chemin.split("/")
    if (
        not chemin.startswith(f"{_base()}/")
        or "\\" in chemin
        or ".." in segments
        or "" in segments[1:]
    ):
        raise ValueError(
            "URL de fichier invalide : déposez le fichier via `/uploads` et "
            "reportez l'`url` obtenue."
        )
    return chemin


def appartient_a_une_autre_organisation(chemin: str, organization_id: uuid.UUID) -> bool:
    """Le fichier est-il rangé chez une autre organisation ?

    Un fichier antérieur au rangement par organisation n'a pas de propriétaire
    lisible dans son chemin : il est accepté, pour ne pas faire échouer la
    synchronisation de tablettes restées hors ligne pendant la mise à jour.
    """
    prefixe = f"{_base()}/{DOSSIER_ORGANISATIONS}/"
    if not chemin.startswith(prefixe):
        return False
    return not chemin.startswith(f"{_base()}/{dossier_organisation(organization_id)}/")
