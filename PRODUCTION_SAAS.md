# Fp-visites — Production SaaS

Architecture cible et feuille de route pour exploiter Fp-visites **en SaaS
multi-ministères et multi-organisations**. La procédure d'installation est dans
[`DEPLOYMENT.md`](DEPLOYMENT.md), le pourquoi des choix techniques dans
[`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md).

État au 16/10/2026.

---

## 1. Ce qu'on héberge, et ce qui le dimensionne

| Composant | Techno | Nature |
|---|---|---|
| API | FastAPI / Python 3.12, uvicorn | Async, légère, sans état sauf les fichiers |
| Base | PostgreSQL 16 | Une base unique, cloisonnée par `organization_id` (ADR-020, ADR-023) |
| Fichiers | Disque local, rangés `org/<id>/` | Photos CNI, signatures, logos ; URLs signées (ADR-024) |
| Dashboard admin | Vue.js | Fichiers statiques servis par nginx |
| App mobile | Flutter + Google ML Kit | Lecture MRZ sur le téléphone, synchro hors-ligne idempotente |
| Tâches planifiées | Timers systemd | Clôture auto, purge des images, purge des sessions |

**Ce qui dimensionne :**
- **Le calcul pèse peu.** La lecture MRZ est faite par le mobile. PaddleOCR reste un secours **désactivé par défaut** : l'`ADMIN` l'active par `server_ocr_enabled`, et chaque worker charge alors ~1,2 Go. Il faut garder cette RAM disponible.
- **Le stockage d'images est le premier volume.** Il faut compter 30 à 150 Go par an pour 100 visites/jour (rétention 365 j). Dix ministères représentent 0,3 à 1,5 To/an, d'où le besoin d'un stockage objet.
- **La disponibilité est le vrai critère.** La base est petite mais commune à tous les clients : une panne arrête tous les ministères à la fois.
- **Ce sont des données d'identité de clients publics.** L'hébergement au Sénégal est quasi imposé (loi 2008-12, CDP). Chaque ministère, responsable de traitement, exigera réversibilité et isolation.

---

## 2. Hébergement et architecture cible

| Option | Verdict |
|---|---|
| **Datacenter / cloud souverain au Sénégal** (Diamniadio / Sénégal Numérique, ou hébergeur local qualifié) | **Production** |
| Cloud étranger (OVH, Hetzner, Scaleway…) | Dev et recette, données fictives uniquement |
| PaaS, Kubernetes | Écartés : hors territoire pour l'un, disproportionné pour l'autre |

**À exiger de l'hébergeur :**
- des VM avec snapshots ;
- un **stockage objet compatible S3** ;
- un réseau privé entre les VM ;
- un load balancer ou une IP flottante ;
- des sauvegardes sur un second site ;
- un support en heures ouvrées.

```
 Flutter (ML Kit) / Vue.js
        │ HTTPS
        ▼
 ┌─────────────────┐     réseau privé
 │ LB + nginx      │──────────────┬───────────────────┐
 │ api.<domaine>   │              ▼                   ▼
 │ admin.<domaine> │      ┌──────────────┐    ┌──────────────┐
 └─────────────────┘      │ API nœud 1   │    │ API nœud 2   │
                          └──────┬───────┘    └──────┬───────┘
                                 ├───────────┬───────┘
                                 ▼           ▼
                   ┌──────────────────┐   ┌──────────────────────┐
                   │ PostgreSQL 16    │   │ Stockage objet S3    │
                   │ primaire+réplique│   │ org/<id>/…, chiffré  │
                   │ PgBouncer, PITR  │   └──────────────────────┘
                   └──────────────────┘
          Supervision : Prometheus · Grafana · Loki · alertes
```

| Rôle | Qté | Taille | Remarque |
|---|---|---|---|
| Load balancer / nginx | 1 | 2 vCPU / 2 Go | TLS, limitation de débit, dashboard, vérification des URLs signées |
| Nœuds API | 2 | 2 vCPU / 4 Go | `WEB_CONCURRENCY=2` ; 4 vCPU / 8 Go si le secours OCR reste actif. Timers sur le nœud 1 seulement |
| PostgreSQL | 2 | 4 vCPU / 8 Go, 100 Go SSD | Primaire + réplique, `pgBackRest` (PITR) |
| Stockage objet | — | 1 To extensible | Versioning, chiffrement, cycle de vie aligné sur la rétention |
| Supervision | 1 | 2 vCPU / 4 Go | Métriques, logs, alertes |
| Recette | 1 | 2 vCPU / 4 Go | Tout-en-un, données fictives |

**Le déploiement reste natif** (systemd + nginx, sans Docker). Il est automatisé
par Ansible et mis à jour nœud par nœud derrière le load balancer, donc sans
coupure (`deploy/scripts/deploy.sh`).

**Engagements de service visés :**
- disponibilité de 99,5 % aux heures ouvrées ;
- RPO ≤ 15 min ;
- RTO ≤ 4 h ;
- restauration testée chaque trimestre.

**Deux offres, une seule automatisation :**
- **Mutualisée** (par défaut) : la plateforme ci-dessus, une organisation par client.
- **Dédiée** (premium) : une pile isolée, sur 2 à 3 petites VM, pour un client qui exige l'isolation physique.

**Quand faire évoluer :**

| Signal | Évolution |
|---|---|
| Latence en hausse aux heures de pointe | Ajouter un nœud API |
| Plus de 20 à 30 organisations, ou exigence de haute disponibilité | Bascule automatique PostgreSQL (Patroni), LB redondant |
| Volume de photos | Compression côté Flutter, rétention par organisation |
| Équipe d'exploitation qui grandit | Rouvrir la question de la conteneurisation |

---

## 3. Déployer la version actuelle

La version actuelle tourne sur **un nœud API**. Deux nœuds demandent en plus un
volume de fichiers partagé, tant que S3 (1.6) n'est pas branché.

1. **Sauvegarder la base** : `sudo -u postgres pg_dump -Fc sigv > avant-c6e8a2d47f19.dump`.
2. **Compléter `.env`** depuis [`.env.production.example`](.env.production.example) :
   - `STORAGE_URL_SIGNING_KEY` est **obligatoire**, sinon l'API refuse de démarrer ;
   - les réglages `DB_POOL_*` ont des défauts sûrs.
3. **Créer `/etc/nginx/sigv-storage-key.conf`** avec la même clé (DEPLOYMENT.md, « Accès aux fichiers »).
4. **Recopier les fichiers modifiés** :
   - nginx : `00-shared.conf`, `sigv-app.inc`, `30-admin.conf` ;
   - systemd : `sigv-api.service` (qui ne migre plus au démarrage) et `sigv-purge-sessions.*`.
5. **Déployer** : `sudo bash deploy/scripts/deploy.sh <tag> --migrate`. La migration
   `c6e8a2d47f19` fusionne les éventuelles fiches visiteurs en double.
6. **Activer** `sigv-purge-sessions.timer`.

**Ce que les clients vont voir :**
- une reconnexion unique pour les comptes dont le refresh token précède le suivi des sessions ;
- des URLs de fichiers signées, à afficher et renvoyer telles que reçues (`410` au-delà d'une heure : relire la visite) ;
- de nouveaux refus : `403 CROSS_ORGANIZATION_FORBIDDEN` et `400` pour une URL hors du stockage.

Le détail pour les équipes Flutter et Vue est dans [`INTEGRATION_FRONTEND.md`](INTEGRATION_FRONTEND.md), §14.

---

## 4. Feuille de route

**Priorités** :
- **P0** : bloquant avant le premier client SaaS.
- **P1** : à faire avant l'ouverture à de nombreuses organisations.
- **P2** : pour la croissance.

**Statut** : ✅ fait · 🟡 en partie · ⬜ à faire · ⚖️ décision attendue.

**Effort** : S ≤ 2 j · M ≤ 1 sem · L > 1 sem.

### 4.1 Déjà fait (octobre 2026)

| # | Correction | Réf. |
|---|---|---|
| 1.1 | Une visite ne référence que le service, le motif, les fichiers et la fiche de **sa** organisation, y compris en synchro et en correction | ADR-023 |
| 1.2 | « Déjà présent » est jugé par organisation, sans fuite de la visite d'autrui | ADR-023 |
| 1.3 | `client_reference` est unique par organisation | ADR-023 |
| 1.4 | Le refresh token est rattaché à sa session : révoquer une tablette coupe aussi l'ancien token | ADR-023 |
| 1.5 | Les photos ne sont servies que sur URL signée et datée (nginx `secure_link`) | ADR-024 |
| 1.11 | Débit limité par jeton plutôt que par IP (NAT des ministères) | `00-shared.conf` |
| 1.12 | Rotation de la clé JWT sans déconnexion (`JWT_SECRET_KEY_PREVIOUS`) | DEPLOYMENT.md |
| 1.13 | Temps de réponse identique pour un identifiant inconnu | — |
| 2.1 | Unicité de la pièce posée en base, doublons fusionnés, collision rejouée | ADR-010, ADR-023 |
| 2.2 | Index trigrammes `pg_trgm` pour la recherche de visiteurs | migration `c6e8a2d47f19` |
| 2.4 | Pool de connexions explicite, `statement_timeout`, mode PgBouncer | DEPLOYMENT.md |
| 3.13 | OCR serveur de secours désactivable (backend) | — |
| 5.4 | Vhost nginx du dashboard avec CSP stricte | `30-admin.conf.example` |
| 5.6 | Migrations jouées une fois par déploiement, unités systemd compatibles avec une base distante | `deploy.sh` |
| 5.7 | Timers sur un seul nœud (documenté) | DEPLOYMENT.md §4 |
| 5.9 | CI : lint, tests, migrations sur PostgreSQL 16 | `.github/workflows/ci.yml` |
| 5.11 | `/health/ready` vérifie aussi le stockage | — |

### 4.2 Sécurité et isolation

| # | Prio | Statut | À faire | Effort |
|---|---|---|---|---|
| 1.6 | **P0** | 🟡 | **Stockage objet S3.** Fait : les nouveaux fichiers sont rangés `org/<id>/` et contrôlés par organisation. Reste : `S3StorageService`, la migration des fichiers existants, puis les URLs présignées S3. Prérequis pour avoir deux nœuds. | M |
| 1.7 | **P0** | ⚖️ | **Identifiant de connexion unique sur tout le parc** : deux ministères ne peuvent pas avoir chacun un `agent001`. À choisir : identifiant + code d'organisation, e-mail, ou préfixe. Impacte les clients. | M |
| 1.8 | **P0** | ⚖️ | **Fiche visiteur commune à toutes les organisations** : les coordonnées saisies par B écrasent celles de A. À valider par la CDP, puis : une fiche par organisation, ou un noyau commun avec des contacts propres. | M-L |
| 1.9 | P1 | ⬜ | Row-Level Security PostgreSQL en défense en profondeur. | L |
| 1.10 | P1 | ⚖️ | **Mot de passe de 6 caractères minimum**, abaissé volontairement depuis 12 (commit 8ee97ed). Recommandé : 10 caractères minimum et liste de mots de passe courants refusés, double authentification TOTP pour `ADMIN` et `ORG_ADMIN`. | M |
| 1.14 | P2 | ⬜ | NIN et numéro de pièce en clair en base : chiffrement du disque au minimum. | M |
| 1.15 | P1 | ⬜ | Test d'intrusion avant l'ouverture, avec un scénario dédié au cloisonnement. | — |
| 1.16 | P2 | ⬜ | L'identité envoyée par le mobile n'est pas vérifiée : revérifier côté serveur les sommes de contrôle MRZ si l'app transmet la ligne brute. | S |

### 4.3 Données et performance

| # | Prio | Statut | À faire | Effort |
|---|---|---|---|---|
| 2.3 | P1 | ⬜ | Export CSV en streaming ou en tâche de fond. Il est borné à 50 000 lignes d'ici là : ne pas relever ce plafond avant. | M |
| 2.5 | P1 | 🟡 | Fait : purge des sessions. Reste : la rétention de `audit_logs`, à fixer avec la CDP. | S |
| 2.6 | P1 | ⚖️ | Durée de conservation des visites et visiteurs, à définir avec la CDP, puis anonymiser ou purger. | M |
| 2.7 | P1 | 🟡 | Fait : migrations testées sur PostgreSQL en CI. Reste : la suite d'intégration sur PostgreSQL. | S |
| 2.8 | P1 | ⬜ | Test de charge (k6 ou Locust) : N organisations × M postes, pic du matin, synchro, export. | M |
| 2.9 | P2 | ⬜ | Fuseau horaire par organisation (tout est en UTC, ce qui est correct au Sénégal). | S |
| 2.10 | P2 | ⬜ | Profiler les listes et agréger en SQL les statistiques de la vue globale. | M |

### 4.4 Fonctionnalités SaaS

| # | Prio | Statut | À faire | Effort |
|---|---|---|---|---|
| 3.1 | **P0** | ⚖️ | Réversibilité : export complet et suppression définitive d'une organisation en fin de contrat (le principe « aucune suppression physique » est à amender). | M |
| 3.2 | P1 | ⚖️ | Sites et postes d'accueil multiples par organisation ; hiérarchie ministère → directions ? | M-L |
| 3.3 | P1 | ⬜ | Abonnement et quotas : offre, date de fin, maximum de postes et de comptes, stockage. | M |
| 3.4 | P1 | ⬜ | Mesure de l'usage par organisation (visites, comptes, Go). | M |
| 3.5 | P1 | ⬜ | Console de pilotage du super-admin. | M |
| 3.6 | P1 | ⬜ | Mot de passe oublié : fournisseur SMTP (stub aujourd'hui, ADR-006). | M |
| 3.7 | P1 | ⬜ | Version minimale de l'app mobile imposée par l'API. | S |
| 3.8 | P1 | ⬜ | Droits des visiteurs : accès, rectification, effacement, tracés. | M |
| 3.9 | P2 | ⬜ | **SMS pour pièce oubliée.** Le reste du besoin (clôture auto, fiches de suivi) est livré, et `sms_sent_at` existe déjà. Il manque : `POST /forgotten-document-cases/{id}/send-sms` ; un `SmsService` indépendant du fournisseur (numéro validé, envoi idempotent, état envoyé/échec, relance possible, numéro masqué dans les journaux, secrets dans `.env`) ; côté dashboard, confirmation avant envoi et bouton inactif sans téléphone. | M |
| 3.10 | P2 | ⬜ | Export PDF (`501` aujourd'hui), ou retrait de l'option. | S |
| 3.11 | P2 | ⬜ | Support : lecture seule « en tant que » une organisation, tracée. | M |
| 3.12 | P2 | ⬜ | Retirer `mrz_image_url`, déprécié. | S |

### 4.5 Clients Flutter et Vue

| # | Prio | Statut | À faire |
|---|---|---|---|
| 4.1 | **P0** | ⬜ | Vérifier en recette, clé de signature active : les URLs de fichiers sont affichées et renvoyées telles que reçues, et un `410` déclenche une relecture. |
| 4.2 | **P0** | ⬜ | Flutter en HTTPS uniquement (retirer `usesCleartextTraffic`) ; `receiveTimeout` ≥ 60 s sur le scan. |
| 4.3 | P1 | ⬜ | Gérer `CROSS_ORGANIZATION_FORBIDDEN`, `SERVER_OCR_DISABLED`, 429, et un `401` au refresh. Flutter : bascule sur le scan serveur si ML Kit échoue. Vue : case `server_ocr_enabled`. |
| 4.4 | P1 | ⬜ | Épinglage du certificat TLS ; tokens dans le Keystore / Keychain. |
| 4.5 | P1 | 🟡 | Dashboard : vhost prêt ; reste le build de production et `CORS_ORIGINS`. |
| 4.6 | P1 | ⬜ | Flotte de tablettes : MDM, mode kiosque, effacement à distance. |
| 4.7 | P2 | ⬜ | Remontée des plantages mobiles, sans donnée d'identité. |

### 4.6 Infrastructure et exploitation

| # | Prio | Statut | À faire | Effort |
|---|---|---|---|---|
| 5.1 | **P0** | ⚖️ | Choisir l'hébergeur souverain. | — |
| 5.2 | **P0** | ⬜ | Sauvegardes PITR et fichiers, chiffrées, sur un second site ; **restauration testée**. | M |
| 5.3 | **P0** | ⬜ | Environnement de recette distinct. | S |
| 5.5 | P1 | ⬜ | Deux nœuds API, PostgreSQL primaire + réplique, PgBouncer (après 1.6). | M |
| 5.8 | P1 | ⬜ | Automatisation Ansible, secrets dans Ansible Vault. | L |
| 5.9b | P1 | ⬜ | Rendre la CI obligatoire pour fusionner sur `main`. | S |
| 5.10 | P1 | ⬜ | Observabilité : Prometheus, Grafana, logs centralisés, suivi d'erreurs, alertes. | M |
| 5.12 | P1 | ⬜ | Durcissement des machines : SSH par clé, `fail2ban`, mises à jour automatiques, VPN ou bastion. | S |
| 5.13 | P2 | ⬜ | Plan de reprise d'activité, exercice trimestriel. | S |
| 5.14 | P2 | ⬜ | Patroni et load balancer redondant. | L |

### 4.7 Conformité, contrats, organisation

| # | Prio | Statut | À faire |
|---|---|---|---|
| 6.1 | **P0** | ⚖️ | Dossier CDP : déclaration ou autorisation, registre des traitements, analyse d'impact. |
| 6.2 | **P0** | ⚖️ | Contrat SaaS type : sous-traitance, localisation, réversibilité, conservation, notification des violations. |
| 6.4 | P1 | ⚖️ | Engagements de service et plan d'assurance sécurité. |
| 6.5 | P1 | ⬜ | Mention d'information des visiteurs au poste d'accueil. |
| 6.6 | P1 | ⬜ | Procédure d'incident et de violation de données. |
| 6.7 | P1 | ⬜ | Organisation du support : niveaux, astreinte, tickets. |
| 6.8 | P2 | ⚖️ | Modèle de facturation (s'appuie sur 3.3 et 3.4). |

### 4.8 Documentation

| # | Prio | Statut | À faire |
|---|---|---|---|
| 7.2 | P1 | 🟡 | Procédures d'exploitation. Faites : déploiement, rotation des clés. Restent : intégrer une organisation, restaurer, gérer un incident. |
| 7.3 | P1 | ⬜ | Guides utilisateurs : super-admin, `ORG_ADMIN`, agent d'accueil. |
| 7.4 | P2 | ⬜ | Contrat d'API versionné (OpenAPI) et politique de dépréciation. |
| 7.5 | P2 | ⬜ | `ruff format` sur tout le dépôt, imposé en CI. |

---

## 5. Décisions attendues et ordre de réalisation

**Décisions** (⚖️) :
- **Hébergeur** (5.1).
- **Identifiant de connexion** (1.7).
- **Fiche visiteur partagée** (1.8).
- **Mot de passe** (1.10).
- **Réversibilité** (3.1).
- **Durées de conservation** (2.6), avec la CDP.
- **Sites** (3.2).
- **Domaines** : `api.` / `admin.` uniques, ou sous-domaine par client.
- **Offre dédiée** : dès le lancement, ou plus tard.
- **Engagements de service** (6.4) et **facturation** (6.8).

**Ordre :**
1. Déployer la version actuelle en recette (§3), vérifier les clients (4.1, 4.2).
2. Prendre les décisions ci-dessus, en parallèle.
3. Stockage objet (1.6), dès l'hébergeur choisi.
4. Socle d'exploitation : 5.2, 5.3, 5.10, 5.12, puis 5.5 et 5.8.
5. Pilote sur un ministère : test de charge (2.8), test d'intrusion (1.15).
6. Le reste des P1, puis les P2.

**Critère de sortie pour vendre :**
- Tous les P0 sont clos.
- Une restauration complète a été testée.
- Le test d'intrusion n'a laissé aucune faille critique ouverte.
- Le pilote a tourné un mois sans incident majeur.
