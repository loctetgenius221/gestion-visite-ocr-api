# Déploiement sur VPS

Mise en production du backend SIGV en installation native : PostgreSQL, l'API
sous systemd, et Nginx en terminaison TLS et serveur de fichiers.

```
                 :443                     127.0.0.1:8000              :5432
Internet ─────► nginx ───────────────────► sigv-api ────────────────► postgresql
                  │  proxy /api/v1          (uvicorn, N workers)       (local)
                  └─ sert /storage/uploads directement depuis le disque
```

L'API n'écoute que sur la boucle locale : elle n'est joignable que par le proxy.

---

## 1. Dimensionner la machine

L'OCR est le poste dominant : **chaque worker uvicorn charge sa propre copie du
modèle PaddleOCR**, soit environ 1,2 Go de RAM par worker.

| Ressource | Minimum | Confortable | Pourquoi |
|---|---|---|---|
| vCPU | 2 | 4 | Un scan MRZ sature les cœurs pendant ~6 s |
| RAM | 4 Go | 8 Go | 1,2 Go par worker + PostgreSQL + le système |
| Disque | 20 Go | 40 Go | Dépendances (~3 Go) et modèles (~500 Mo), plus les photos déposées |

Réglez `WEB_CONCURRENCY` et `OMP_NUM_THREADS` dans `.env` de sorte que
`WEB_CONCURRENCY × OMP_NUM_THREADS ≤ nombre de vCPU`. Sur un VPS 2 vCPU / 4 Go :
`WEB_CONCURRENCY=2` et `OMP_NUM_THREADS=1` — ou `WEB_CONCURRENCY=1` et
`OMP_NUM_THREADS=2` si vous préférez un scan rapide à deux scans simultanés.

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
```

...ainsi que `TRUSTED_HOSTS` et `CORS_ORIGINS` avec vos domaines réels.

> `ENVIRONMENT=production` active des garde-fous : l'API **refuse de démarrer**
> si la clé JWT est celle de l'exemple ou trop courte, si `CORS_ORIGINS` vaut
> `*`, si la base est en SQLite ou si `DB_ECHO=true`. Un service qui ne démarre
> pas et le dit vaut mieux qu'une API en ligne signant ses jetons avec une clé
> publique.

Schéma, données initiales et préchargement des modèles OCR :

```bash
uv run alembic upgrade head

# Référentiels et comptes agents — SEED_AGENT_PASSWORD doit être renseigné,
# faire au moins 12 caractères et différer du mot de passe de démonstration.
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
    /home/sigv/annuaire.csv --dry-run

# Puis en réel
sudo -u sigv /home/sigv/sigv-backend/.venv/bin/python -m app.import_annuaire \
    /home/sigv/annuaire.csv
```

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

Le service joue `alembic upgrade head` avant chaque démarrage : le schéma suit
toujours le code déployé.

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

nginx -t && systemctl reload nginx
```

Vérification :

```bash
curl http://localhost/health          # {"status":"ok","environment":"production"}
curl http://localhost/health/ready    # {"status":"ok","database":"ok"} — 503 si la base est HS
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

```bash
sudo -iu sigv
cd ~/sigv-backend
git pull
uv sync --frozen --no-dev
exit
systemctl restart sigv-api           # migrations jouées par ExecStartPre
```

L'indisponibilité dure le temps du redémarrage plus le préchargement du modèle
OCR, soit une trentaine de secondes. Pour l'éviter, il faudrait deux instances
sur des ports distincts derrière nginx, mises à jour l'une après l'autre.

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

### Durcir l'accès aux fichiers

Nginx sert `/storage/uploads/` **sans authentification**. Les noms de fichiers
sont des UUID v4 non devinables et le listing est désactivé, mais quiconque
obtient une URL accède à l'image du document d'identité.

Pour un accès authentifié, l'option la plus simple est `X-Accel-Redirect` :
ajouter à l'API une route protégée par le bearer token qui renvoie l'en-tête
`X-Accel-Redirect: /_fichiers/<chemin>`, et passer la location nginx en
`internal`. L'app Flutter devra alors joindre le token à ses requêtes d'image :
c'est un changement de contrat côté client, à planifier.

### Rotation de la clé JWT

Changer `JWT_SECRET_KEY` invalide instantanément tous les jetons émis : tous les
agents devront se reconnecter. À faire hors des heures d'ouverture du poste.

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
