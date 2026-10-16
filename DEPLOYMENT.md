# Déploiement sur VPS

Mise en production du backend Fp-visites en installation native : PostgreSQL, l'API
sous systemd, et Nginx en terminaison TLS et serveur de fichiers.

> **Nom des ressources système.** L'application s'appelle désormais *Fp-visites*,
> mais l'utilisateur Linux, la base PostgreSQL, les unités systemd
> (`sigv-api.service`…) et les fichiers nginx gardent volontairement le préfixe
> `sigv`, hérité du nom précédent. Ce sont des identifiants d'infrastructure :
> les renommer imposerait, sur une machine déjà installée, de recréer
> l'utilisateur, de basculer la base et de redéployer les unités — pour un gain
> nul côté utilisateur, qui ne les voit jamais. Les libellés affichés
> (`systemctl status`, titre de l'API, exports) portent, eux, le nouveau nom.

```
                 :443                     127.0.0.1:8000              :5432
Internet ─────► nginx ───────────────────► sigv-api ────────────────► postgresql
                  │  proxy /api/v1          (uvicorn, N workers)       (local)
                  └─ sert /storage/uploads directement depuis le disque
```

L'API n'écoute que sur la boucle locale : elle n'est joignable que par le proxy.

---

## 1. Dimensionner la machine

La lecture MRZ se fait sur le mobile (Google ML Kit). L'OCR serveur PaddleOCR
n'est qu'un secours, **désactivé par défaut** (`server_ocr_enabled`) : tant qu'il
l'est, un worker consomme ~150 Mo. Dimensionnez pourtant pour le jour où il
s'active — chaque worker charge alors ~1,2 Go de modèle. Choix d'hébergement et
architecture cible : [`PRODUCTION_SAAS.md`](PRODUCTION_SAAS.md).

| Ressource | Minimum | Pourquoi |
|---|---|---|
| vCPU | 2 | API async légère ; le secours OCR occupe 2 cœurs pendant un scan |
| RAM | 4 Go | 2 workers + PostgreSQL local, et ~2,5 Go si le secours OCR s'active |
| Disque | 40 Go + volume données | Dépendances (~3 Go, PaddleOCR compris) ; photos sur un volume séparé et extensible |

`WEB_CONCURRENCY=2` suffit largement à une API async. Si le secours OCR est activé,
gardez `WEB_CONCURRENCY × OMP_NUM_THREADS ≤ nombre de vCPU`.

**Plusieurs nœuds API.** Les fichiers déposés vivent sur le disque local : deux
nœuds doivent alors partager le même volume (NFS) tant que le stockage objet S3
n'est pas branché (`PRODUCTION_SAAS.md`, 1.6). Les timers (§4) ne
s'activent que sur un seul nœud, et les migrations ne se jouent qu'une fois (§7).

---

## 2. Préparer le VPS

Debian 12 / Ubuntu 24.04, en root :

```bash
apt update && apt upgrade -y
apt install -y git curl nginx postgresql postgresql-contrib \
               libgomp1 libglib2.0-0        # runtimes exigés par PaddlePaddle et OpenCV

# Compte de service, sans mot de passe ni shell de connexion interactif
adduser --disabled-password --gecos "" sigv
# nginx (www-data) doit pouvoir traverser le répertoire pour servir les fichiers
chmod 755 /home/sigv
```

Pare-feu — seuls SSH et HTTP(S) sont ouverts, PostgreSQL n'est jamais exposé :

```bash
apt install -y ufw
ufw allow OpenSSH && ufw allow 'Nginx Full'
ufw enable
```

### Base de données

```bash
sudo -u postgres psql <<'SQL'
CREATE USER sigv WITH PASSWORD 'MOT_DE_PASSE_GENERE';
CREATE DATABASE sigv OWNER sigv ENCODING 'UTF8';
SQL
```

Vérifiez que PostgreSQL n'écoute que sur la boucle locale — c'est le défaut
Debian : `listen_addresses = 'localhost'` dans `/etc/postgresql/16/main/postgresql.conf`.

---

## 3. Installer l'application

En tant qu'utilisateur `sigv` (`sudo -iu sigv`) :

```bash
# uv — le projet est verrouillé par uv.lock
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

git clone <url-du-dépôt> ~/sigv-backend
cd ~/sigv-backend

# Dépendances de production uniquement, versions figées par le lock
uv sync --frozen --no-dev

cp .env.production.example .env
chmod 600 .env
```

Complétez les valeurs marquées « À REMPLIR » dans `.env` :

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # mot de passe PostgreSQL
python3 -c "import secrets; print(secrets.token_urlsafe(64))"   # JWT_SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # STORAGE_URL_SIGNING_KEY
```

...ainsi que `TRUSTED_HOSTS` et `CORS_ORIGINS` avec vos domaines réels
(`CORS_ORIGINS` = l'origine du dashboard, par exemple `https://admin.votre-domaine.sn`).

> `ENVIRONMENT=production` active des garde-fous : l'API **refuse de démarrer**
> si la clé JWT est celle de l'exemple ou trop courte, si `CORS_ORIGINS` vaut
> `*`, si la base est en SQLite, si `DB_ECHO=true` ou si
> `STORAGE_URL_SIGNING_KEY` manque. Un service qui ne démarre pas et le dit vaut
> mieux qu'une API en ligne signant ses jetons avec une clé publique.

Schéma, données initiales et préchargement des modèles OCR :

```bash
# PostgreSQL ≥ 13 : l'extension `pg_trgm` (recherche des visiteurs) est créée par
# la migration, ce que le propriétaire de la base a le droit de faire.
uv run alembic upgrade head

# Référentiels et comptes agents — SEED_AGENT_PASSWORD doit être renseigné,
# faire au moins 12 caractères et différer du mot de passe de démonstration.
# Crée aussi l'organisation `MIN-INTERIEUR`, à laquelle tous les comptes sont
# rattachés sauf `admin001` : `ADMIN` est le seul rôle global de la plateforme.
uv run python -m app.seeds

# Télécharge les modèles PaddleOCR (~500 Mo) maintenant plutôt qu'au premier scan
uv run python -c "from app.services.ocr_engine import get_ocr_engine; get_ocr_engine().load()"
```

### Annuaire du ministère

`app.seeds` pose des services et des agents **de démonstration**. En production,
remplissez les référentiels depuis l'export CSV de l'annuaire, une fois le schéma
migré :

```bash
# Déposez le CSV hors du dépôt, lisible par l'utilisateur `sigv` uniquement
install -o sigv -g sigv -m 600 annuaire.csv /home/sigv/annuaire.csv

# Toujours en simulation d'abord : le rapport montre les services qui seraient
# créés, ceux qui seraient réutilisés et les libellés fusionnés.
sudo -u sigv /home/sigv/sigv-backend/.venv/bin/python -m app.import_annuaire \
    /home/sigv/annuaire.csv --organisation MIN-INTERIEUR --dry-run

# Puis en réel
sudo -u sigv /home/sigv/sigv-backend/.venv/bin/python -m app.import_annuaire \
    /home/sigv/annuaire.csv --organisation MIN-INTERIEUR
```

`--organisation` désigne la structure qui reçoit l'annuaire, par son code. Un
service appartient toujours à une organisation, et le rapprochement avec
l'existant est borné à celle-ci : deux ministères ont chacun leur « DRH », et
réutiliser celui du voisin y déverserait un annuaire étranger.

Le script est idempotent : à chaque nouvelle version de l'annuaire, relancez-le
sur le CSV complet. Il n'ajoute que ce qui manque, ne renomme pas les services
déjà administrés, et ne modifie une fiche existante qu'avec `--mettre-a-jour`.
Rien n'est jamais supprimé : les départs se traitent en archivant depuis le
dashboard, parce que les visites déjà enregistrées référencent l'agent visité.

Le CSV contient les nom, téléphone et e-mail d'agents réels : effacez-le du
serveur une fois l'import vérifié.

```bash
shred -u /home/sigv/annuaire.csv
```

---

## 4. Service systemd

En root :

```bash
cp /home/sigv/sigv-backend/deploy/systemd/sigv-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sigv-api
systemctl status sigv-api
journalctl -u sigv-api -f          # logs JSON de l'application
```

Le service **ne migre pas** la base à son démarrage : avec plusieurs nœuds, chaque
redémarrage la migrerait en concurrence. Les migrations sont une étape du
déploiement, jouée une seule fois (§7).

> **Plusieurs nœuds API** : les trois timers ci-dessous (clôture, purge des
> images, purge des sessions) ne s'activent que sur **un seul** nœud.

### Clôture automatique des visites

Une visite oubliée en `PRESENT` fausse le registre et les statistiques, et masque
la pièce d'identité qu'un visiteur a laissée au poste. Un timer clôture chaque
soir ce qui traîne, et ouvre une fiche « pièce potentiellement oubliée ».

Mesurez d'abord ce qui serait clôturé — la commande ne touche à rien :

```bash
sudo -u sigv /home/sigv/sigv-backend/.venv/bin/python -m app.auto_close_visits --dry-run
```

Puis activez le timer :

```bash
cp /home/sigv/sigv-backend/deploy/systemd/sigv-auto-close-visits.service /etc/systemd/system/
cp /home/sigv/sigv-backend/deploy/systemd/sigv-auto-close-visits.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sigv-auto-close-visits.timer

systemctl list-timers sigv-auto-close-visits.timer   # prochaine exécution
journalctl -u sigv-auto-close-visits                 # résultat des exécutions
```

Le timer se déclenche **toutes les cinq minutes**, et non une fois par jour à
heure fixe. L'heure de clôture est un réglage **par organisation**
(`automatic_visit_closure_time`, 17:00 par défaut), que chaque administrateur de
structure modifie depuis le dashboard : un timer calé sur une heure unique
obligerait à éditer l'unité systemd à chaque changement, et ne saurait pas servir
deux structures aux horaires différents.

Le coût de ces réveils est nul : la commande sort immédiatement si l'heure d'une
organisation n'est pas atteinte, et la table `automatic_closure_runs` porte une
contrainte d'unicité sur `(organisation, date)` — une seconde exécution le même
jour est un no-op tracé, y compris en cas de relance ou de concurrence.

Les organisations désactivées sont écartées : leurs comptes n'ont plus accès à
l'API, clôturer leurs visites au nom de personne n'aurait pas de sens.

### Purge des photos de pièces d'identité

Les images de CNI sont la donnée la plus sensible de l'installation. Elles ne sont
**pas** supprimées automatiquement par l'application : un timer les efface passé
leur durée de conservation (ADR-018).

Mesurez d'abord ce qui serait supprimé — la commande ne touche à rien :

```bash
sudo -u sigv /home/sigv/sigv-backend/.venv/bin/python -m app.purge_documents --dry-run
```

Puis activez le timer :

```bash
cp /home/sigv/sigv-backend/deploy/systemd/sigv-purge-documents.service /etc/systemd/system/
cp /home/sigv/sigv-backend/deploy/systemd/sigv-purge-documents.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sigv-purge-documents.timer

systemctl list-timers sigv-purge-documents.timer   # prochaine exécution
journalctl -u sigv-purge-documents                 # résultat des exécutions
```

La durée est le paramètre système `document_images_retention_days` — 365 jours par
défaut, `0` désactive la purge — modifiable depuis le dashboard sans redéploiement.
Seules les **images** partent : visites, visiteurs et journal d'audit restent
intacts, et chaque exécution laisse une trace `visitor.documents_purged`.

### Purge des sessions

Chaque connexion de tablette crée une session, chaque déconnexion un token
révoqué : sans purge, ces tables grossissent indéfiniment. Le timer quotidien
supprime les tokens révoqués expirés et les sessions mortes depuis plus de
30 jours (`--garder-jours`).

```bash
sudo -u sigv /home/sigv/sigv-backend/.venv/bin/python -m app.purge_sessions --dry-run

cp /home/sigv/sigv-backend/deploy/systemd/sigv-purge-sessions.service /etc/systemd/system/
cp /home/sigv/sigv-backend/deploy/systemd/sigv-purge-sessions.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sigv-purge-sessions.timer
```

---

## 5. Nginx

```bash
cp /home/sigv/sigv-backend/deploy/nginx/conf.d/00-shared.conf   /etc/nginx/conf.d/
cp /home/sigv/sigv-backend/deploy/nginx/conf.d/10-http.conf     /etc/nginx/conf.d/
cp /home/sigv/sigv-backend/deploy/nginx/conf.d/proxy.inc        /etc/nginx/conf.d/
cp /home/sigv/sigv-backend/deploy/nginx/conf.d/sigv-app.inc     /etc/nginx/conf.d/

# Le site par défaut de Debian occupe déjà `default_server` sur le port 80
rm -f /etc/nginx/sites-enabled/default

mkdir -p /var/www/certbot && chown -R www-data:www-data /var/www/certbot
```

### Accès aux fichiers

Les photos de pièces et les signatures ne sont servies que sur **URL signée** par
l'API (ADR-024). nginx vérifie la signature avec la même clé que
`STORAGE_URL_SIGNING_KEY` du `.env` — à recopier à l'identique :

```bash
nginx -V 2>&1 | grep -o with-http_secure_link_module   # module présent (paquets Debian/Ubuntu)

CLE=$(grep '^STORAGE_URL_SIGNING_KEY=' /home/sigv/sigv-backend/.env | cut -d= -f2-)
printf 'map $host $sigv_storage_key { default "%s"; }\n' "$CLE" > /etc/nginx/sigv-storage-key.conf
chown root:www-data /etc/nginx/sigv-storage-key.conf
chmod 640 /etc/nginx/sigv-storage-key.conf

nginx -t && systemctl reload nginx
```

Une URL sans signature répond `403`, une URL échue `410` : le client relit la
visite pour en obtenir une fraîche. Les logos d'organisation restent publics.

> Changer la clé invalide toutes les URLs en cours (une heure au plus) : modifiez
> `.env` et `sigv-storage-key.conf` ensemble, puis redémarrez l'API et rechargez nginx.

Vérification :

```bash
curl http://localhost/health          # {"status":"ok","environment":"production"}
curl http://localhost/health/ready    # {"status":"ok","database":"ok","storage":"ok"} — 503 sinon
```

### Dashboard d'administration

Le build de production du dashboard Vue est servi par nginx sur son propre
domaine, avec une politique de sécurité de contenu stricte :

```bash
mkdir -p /var/www/fp-visites-admin        # y déposer le contenu de `dist/`
cp /home/sigv/sigv-backend/deploy/nginx/conf.d/30-admin.conf.example /etc/nginx/conf.d/30-admin.conf
# remplacer admin.exemple.sn et api.exemple.sn, émettre le certificat (§6), puis :
nginx -t && systemctl reload nginx
```

> Si `/storage/uploads/...` renvoie 403, c'est que www-data ne peut pas traverser
> l'arborescence : `chmod 755 /home/sigv` et vérifiez le chemin de l'`alias` dans
> `/etc/nginx/conf.d/sigv-app.inc`.

---

## 6. Nom de domaine et HTTPS

1. Faites pointer un enregistrement `A` (et `AAAA` si IPv6) vers l'IP du VPS.
2. Vérifiez que `http://votre.domaine.sn/health` répond.
3. Émettez le certificat — nginx sert déjà `/.well-known/acme-challenge/` :

```bash
apt install -y certbot
certbot certonly --webroot -w /var/www/certbot -d api.exemple.sn \
  --email vous@exemple.sn --agree-tos --no-eff-email
```

4. Activez le serveur HTTPS :

```bash
cp /home/sigv/sigv-backend/deploy/nginx/conf.d/20-https.conf.example /etc/nginx/conf.d/20-https.conf
sed -i 's/api.exemple.sn/votre.domaine.sn/g' /etc/nginx/conf.d/20-https.conf
```

5. Dans `/etc/nginx/conf.d/10-http.conf`, remplacez la ligne
   `include /etc/nginx/conf.d/sigv-app.inc;` par la redirection :

```nginx
location / { return 301 https://$host$request_uri; }
```

6. Rechargez : `nginx -t && systemctl reload nginx`

Le paquet `certbot` installe son propre timer de renouvellement. Ajoutez le
rechargement de nginx après renouvellement :

```bash
echo -e '#!/bin/sh\nsystemctl reload nginx' > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
certbot renew --dry-run
```

---

## 7. Exploitation

```bash
systemctl status sigv-api            # état du service
journalctl -u sigv-api -f            # logs applicatifs (JSON)
journalctl -u sigv-api --since today
systemctl restart sigv-api
sudo -u postgres psql sigv           # console SQL
```

### Mise à jour

On déploie un **tag** validé par la CI (`.github/workflows/ci.yml`), jamais une
branche : chaque nœud doit tourner exactement le même code.

```bash
# Nœud unique, ou premier nœud : migre la base puis redémarre
sudo bash /home/sigv/sigv-backend/deploy/scripts/deploy.sh v1.4.0 --migrate

# Nœuds suivants : même tag, sans migration
sudo bash /home/sigv/sigv-backend/deploy/scripts/deploy.sh v1.4.0
```

Le script récupère le tag, synchronise les dépendances, migre si demandé,
redémarre l'API et attend que `/health/ready` réponde ; il échoue en affichant
les derniers journaux sinon. **Faites une sauvegarde de la base avant toute
migration.**

Avec deux nœuds derrière un load balancer, la mise à jour se fait sans coupure :
le nœud qui redémarre sort de la rotation le temps de répondre à nouveau. Cela
impose une règle d'écriture des migrations : rester compatible avec la version
précédente du code, qui tourne encore sur l'autre nœud — **ajouter avant de
retirer** (une colonne retirée l'est dans une version ultérieure, une fois plus
aucun code ne la lisant).

### Sauvegardes

Deux choses à sauvegarder : la base et les fichiers déposés.

```bash
# Base — dump compressé horodaté
sudo -u postgres pg_dump -Fc sigv > "sigv-$(date +%F).dump"

# Fichiers (images MRZ, signatures)
tar czf "storage-$(date +%F).tar.gz" -C /home/sigv/sigv-backend/storage uploads
```

Restauration : `sudo -u postgres pg_restore -d sigv --clean sigv-2026-08-06.dump`

> Ces sauvegardes contiennent des données d'identité (nom, date de naissance,
> numéro de document, NIN, photo du document). Chiffrez-les, stockez-les hors du
> VPS, et fixez une durée de conservation.

