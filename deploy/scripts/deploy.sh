#!/usr/bin/env bash
# Déploiement d'une version sur UN nœud API, sans coupure côté load balancer.
#
#   sudo bash deploy/scripts/deploy.sh <ref> [--migrate]
#
#   <ref>      tag ou commit à déployer (ex. v1.4.0). Jamais une branche :
#              chaque nœud doit tourner exactement le même code.
#   --migrate  joue `alembic upgrade head`. À passer sur le PREMIER nœud
#              seulement : les migrations sont une étape unique du déploiement.
#
# Déroulé sur un parc de deux nœuds :
#
#   nœud 1 : sudo bash deploy/scripts/deploy.sh v1.4.0 --migrate
#   nœud 2 : sudo bash deploy/scripts/deploy.sh v1.4.0
#
# Pendant le redémarrage, `/health/ready` ne répond pas : le load balancer sort
# le nœud de la rotation et l'autre sert le trafic. Une migration doit donc
# rester compatible avec la version précédente du code, qui tourne encore sur
# l'autre nœud (ajouter avant de retirer — voir DEPLOYMENT.md).

set -euo pipefail

APP_USER="${APP_USER:-sigv}"
APP_DIR="${APP_DIR:-/home/${APP_USER}/sigv-backend}"
SERVICE="${SERVICE:-sigv-api}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health/ready}"
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-120}"

ref="${1:-}"
migrate="false"
[[ "${2:-}" == "--migrate" ]] && migrate="true"

if [[ -z "${ref}" ]]; then
    echo "Usage : $0 <tag-ou-commit> [--migrate]" >&2
    exit 2
fi
if [[ "${EUID}" -ne 0 ]]; then
    echo "À lancer en root (redémarrage du service systemd)." >&2
    exit 2
fi

as_app() {
    sudo -u "${APP_USER}" -H bash -lc "cd '${APP_DIR}' && $*"
}

echo "==> Récupération de ${ref}"
as_app "git fetch --tags --prune origin && git checkout --detach '${ref}'"
echo "    version : $(as_app 'git describe --tags --always')"

echo "==> Dépendances (versions figées par uv.lock)"
as_app "uv sync --frozen --no-dev"

if [[ "${migrate}" == "true" ]]; then
    echo "==> Migrations"
    as_app ".venv/bin/alembic upgrade head"
fi
echo "    schéma : $(as_app '.venv/bin/alembic current' 2>/dev/null | tail -n 1)"

echo "==> Redémarrage de ${SERVICE}"
systemctl restart "${SERVICE}"

echo "==> Attente de ${HEALTH_URL} (${HEALTH_TIMEOUT_S} s max)"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_S ))
until curl --silent --fail --max-time 5 "${HEALTH_URL}" > /dev/null; do
    if (( $(date +%s) >= deadline )); then
        echo "ÉCHEC : le service ne répond pas. Derniers journaux :" >&2
        journalctl -u "${SERVICE}" -n 50 --no-pager >&2
        exit 1
    fi
    sleep 2
done

echo "==> ${SERVICE} prêt sur cette machine. Passez au nœud suivant."
