# Fp-visites — État des lieux et infrastructure SaaS

Base de décision pour héberger Fp-visites **en SaaS multi-ministères et
multi-organisations**. Procédure d'installation d'un nœud : [`DEPLOYMENT.md`](DEPLOYMENT.md).

---

## 1. Le produit à héberger

| Composant | Techno | Nature |
|---|---|---|
| API | FastAPI / Python 3.12, uvicorn | Async, légère, sans état sauf les fichiers |
| Base | PostgreSQL 16 | Une base unique, cloisonnée par `organization_id` |
| Fichiers | Disque local aujourd'hui | Photos CNI recto/verso, signatures, logos |
| Dashboard admin | Vue.js | Fichiers statiques |
| App mobile | Flutter + **Google ML Kit** | **Lecture MRZ sur le téléphone**, synchro hors-ligne, HTTPS obligatoire |
| Tâches planifiées | Timers systemd | Clôture auto (5 min, idempotente), purge des images |

**L'OCR a quitté le serveur.** Le scan PaddleOCR côté serveur prenait 5 à 15 s.
La lecture MRZ se fait désormais sur le mobile avec ML Kit ; elle est
opérationnelle. L'app envoie à `POST /visits` l'identité déjà extraite et dépose
les photos via `/uploads`.

PaddleOCR est conservé comme **solution de secours, désactivée par défaut**.
L'`ADMIN` de plateforme l'active par le paramètre `server_ocr_enabled`
(`PUT /settings`, action tracée dans l'audit) si ML Kit cesse de fonctionner.
Désactivé, le modèle n'est pas chargé et `POST /ocr/scan` répond
`503 SERVER_OCR_DISABLED`. L'app consulte `GET /ocr/status` pour savoir si elle
peut proposer le scan serveur.

**Modèle multi-tenant actuel :** une instance et une base partagées, isolation
applicative par `TenantScope` (ADR-020). Il existe un rôle `ADMIN` plateforme, et
des rôles `ORG_ADMIN`, `SUPERVISEUR` et `AGENT_CONTROLE` par organisation. Le
branding, l'heure de clôture et l'audit sont déjà gérés par organisation. C'est
une bonne base de SaaS.

---

## 2. Ce qui dimensionne l'infrastructure

- **Le calcul n'est plus un sujet au quotidien.** OCR désactivé, l'API ne fait
  que des lectures/écritures en base et des dépôts de fichiers. Un worker
  consomme ~150 Mo au lieu de ~1,2 Go, et le démarrage passe de ~30 s à quelques
  secondes. De petites VM suffisent, et la charge croît doucement avec le nombre
  de postes. Il faut seulement **garder la RAM pour le jour où le secours
  s'active** : chaque worker charge alors ~1,2 Go.
- **Le stockage d'images est le premier volume.** Pour 100 visites/jour, une
  organisation produit 30 à 150 Go/an (rétention 365 j). Dix ministères
  représentent **0,3 à 1,5 To/an**. Il faut un stockage objet partagé par les
  nœuds API.
- **La disponibilité est le vrai critère.** La base est modeste (quelques Go/an
  tous clients confondus) mais **critique** : une panne arrête tous les
  ministères à la fois. On redonde pour la continuité, pas pour la puissance.
- **Clients publics et données d'identité.** Hébergement **au Sénégal** quasi
  imposé (loi 2008-12, CDP, souveraineté). Chaque ministère est responsable de
  traitement et exigera réversibilité, traçabilité et isolation.

---

## 3. Écarts entre le code actuel et un SaaS

| # | Écart | Pourquoi ça compte en SaaS | Priorité |
|---|---|---|---|
| 1 | Fichiers sur **disque local** (seul `LocalStorageService` existe) | Impossible d'avoir 2 nœuds API ; pas de passage à l'échelle du stockage | **Bloquant** |
| 2 | `/storage/uploads` servi **sans authentification** | Une URL qui fuit expose la CNI ; les clients pourraient s'exposer mutuellement | **Bloquant** |
| 3 | Fichiers **non rangés par organisation** (`mrz/`, `signatures/`, `documents/`) | Export, suppression en fin de contrat et mesure de l'usage par client impossibles | **Bloquant** |
| 4 | **Fiche visiteur partagée** entre organisations (ADR-020) | Données d'identité communes à plusieurs responsables de traitement : à valider juridiquement (CDP) | **Bloquant (décision)** |
| 5 | ~~PaddleOCR chargé à chaque démarrage~~ **Traité** : secours désactivé par défaut, activable par l'`ADMIN` | Les dépendances (~3 Go) restent installées sur chaque nœud pour que le secours soit disponible | — |
| 6 | Migrations jouées au démarrage (`ExecStartPre`) | Course entre nœuds : passer à une étape unique du déploiement | Élevée |
| 7 | Isolation uniquement applicative | Un oubli de filtre fait fuiter des données d'un client vers un autre. Ajouter une **Row-Level Security** PostgreSQL en défense en profondeur | Élevée |
| 8 | Pas de cycle de vie client : export, offboarding, suppression | Réversibilité exigée dans tout contrat SaaS public | Élevée |
| 9 | Identité reçue du mobile **sans contrôle serveur** | Le serveur fait confiance au client. Revalider côté API les checksums ICAO (la lib `mrz` est légère) si l'app transmet la ligne MRZ brute | Moyenne |
| 10 | Pas de métriques d'usage par organisation (visites, Go stockés) | Facturation et quotas | Moyenne |
| 11 | Mot de passe oublié en stub (ADR-006) | Support : impossible à grande échelle sans SMTP | Moyenne |
| 12 | Pas de CI/CD, déploiement manuel | Plusieurs nœuds et environnements : il faut automatiser | Moyenne |
| 13 | Dashboard Vue absent de la config nginx | Rien à servir aujourd'hui | Faible |

**Écarts 1 à 3.** Ils se traitent ensemble. Il faut un `S3StorageService`
(l'interface existe déjà), des clés préfixées `org/<id>/…` et un accès par URL
signée de courte durée. L'app Flutter et le dashboard doivent suivre ce
changement de contrat.

**Écart 5, comportement de l'interrupteur.**
- **Activation** : le worker qui reçoit la requête précharge le modèle en tâche
  de fond. Les autres workers le chargent à leur premier scan (lent, jusqu'à
  ~30 s), ou au redémarrage du service. Pour un basculement propre, il faut
  activer puis faire un `systemctl restart sigv-api`, nœud par nœud.
- **Désactivation** : les scans sont refusés immédiatement. La mémoire n'est
  rendue qu'au redémarrage.
- **Côté nginx** : on garde la location `/api/v1/ocr/` et son timeout de 180 s.

---

## 4. Options d'hébergement

| Option | Verdict |
|---|---|
| **Cloud / datacenter souverain au Sénégal** (datacenter national de Diamniadio / Sénégal Numérique, ou hébergeur local qualifié) | **Recommandé pour la production.** Argument commercial auprès des ministères. |
| Cloud étranger (OVH, Hetzner, Scaleway…) | Acceptable pour **dev/recette** avec données fictives, à éviter en production |
| PaaS (Render, Heroku…) | Techniquement possible sans l'OCR, mais **écarté** : hors territoire, peu de maîtrise des données |
| Kubernetes | Écarté : disproportionné pour la taille de l'équipe et le volume |

**Critères à exiger de l'hébergeur :** VM avec snapshots, **stockage objet
compatible S3**, réseau privé entre VM, load balancer ou IP flottante, sauvegardes
sur un second site, support en heures ouvrées.

---

## 5. Architecture proposée

### Cible de lancement (mutualisée)

```
 Flutter (ML Kit) / Vue.js
        │ HTTPS
        ▼
 ┌─────────────────┐     réseau privé
 │ LB + nginx      │──────────────┬───────────────────┐
 │ app.<domaine>   │              ▼                   ▼
 │ admin.<domaine> │      ┌──────────────┐    ┌──────────────┐
 │ (statiques Vue) │      │ API nœud 1   │    │ API nœud 2   │
 └─────────────────┘      │ uvicorn      │    │ uvicorn      │
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

| Rôle | Quantité | Taille | Remarque |
|---|---|---|---|
| Load balancer / nginx | 1 (ou LB managé) | 2 vCPU / 2 Go | TLS, rate limiting, statiques du dashboard |
| Nœuds API | **2** (pour la disponibilité) | **2 vCPU / 4 Go** | `WEB_CONCURRENCY=2` : largement assez pour une API async, et ~3 Go si le secours OCR s'active (4 workers saturaient la RAM). Timers actifs sur le nœud 1 seulement |
| PostgreSQL | **2** (primaire + réplique) | 4 vCPU / 8 Go, 100 Go SSD | Réplication en streaming, `pgBackRest` (PITR) vers le stockage objet |
| Stockage objet | offre hébergeur (ou MinIO) | 1 To extensible | Versioning, chiffrement, cycle de vie aligné sur la rétention |
| Supervision | 1 | 2 vCPU / 4 Go | Métriques, logs centralisés, sonde `/health/ready` |
| **Recette** | 1 VM tout-en-un | 2 vCPU / 4 Go | Données fictives, même automatisation |

Côté API, ces besoins sont environ **2 fois moindres en CPU** que dans une
version où PaddleOCR tournerait en permanence. La facture se concentre sur la
base et le stockage. Si le secours reste activé durablement, il faut passer les
nœuds API à 4 vCPU / 8 Go.

**Déploiement :** on reste en natif (systemd + nginx, conformément à la décision
actuelle). L'installation est automatisée par **Ansible** et la CI tourne sur
GitHub/GitLab : tests, migration unique, puis redémarrage nœud par nœud derrière
le LB, ce qui donne une mise à jour **sans coupure**. Le redémarrage ne prend
plus que quelques secondes.

**Engagements de service visés (à contractualiser) :**
- Disponibilité de 99,5 % aux heures ouvrées.
- RPO ≤ 15 min (PITR), RTO ≤ 4 h.
- Restauration testée chaque trimestre.

**Côté mobile :** la synchro hors-ligne (`POST /visits/sync`, idempotente) permet
au poste d'accueil de continuer à travailler pendant une coupure réseau ou une
maintenance. C'est un atout de continuité à mettre en avant.

### Deux offres, une seule automatisation

| Offre | Pour qui | Infra |
|---|---|---|
| **Mutualisée** (par défaut) | Ministères, directions, agences | Plateforme ci-dessus, une organisation par client |
| **Dédiée** (premium) | Client exigeant une isolation physique (Défense, Intérieur…) | Même playbook Ansible, pile complète isolée, éventuellement chez le client |

Prévoir l'offre dédiée dès maintenant ne coûte rien si tout est automatisé, et
évite de perdre les clients les plus sensibles. Sans l'OCR, une pile dédiée tient
sur 2 à 3 petites VM : l'offre devient abordable.

### Quand faire évoluer

| Signal | Évolution |
|---|---|
| Latence API en hausse aux heures de pointe | Ajouter un nœud API (horizontal) |
| Plus de 20 à 30 organisations, ou exigence de haute disponibilité | Bascule automatique PostgreSQL (Patroni), LB redondant |
| Volume photos qui explose | Compression plus forte côté Flutter, rétention ajustée par organisation |
| Équipe d'exploitation qui grandit, déploiements fréquents | Rouvrir la question de la conteneurisation |

---

## 6. Feuille de route avant la commercialisation

1. **Code** : écarts 1 à 3 (stockage S3 par organisation + URLs signées), puis 6
   et 7. Côté Flutter, basculer sur `POST /ocr/scan` quand ML Kit échoue et que
   `GET /ocr/status` répond `enabled: true`.
2. **Juridique** : statut de la fiche visiteur partagée (écart 4), modèle de
   contrat SaaS (sous-traitance des données, réversibilité, localisation), dossier CDP.
3. **Produit** : export/offboarding d'une organisation, métriques d'usage, SMTP,
   contrôle serveur de l'identité reçue.
4. **Infra** : choix de l'hébergeur, playbooks Ansible, CI/CD, supervision, test de restauration.
5. **Recette** sur l'infra cible avec un premier ministère pilote.
6. **Docs** : refondre le dimensionnement de `DEPLOYMENT.md` (§1), qui décrit
   encore l'OCR serveur comme le poste dominant.

## 7. Décisions à prendre

- [ ] Hébergeur souverain retenu (et disponibilité d'un stockage objet S3)
- [ ] Fiche visiteur : partagée entre organisations, ou cloisonnée par organisation
- [ ] Offre dédiée proposée dès le lancement, ou plus tard
- [ ] Domaines : `app.` / `admin.` uniques, ou sous-domaine par client
- [ ] Engagements de service (disponibilité, RPO/RTO) inscrits au contrat
- [ ] Modèle de facturation (par organisation, par poste, par volume de visites)