Automatisation minimale, dans le `crontab -e` de root :

```cron
0 2 * * * sudo -u postgres pg_dump -Fc sigv > /var/backups/sigv-$(date +\%F).dump
0 3 * * 0 find /var/backups -name 'sigv-*.dump' -mtime +30 -delete
```

---

## 8. Points de vigilance

### Rotation de la clé JWT

Sans coupure : placez l'ancienne clé dans `JWT_SECRET_KEY_PREVIOUS`, la nouvelle
dans `JWT_SECRET_KEY`, et redémarrez. Les jetons déjà émis restent valides jusqu'à
leur expiration ; videz `JWT_SECRET_KEY_PREVIOUS` après `REFRESH_TOKEN_EXPIRE_DAYS`
jours. En cas de **fuite** de la clé, ne renseignez pas la précédente : tous les
agents se reconnectent, c'est le but.

### Base de données

Le pool est réglé par `DB_POOL_SIZE` et `DB_MAX_OVERFLOW`, **par worker**. Gardez
`nœuds × WEB_CONCURRENCY × (DB_POOL_SIZE + DB_MAX_OVERFLOW)` sous le
`max_connections` de PostgreSQL. Au-delà de quelques nœuds, intercalez PgBouncer
en mode transaction et passez `DB_USE_PGBOUNCER=true`. Toute requête est annulée
au bout de `DB_STATEMENT_TIMEOUT_MS` (30 s).

### Latence du scan

`POST /api/v1/ocr/scan` prend 6 à 15 s. Le proxy est réglé sur 180 s
(`proxy_read_timeout`) et le client Flutter doit prévoir un `receiveTimeout`
d'au moins 60 s.

### Journalisation

Les logs applicatifs partent sur la sortie standard au format JSON et sont
collectés par journald. Bornez-les dans `/etc/systemd/journald.conf` :

```ini
SystemMaxUse=500M
MaxRetentionSec=30day
```

Aucune donnée personnelle n'est écrite dans les logs applicatifs : les clés
sensibles sont masquées à la source (`app/core/logging.py`).

### Conteneurisation

Volontairement écartée pour cette version. Le jour où elle devient utile, les
seuls éléments à produire sont un `Dockerfile` (image Python 3.12 slim,
`uv sync --frozen --no-dev`, `libgomp1` et `libglib2.0-0`, modèles OCR
préchargés à la construction) et un `docker-compose.yml` ; la configuration
nginx et les réglages de production de cette page restent valables tels quels.
