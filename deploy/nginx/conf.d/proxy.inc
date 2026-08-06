# En-têtes de proxy communs à toutes les locations applicatives.
# Extension `.inc` volontaire : nginx n'inclut automatiquement que `conf.d/*.conf`,
# ce fichier ne doit être chargé que par les `include` explicites.

proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
# Corrèle les logs nginx et les logs applicatifs : l'API renvoie le même
# identifiant dans l'en-tête X-Request-ID de sa réponse.
proxy_set_header X-Request-ID      $request_id;
proxy_redirect off;
