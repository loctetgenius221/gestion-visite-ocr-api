# Locations applicatives, partagées par les serveurs HTTP et HTTPS.
# Extension `.inc` : jamais chargée automatiquement, uniquement par `include`.

# Le navigateur ne doit ni deviner un type MIME sur les images servies, ni
# afficher l'API dans une iframe tierce.
add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options DENY always;
add_header Referrer-Policy no-referrer always;

# --- Authentification : quota resserré ---------------------------------------
location /api/v1/auth/ {
    limit_req zone=sigv_auth burst=30 nodelay;
    include /etc/nginx/conf.d/proxy.inc;
    proxy_pass http://sigv_api;
}

# --- Scan MRZ : requête longue ------------------------------------------------
# ~6 s sur une machine de développement, davantage sur un VPS partagé. Le défaut
# nginx de 60 s couperait la connexion avant la réponse.
location /api/v1/ocr/ {
    limit_req zone=sigv_api burst=10 nodelay;
    include /etc/nginx/conf.d/proxy.inc;
    proxy_pass http://sigv_api;
    proxy_read_timeout 180s;
    proxy_send_timeout 180s;
    # L'image part vers l'API dès sa réception, sans attendre le corps complet.
    proxy_request_buffering off;
}

# --- Reste de l'API ------------------------------------------------------------
location /api/v1/ {
    limit_req zone=sigv_api burst=40 nodelay;
    include /etc/nginx/conf.d/proxy.inc;
    proxy_pass http://sigv_api;
    proxy_read_timeout 60s;
}

# --- Fichiers déposés ----------------------------------------------------------
# Photos de pièces d'identité et signatures : données personnelles. En
# production, l'API ne monte pas ce répertoire (SERVE_STORAGE=false) et nginx le
# sert depuis le disque, **à condition que l'URL soit signée** par l'API
# (`?md5=…&expires=…`, ADR-024). Une URL sans signature répond 403, une URL
# échue 410 : le client relit alors la visite pour obtenir une URL fraîche.
# Adaptez `alias` au chemin réel de l'installation.
location /storage/uploads/ {
    secure_link $arg_md5,$arg_expires;
    secure_link_md5 "$secure_link_expires$uri $sigv_storage_key";
    if ($secure_link = "") { return 403; }
    if ($secure_link = "0") { return 410; }

    alias /home/sigv/sigv-backend/storage/uploads/;
    autoindex off;
    # `add_header` ne s'hérite pas dès qu'une location en redéfinit : la liste est
    # reprise en entier ici.
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;
    add_header X-Robots-Tag "noindex, nofollow" always;
    # Privé : un cache partagé (proxy d'entreprise) ne doit pas garder une pièce
    # d'identité, et l'URL change à chaque lecture de toute façon.
    add_header Cache-Control "private, max-age=3600" always;

    # Aucun contenu servi depuis ce répertoire ne doit être interprété.
    location ~ \.(php|py|sh|html?)$ {
        return 404;
    }
}

# Logos d'organisation : affichés sur l'écran de connexion, avant toute
# authentification, et sans donnée personnelle. Seule exception à la signature.
location /storage/uploads/organizations/ {
    alias /home/sigv/sigv-backend/storage/uploads/organizations/;
    autoindex off;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;
    expires 7d;

    location ~ \.(php|py|sh|html?|svg)$ {
        return 404;
    }
}

# --- Sondes --------------------------------------------------------------------
location = /health {
    include /etc/nginx/conf.d/proxy.inc;
    proxy_pass http://sigv_api;
    access_log off;
}

location = /health/ready {
    include /etc/nginx/conf.d/proxy.inc;
    proxy_pass http://sigv_api;
    access_log off;
}

# --- Documentation interactive --------------------------------------------------
# Désactivée côté API en production (ENABLE_DOCS=false) : la requête aboutit alors
# sur un 404 propre. Décommentez `deny` pour la bloquer dès le proxy.
location /docs {
    # deny all;
    include /etc/nginx/conf.d/proxy.inc;
    proxy_pass http://sigv_api;
}

location = /openapi.json {
    include /etc/nginx/conf.d/proxy.inc;
    proxy_pass http://sigv_api;
}

# Rien d'autre n'est exposé.
location / {
    return 404;
}
