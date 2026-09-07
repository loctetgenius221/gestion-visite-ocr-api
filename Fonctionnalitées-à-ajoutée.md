Fonctionnement recommandé

À l’heure configurée, le serveur :

1. récupère les visites encore  PRESENT  et entrées avant l’heure limite ;
2. les clôture avec  statut = SORTI ,  checked_out_at  égal à l’heure d’exécution ;
3. renseigne une origine de clôture  MANUAL  ou  AUTOMATIC ;
4. crée une fiche de suivi « pièce potentiellement oubliée » pour chaque visite clôturée automatiquement ;
5. journalise l’opération dans l’audit.

Une visite enregistrée après l’heure limite ne doit pas être clôturée rétroactivement le même jour : elle sera traitée au prochain cycle.

Modifications backend à prévoir

1. Modèle de visite

Ajouter à  Visit  :

•  checkout_origin :  MANUAL  /  AUTOMATIC ;
• éventuellement  automatic_closure_date  ou une référence vers l’exécution quotidienne.

Cela permet de distinguer une sortie réelle d’une clôture automatique sans casser le statut existant  SORTI .

2. Suivi des pièces oubliées

Créer une table dédiée, préférable à plusieurs colonnes dans  visits  :

•  id ;
•  visit_id  unique ;
•  visitor_id ;
•  status :  PENDING ,  RESOLVED ,  SMS_SENT ,  SMS_FAILED , éventuellement  NO_PHONE ;
•  resolved_at ,  resolved_by ;
•  sms_sent_at ;
• identifiant et erreur du fournisseur SMS ;
• timestamps.

Ainsi, l’agent peut retirer une personne de la liste sans modifier l’historique de la visite.

3. Réglage administrateur

Étendre les schémas existants  SystemSettingsRead/Update  avec :

•  automatic_visit_closure_enabled ;
•  automatic_visit_closure_time , défaut  17:00 .

La table clé/valeur actuelle permet cet ajout sans nouvelle colonne. Il faudra toutefois valider strictement le format  HH:MM  et auditer chaque modification.

Le fuseau doit être explicite. Pour le Sénégal,  Africa/Dakar  convient ; il devrait être configuré côté serveur, par exemple via  APP_TIMEZONE , tandis que l’heure reste modifiable dans le dashboard.

4. Planification

Le projet n’utilise volontairement pas Celery/Redis. Le meilleur choix est donc :

• une commande  python -m app.auto_close_visits ;
• un service systemd  sigv-auto-close-visits.service ;
• un timer systemd exécuté chaque minute.

La commande lit l’heure configurée et ne fait rien avant celle-ci. Une table  automatic_closure_runs  avec une date unique garantirait une seule exécution par jour, même en cas de relance ou de concurrence.

Cette approche s’intègre au déploiement existant, comme  sigv-purge-documents.timer , sans lancer une tâche par worker Uvicorn.

5. API à ajouter

Pour le dashboard :

•  GET /visits/auto-closed?date=YYYY-MM-DD&status=PENDING ;
•  POST /forgotten-document-cases/{id}/resolve ;
•  POST /forgotten-document-cases/{id}/send-sms .

La première route doit retourner la personne, la visite, le service, l’heure d’entrée, le numéro de téléphone et l’état du suivi.

Les agents de contrôle devraient pouvoir consulter et traiter les cas ; les administrateurs conserveraient également l’accès. Le réglage de l’heure resterait réservé à  ADMIN .

6. SMS

Créer une abstraction  SmsService , indépendante du fournisseur :

• validation du numéro ;
• template configurable côté code ;
• envoi idempotent ;
• conservation de l’état  SENT/FAILED ;
• possibilité de réessayer un échec ;
• journalisation de l’action sans exposer inutilement le numéro complet ni le contenu sensible.

Le message pourrait être du type : « Bonjour [Prénom], votre pièce d’identité semble être restée au poste d’accueil de [structure]. Merci de venir la récupérer. »

Le fournisseur réel et ses secrets doivent rester dans  .env , jamais dans la base ni le code.

Adaptations frontend/dashboard

Le dashboard devra ajouter :

• un réglage de l’heure automatique ;
• une liste filtrée par date des clôtures automatiques ;
• une action « pièce récupérée / retirer de la liste » ;
• une action « envoyer un SMS » ;
• un état visuel du SMS : non envoyé, envoyé, échec, numéro absent ;
• une confirmation avant envoi ;
• un bouton désactivé si aucun téléphone n’est disponible.

Le dépôt actuel est principalement backend : le contrat API devra être documenté pour l’équipe frontend dans  INTEGRATION_FRONTEND.md .

Tests et documentation

Prévoir :

• migration Alembic ;
• tests de clôture à l’heure configurée ;
• tests avant/après l’heure limite ;
• test des visites entrées après l’heure ;
• test d’idempotence et de concurrence ;
• test du suivi  PENDING/RESOLVED/SMS_SENT/FAILED  ;
• test sans numéro de téléphone ;
• test des droits agent/admin ;
• test du fournisseur SMS simulé ;
• test des audits.

Documents à mettre à jour :  SPEC_BACKEND_FP_VISITES_OCR_MRZ.md ,  ARCHITECTURE_DECISIONS.md ,  DEPLOYMENT.md ,  README.md  et  INTEGRATION_FRONTEND.md .

Découpage conseillé : modèle et migration → service de clôture → planification systemd → suivi des pièces → API → intégration SMS → dashboard et tests.