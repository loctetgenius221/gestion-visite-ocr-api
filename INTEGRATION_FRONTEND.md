# Guide d'intégration front — évolutions de septembre et octobre 2026

> **Octobre 2026** : URLs de fichiers signées, refus des références d'une autre
> organisation, sessions, OCR serveur de secours. Voir le §14.

Destiné aux équipes **app mobile (Flutter)** et **dashboard web**.

Base URL : `/api/v1` · Authentification : `Authorization: Bearer <access_token>`
(inchangée) · Format d'erreur : `{ "error_code", "message", "details" }` (inchangé).

---

## 1. En un coup d'œil

| # | Évolution | Mobile | Dashboard | Rupture ? |
|---|---|---|---|---|
| 1 | Le NIN peut contenir une lettre | ✅ | ✅ | Si vous validez « 13 chiffres » |
| 2 | Réenregistrer un visiteur connu sans rescan | ✅ | — | Non, additif |
| 3 | Une personne déjà présente ne peut plus entrer 2× | ✅ | — | **Oui**, nouveau 409 |
| 4 | Photos recto + verso en saisie manuelle | ✅ | ✅ (lecture) | Non, additif |
| 5 | Mot de passe agent saisi au lieu de généré | — | ✅ | Non, existait déjà |
| 6 | Réglage de conservation des photos | — | ✅ | Non, additif |
| 7 | La personne rencontrée devient facultative | ✅ | ✅ | Non, mais `agent` peut être `null` en lecture |
| 8 | Clôture automatique des visites | — | ✅ | Additif, mais `checkout_origin` doit être affiché |
| 9 | Suivi des pièces potentiellement oubliées | — | ✅ | Nouvelles routes et nouveaux statuts |
| 10 | Branding et thème par organisation | — | ✅ | Nouvelles routes et champs |
| 11 | `?organization_id=` sur les listes et les statistiques | — | ✅ | Non, additif |
| 12 | Un paramètre de requête inconnu est refusé | ✅ | ✅ | **Oui**, `400` là où c'était ignoré |
| 13 | Import en masse des services et des agents (.xlsx) | — | ✅ | Non, additif |

### Les quatre seuls points qui cassent quelque chose

1. **`POST /visits` peut désormais répondre `409 VISITOR_ALREADY_PRESENT`.** Si vous
   ne traitez pas ce code, l'agent verra une erreur générique là où il devrait voir
   « déjà présent depuis 10h30 — clôturer ? ». **C'est le point à traiter en premier.**
2. **Une validation client « NIN = 13 chiffres » rejettera des cartes valides.**
   Le champ est alphanumérique.
3. **`agent` peut valoir `null` dans toute `VisitRead`.** Un `visit.agent.name` sans
   garde plante. La rupture est *conditionnelle* : elle ne se déclenche qu'à partir
   du moment où une visite sans personne rencontrée existe en base — donc dès que le
   dashboard ou une tablette à jour en crée une. Un client non corrigé plantera alors
   en lisant le registre, **y compris sur des visites qu'il n'a pas créées**.
   Traitez-le avant d'ouvrir la fonctionnalité aux agents.
4. **Un paramètre de requête non déclaré répond désormais `400 VALIDATION_ERROR`**
   au lieu d'être ignoré. Purgez les paramètres décoratifs — anti-cache `?_=169…`,
   `utm_*`, restes de copier-coller : ils faisaient jusqu'ici une requête valide.
   Détail et format de la réponse en §1.2.

Tout le reste est additif : le code existant continue de fonctionner à l'identique.
Les sections 8 à 10 décrivent les fonctionnalités ajoutées pour le dashboard.

## 1.1 Authentification et profil

### Connexion

Pour l'application mobile et le dashboard, utiliser de préférence la connexion
JSON :

```http
POST /api/v1/auth/login
Content-Type: application/json
```

```json
{
  "identifiant": "admin-test",
  "mot_de_passe": "AdminTest@2026"
}
```

La réponse contient `access_token`, `refresh_token`, `expires_in` et le profil
`user`. Le bearer token doit être envoyé sur les routes protégées :

```http
Authorization: Bearer <access_token>
```

`POST /api/v1/auth/token` accepte le même flux au format
`application/x-www-form-urlencoded` avec `username` et `password`. Il est prévu
pour Swagger/OAuth2 ; Flutter doit utiliser `/auth/login`.

### Renouvellement et déconnexion

```http
POST /api/v1/auth/refresh
Content-Type: application/json
{ "refresh_token": "…" }
```

La réponse peut contenir un nouveau `refresh_token`. Quand il est présent, le
client doit remplacer l'ancien immédiatement.

```http
POST /api/v1/auth/logout
Content-Type: application/json
{ "refresh_token": "…" }
```

Le profil courant est disponible sur les deux routes suivantes :

```http
GET /api/v1/auth/me
GET /api/v1/me
```

Le champ `user.organization_id` est `null` pour `ADMIN` et renseigné pour les
comptes rattachés à une organisation.

## 1.2 Matrice d'autorisation et périmètre organisationnel

Le frontend doit piloter les menus à partir de `user.role`, mais le backend reste
l'autorité finale. Un appel non autorisé renvoie `403` avec
`error_code: "FORBIDDEN"`.

| Rôle | Périmètre | Accès principal |
|---|---|---|
| `ADMIN` | Plateforme entière | Organisations, utilisateurs, référentiels, visites, paramètres, audits, clôture automatique, pièces oubliées, branding et statistiques globales |
| `ORG_ADMIN` | `user.organization_id` uniquement | Utilisateurs, référentiels, visites, pièces oubliées, branding et statistiques de son organisation |
| `SUPERVISEUR` | `user.organization_id` uniquement | Opérations de visite proches de l'agent, statistiques et consultation des agents ; pas de gestion des comptes |
| `AGENT_CONTROLE` | Opérations terrain | Référentiels en lecture, création/recherche de visiteurs, création et clôture de visites |

`SUPER_ADMIN` **n'existe plus** : la valeur a été retirée de l'énumération. Elle
n'a jamais été un rôle utilisable — le type PostgreSQL ne l'a jamais contenue. Le
rôle global est `ADMIN`.

### Le rattachement est obligatoire

`ADMIN` est le **seul** rôle dont `organization_id` vaut `null` : c'est ce qui lui
donne la vue d'ensemble. Pour tous les autres, l'organisation est obligatoire et
le backend la refuse autrement :

- créer un compte non-`ADMIN` sans `organization_id` renvoie
  `400 ORGANIZATION_REQUIRED` — affichez un sélecteur d'organisation, pas un
  message d'accès refusé ;
- créer un `ADMIN` **avec** un `organization_id` renvoie `403 FORBIDDEN` ;
- un compte non-`ADMIN` resté sans rattachement en base est refusé dès sa
  première requête avec `403 UNSCOPED_ACCOUNT`. Si vous le rencontrez, le compte
  est à corriger côté administration, ce n'est pas un défaut de droits.

### Franchir la frontière est refusé, jamais ignoré

Demander explicitement les données d'une autre organisation renvoie
`403 CROSS_ORGANIZATION_FORBIDDEN` — distinct de `FORBIDDEN`, qui signale un rôle
insuffisant. Le paramètre n'est pas silencieusement réécrit sur l'organisation de
l'appelant : un filtre ignoré sans le dire donnerait l'illusion d'avoir consulté
ce qui était demandé.

Règles de sécurité à respecter côté interface :

- ne jamais permettre à `ORG_ADMIN` de sélectionner une autre organisation ;
- ne jamais afficher la gestion des comptes à `SUPERVISEUR` ou `AGENT_CONTROLE` ;
- ne jamais proposer la création d'un `ADMIN` depuis l'interface `ORG_ADMIN` ;
- envoyer `organization_id` pour `ORG_ADMIN`, `SUPERVISEUR` et `AGENT_CONTROLE`
  lors de la création d'un compte ; le laisser à `null` pour `ADMIN` ;
- pour une ressource d'une autre organisation, traiter `403` comme un accès
  interdit, pas comme une ressource inexistante.

### Ce que le cloisonnement change dans les listes

Les routes suivantes ne renvoient plus que les données de l'organisation de
l'appelant. Si votre interface s'appuyait sur une vue globale, elle changera :

| Route | Avant | Maintenant |
|---|---|---|
| `GET /visitors` | tous les visiteurs de la plateforme | ceux **déjà venus dans l'organisation** |
| `GET /services` | tous les services | ceux de l'organisation |
| `GET /agents` | tout l'annuaire | l'annuaire de l'organisation |
| `GET /purposes` | tous les motifs | motifs communs + motifs de l'organisation |
| `GET /audit-logs` | tout le journal | le journal de l'organisation |
| `GET /settings` | valeurs de plateforme | valeurs effectives de l'organisation |

`GET /visitors` mérite une note : la fiche visiteur reste **commune** à toutes les
structures — une même personne peut se présenter dans plusieurs — mais on ne la
retrouve que là où elle s'est effectivement présentée. Une personne dont la seule
visite a été supprimée disparaît donc de la recherche ; son identité est
retrouvée au scan de sa pièce, par son numéro de document.

### Filtrer sur une organisation — `?organization_id=`

Les routes de lecture acceptent `organization_id` en paramètre de requête. Le
paramètre existe pour **tous** les rôles, mais un seul peut le faire varier :

| Rôle | Effet de `?organization_id=X` |
|---|---|
| `ADMIN` | filtre sur `X` ; absent, la réponse couvre toute la plateforme |
| tout autre rôle | accepté si `X` est le sien, `403 CROSS_ORGANIZATION_FORBIDDEN` sinon |

Le dashboard peut donc envoyer le même paramètre quel que soit le rôle connecté,
sans brancher son code sur le rôle : ce que voit un `ORG_ADMIN` ne dépend pas de
ce qu'il demande.

Routes concernées :

| Domaine | Routes |
|---|---|
| Registre | `GET /visits`, `GET /visits/export`, `GET /visits/auto-closed` |
| Tableau de bord | `GET /dashboard/stats` et les six `GET /dashboard/stats/*` (`timeseries`, `by-service`, `by-purpose`, `peak-hours`, `avg-duration`, `top-agents`) |
| Comptes | `GET /users` |
| Référentiels | `GET /services`, `GET /agents`, `GET /purposes` |
| Visiteurs | `GET /visitors` |
| Déjà en place | `GET /audit-logs`, `GET /forgotten-document-cases`, `GET /settings` |

Deux précisions :

- `GET /organizations/{id}/users` reste disponible et rend exactement le même
  résultat que `GET /users?organization_id={id}`. Les deux sont supportées :
  prenez celle qui correspond à votre navigation.
- `GET /purposes?organization_id=X` rend ce que **verrait** cette structure : ses
  motifs propres **et** les motifs communs à la plateforme. Sans le paramètre,
  l'`ADMIN` voit tout le référentiel, motifs de toutes les structures compris.

`AgentRead` expose désormais `organization_id`, hérité du service de rattachement
de l'agent. Le champ est présent partout où un agent apparaît, y compris imbriqué
dans `VisitRead.agent`.

### ⚠️ Un paramètre de requête inconnu est maintenant refusé

Jusqu'ici, un paramètre non déclaré par la route était **silencieusement ignoré**
et la réponse restait `200`. C'est ce qui a fait passer inaperçu l'absence de
`organization_id` : `GET /visits?organization_id=…` répondait `200` avec le
registre entier, sans rien signaler.

Toute route rejette désormais ce qu'elle ne sait pas honorer :

```http
GET /api/v1/visits?organisation_id=7e21…
```

```json
{
  "error_code": "VALIDATION_ERROR",
  "message": "Les données envoyées sont invalides.",
  "details": [
    {
      "type": "unexpected_query_parameter",
      "loc": ["query", "organisation_id"],
      "msg": "Paramètre de requête inconnu pour cette route.",
      "input": "7e21…",
      "ctx": { "parametres_acceptes": ["agent_id", "date_from", "…", "organization_id"] }
    }
  ]
}
```

Le code est **`400 VALIDATION_ERROR`**, pas `422` : la spec §5.6 réserve le `422`
aux échecs métier, et une query string fautive n'en est pas un. C'est déjà le
code rendu par `?page=0` ou `?statut=inconnu` — un seul cas à traiter, pas deux.

`ctx.parametres_acceptes` liste ce que la route accepte réellement : de quoi
distinguer d'un coup d'œil la faute de frappe du filtre que le backend n'expose
pas encore.

Ce que cela implique côté client :

- retirer les paramètres décoratifs — anti-cache (`?_=169…`), traçage (`utm_*`),
  restes de copier-coller. Ils font maintenant échouer la requête.
- un `400` sur une route qui marchait hier signale un paramètre mal orthographié,
  pas une régression serveur : lisez `details[0].loc`.

## 1.3 Gestion des comptes utilisateurs

Toutes les routes suivantes nécessitent `ADMIN` ou `ORG_ADMIN`. Les résultats
d'un `ORG_ADMIN` sont automatiquement limités à son organisation, même si le
client tente d'envoyer un autre filtre `organization_id`.

| Méthode | Route | Usage |
|---|---|---|
| `GET` | `/api/v1/users` | Liste paginée (`role`, `status`, `search`, `sort`, `page`, `page_size`) |
| `POST` | `/api/v1/users` | Créer un compte |
| `GET` | `/api/v1/users/{user_id}` | Détail d'un compte |
| `PUT` | `/api/v1/users/{user_id}` | Modifier nom, poste, rôle ou organisation |
| `PATCH` | `/api/v1/users/{user_id}/status` | Activer/désactiver |
| `POST` | `/api/v1/users/{user_id}/reset-password` | Réinitialiser le mot de passe |
| `POST` | `/api/v1/users/{user_id}/unlock` | Lever un verrouillage |
| `GET` | `/api/v1/users/{user_id}/sessions` | Lister les sessions actives |
| `DELETE` | `/api/v1/users/{user_id}/sessions/{session_id}` | Révoquer une session |

Exemple de création :

```json
{
  "nom": "Awa Ndiaye",
  "identifiant": "awa.ndiaye",
  "organization_id": "7e21…",
  "role": "SUPERVISEUR",
  "poste": "Superviseure",
  "mot_de_passe": "MotDePasseSolide2026"
}
```

Si `mot_de_passe` est omis, le serveur le génère et le renvoie une seule fois
dans la réponse `mot_de_passe`. Un mot de passe fourni par le frontend n'est
jamais renvoyé en clair.

Contraintes de création et de modification :

- minimum du mot de passe : 6 caractères ; maximum : 128 ;
- `ADMIN` est global et ne peut pas être rattaché à une organisation ;
- seul `ADMIN` peut attribuer le rôle `ADMIN` ;
- `ORG_ADMIN` peut gérer les comptes `ORG_ADMIN`, `SUPERVISEUR` et
  `AGENT_CONTROLE` de sa propre organisation ;
- `SUPER_ADMIN` est refusé pour les nouvelles créations et modifications ;
- la désactivation révoque immédiatement les sessions du compte ;
- le dernier administrateur actif ne peut pas être désactivé ou rétrogradé ;
- un administrateur ne peut pas modifier son propre compte via ces opérations
  sensibles.

La réponse de compte expose notamment `id`, `organization_id`, `nom`,
`identifiant`, `poste`, `role`, `is_active`, les compteurs de verrouillage et les
dates de connexion. Elle n'expose jamais le hash du mot de passe.

---

## 2. Le NIN est alphanumérique

Le NIN sénégalais n'est **pas** une suite de 13 chiffres. Son code d'état civil peut
porter une lettre — observé sur carte réelle : `NIN 2 K05 2012 00108`.

```
2   K05   2012   00108
│    │      │      └── numéro d'ordre, 5 chiffres
│    │      └───────── année, 4 chiffres
│    └──────────────── code d'état civil, 3 caractères ALPHANUMÉRIQUES
└───────────────────── sexe : 1 (homme) ou 2 (femme)
```

**Ce qui change pour vous :**

- `POST /ocr/scan` → `fields.nin` peut valoir `"2K05201200108"`. Le champ fait
  toujours 13 caractères, sans séparateurs.
- Toute validation ou tout masque de saisie côté client doit passer de
  « 13 chiffres » à **13 caractères alphanumériques** :

  ```
  ^[12][A-Z0-9]{3}[0-9]{9}$
  ```

- À l'envoi, le serveur **normalise** : majuscules, espaces et séparateurs retirés.
  `"2 k05 2012 00108"` est accepté et stocké `"2K05201200108"`. Vous pouvez donc
  laisser l'agent saisir avec les espaces de la carte.

> ⚠️ Rappel de l'ADR-014, toujours d'actualité : **ne jamais retomber sur
> `numero_document` quand `nin` est `null`.** Ça transforme une donnée absente en
> donnée fausse. `nin: null` doit rester visible comme tel.

---

## 3. Enregistrer un visiteur déjà venu, sans rescanner sa pièce

Parcours cible, en deux appels :

### 3.1 Retrouver la fiche — `GET /visitors`

```http
GET /api/v1/visitors?search=diop&page=1&page_size=20
Authorization: Bearer <token>
```

`search` cherche sur **nom, prénom, numéro de document et NIN**. Les numéros sont
comparés compactés : l'agent peut taper `2 K05 2012 00108` avec ses espaces.

**Trois caractères minimum**, sinon `400 VALIDATION_ERROR`. Ne déclenchez donc pas
la requête avant le 3ᵉ caractère (et pensez à un *debounce* de ~300 ms).

Réponse — enveloppe de pagination habituelle :

```json
{
  "items": [
    {
      "id": "6f1c…",
      "prenom": "Aminata",
      "nom": "Diop",
      "type_document": "CNI",
      "numero_document": "10120030201000558",
      "nin": "2K05201200108",
      "nationalite": "SEN",
      "date_naissance": "1990-05-14",
      "sexe": "F",
      "date_expiration_document": "2033-01-01",
      "telephone": "+221770000000",
      "email": null,
      "provenance": "Ministère des Finances",
      "immatriculation_vehicule": "DK-4242-AB",
      "document_recto_url": null,
      "document_verso_url": "/storage/uploads/mrz/2026/08/abc.png",
      "mrz_image_url": "/storage/uploads/mrz/2026/08/abc.png",

      "derniere_visite_at": "2026-08-12T09:15:00+00:00",
      "visite_ouverte_id": null
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

Les fiches sont triées de la **venue la plus récente à la plus ancienne** : le
visiteur cherché est presque toujours dans les premiers résultats.

**Les deux champs à exploiter :**

| Champ | Usage |
|---|---|
| `derniere_visite_at` | « Dernière venue : 12/08 à 09h15 » — aide l'agent à confirmer que c'est la bonne personne |
| `visite_ouverte_id` | **Non nul = la personne est encore présente.** Affichez « Déjà présent » et proposez la clôture au lieu du bouton d'enregistrement |

`visite_ouverte_id` vous évite de découvrir le problème par une erreur : si vous
l'exploitez, l'agent ne voit jamais le 409.

### 3.2 Enregistrer la visite — `POST /visits` avec `visitor_id`

```json
{
  "visitor_id": "6f1c…",
  "visitor_passage": {
    "telephone": "+221781111111",
    "provenance": "Ministère des Finances",
    "immatriculation_vehicule": "DK-4242-AB"
  },
  "service_id": "…",
  "agent_id": "…",
  "purpose_id": "…",
  "badge_number": "B-042",
  "signature_url": "/storage/uploads/signatures/2026/08/….png"
}
```

Réponse `201` : une `VisitRead` identique à celle du parcours habituel.

**Règles à respecter :**

- `visitor` et `visitor_id` sont **exclusifs**. Envoyer les deux — ou aucun —
  donne `400 VALIDATION_ERROR`.
- `visitor_passage` n'accompagne **que** `visitor_id`. Avec `visitor`, renseignez
  ces champs directement dans le bloc `visitor` (sinon `400`).
- `visitor_passage` est **facultatif** et **partiel** : un champ omis conserve la
  valeur connue, il ne l'efface pas. N'envoyez que ce que l'agent a modifié.
- Les trois champs de `visitor_passage` sont ceux qui changent d'une venue à
  l'autre. Proposez-les à la confirmation de l'agent plutôt que de les recopier en
  silence : la plaque du véhicule d'il y a trois mois n'est pas celle du jour.

**`visitor_id` inconnu** → `404 VISITOR_NOT_FOUND`.

### 3.3 Le parcours de scan ne change pas

`POST /visits` avec le bloc `visitor` complet fonctionne exactement comme avant.
Aucune modification requise sur le chemin existant.

---

## 4. ⚠️ Un visiteur déjà présent ne peut plus entrer deux fois

**C'est le seul changement de comportement sur une route existante.**

Auparavant, deux visites `PRESENT` pouvaient coexister pour la même personne — ce
qui faussait le compteur des présents du dashboard. Désormais :

```http
POST /api/v1/visits    →    409 Conflict
```

```json
{
  "error_code": "VISITOR_ALREADY_PRESENT",
  "message": "Ce visiteur a déjà une visite en cours : clôturez-la avant d'en enregistrer une nouvelle.",
  "details": {
    "visitor_id": "6f1c…",
    "visit_id": "9a2e…",
    "checked_in_at": "2026-08-14T09:32:11+00:00"
  }
}
```

Le conflit s'applique **aux deux chemins** : `visitor_id` comme `visitor` complet
(un rescan de quelqu'un déjà présent est la même erreur).

### Ce qu'il faut faire de ce 409

`details` contient tout le nécessaire pour proposer la sortie plutôt qu'une impasse :

```
┌──────────────────────────────────────────────┐
│  Aminata Diop est déjà présente              │
│  Entrée aujourd'hui à 09h32                  │
│                                              │
│  [ Clôturer la visite ]   [ Annuler ]        │
└──────────────────────────────────────────────┘
```

`[ Clôturer la visite ]` → `PUT /visits/{details.visit_id}/checkout`, puis rejouez
le `POST /visits`.

### Mode hors-ligne

`POST /visits/sync` renvoie toujours `200`, avec le détail par item. Une entrée en
conflit apparaît ainsi :

```json
{
  "index": 2,
  "client_reference": "abc-123",
  "status": "conflict",
  "error_code": "VISITOR_ALREADY_PRESENT",
  "message": "Ce visiteur a déjà une visite en cours : …"
}
```

> **Limite connue, à avoir en tête.** `POST /visits/sync` ne transporte que des
> **entrées**, jamais les sorties saisies hors-ligne. Un aller-retour de la même
> personne dans la même journée sans connexion verra donc sa seconde entrée
> remonter en `conflict`. Rien n'est perdu en silence — le batch le signale item par
> item — mais présentez ces conflits à l'agent plutôt que de les avaler. La synchro
> des clôtures reste à spécifier : remontez-nous le besoin s'il est réel sur le
> terrain.

---

## 5. Photos recto et verso de la pièce

Le scan MRZ ne capture qu'une face — le **verso** d'une CNI. Quand l'OCR échoue et
que l'agent saisit l'identité à la main, il peut maintenant photographier les deux
faces : le recto porte la photo du titulaire et des mentions absentes du verso.

### 5.1 Déposer une face

```http
POST /api/v1/uploads/document?face=recto
Authorization: Bearer <token>
Content-Type: multipart/form-data
```

| Élément | Valeur |
|---|---|
| Champ de formulaire | `document` |
| Query `face` | `recto` ou `verso` — obligatoire, toute autre valeur → `400` |
| Formats | `.jpg` `.jpeg` `.png` `.heic` `.heif` |
| Taille max | 10 Mo (`413`/`400 FILE_TOO_LARGE` au-delà) |

Réponse `201` :

```json
{ "url": "/storage/uploads/documents/recto/2026/08/8f2c1e….jpg" }
```

Même principe que `/uploads/signature` : on dépose, on récupère l'`url`, on la
reporte dans le payload de création de visite.

### 5.2 Reporter les URLs

Deux champs sur le bloc `visitor` de `POST /visits` :

```json
{
  "visitor": {
    "prenom": "Aminata",
    "nom": "Diop",
    "type_document": "CNI",
    "numero_document": "10120030201000558",
    "nin": "2K05201200108",
    "document_recto_url": "/storage/uploads/documents/recto/2026/08/8f2c….jpg",
    "document_verso_url": "/storage/uploads/documents/verso/2026/08/a41b….jpg"
  }
}
```

Ils apparaissent aussi en lecture sur toute `VisitorRead` — donc dans `VisitRead`,
dans le détail de visite et dans les résultats de `GET /visitors`.

### 5.3 `mrz_image_url` est déprécié

`mrz_image_url` désigne **exactement la même face** que `document_verso_url`.

- Il reste **accepté en écriture** : votre code actuel continue de fonctionner.
- Le serveur **reporte** automatiquement sa valeur sur `document_verso_url` quand
  celui-ci est absent. Vous n'avez rien à faire pour rester cohérent.
- Si les deux sont fournis, `document_verso_url` gagne.
- `POST /ocr/scan` renvoie toujours `mrz_image_url` : **aucune modification requise**
  sur le parcours de scan. Quand vous migrerez, mappez-le vers `document_verso_url`
  côté client.

Objectif à terme : retirer `mrz_image_url`. Prévoyez-le, sans urgence.

### 5.4 Ce que ça implique côté UX

Le recto n'est utile que sur le **parcours de saisie manuelle** (échec OCR). Sur un
scan réussi, ne demandez pas de photo supplémentaire : le verso est déjà déposé par
`/ocr/scan`.

---

## 6. La personne rencontrée devient facultative

Un dépôt de dossier, un retrait de document ou une livraison s'adresse **au service**,
pas à quelqu'un. Jusqu'ici l'agent d'accueil devait quand même désigner une personne —
il en choisissait une au hasard, et le classement des agents les plus visités du
dashboard devenait un classement de qui est sélectionné par défaut.

### 6.1 À la création

`agent_id` est désormais **optionnel** dans `POST /visits` : omettez-le, ou envoyez
`null`.

```json
{
  "visitor": { "…": "…" },
  "service_id": "…",
  "purpose_id": "…",
  "badge_number": "B-042"
}
```

- `service_id` reste **obligatoire** : c'est lui qui situe la visite.
- Un `agent_id` **fourni** est contrôlé exactement comme avant : il doit exister, ne
  pas être archivé, et appartenir au service (`409 AGENT_SERVICE_MISMATCH`).
- Le motif ne change pas : `purpose_id` ou `motif_libre`, comme aujourd'hui.

### 6.2 En lecture — le point à traiter

**`agent` peut désormais valoir `null`** dans toute `VisitRead` : détail de visite,
listing, réponse de création, export.

```json
{
  "id": "…",
  "statut": "PRESENT",
  "service": { "code": "DRH", "name": "Direction des RH" },
  "agent": null,
  "purpose": { "libelle": "Dépôt de dossier" }
}
```

Si votre code fait `visit.agent.name` sans garde, il plantera sur ces visites.
C'est le seul vrai travail de cette évolution. Affichez « — » ou « Service »
plutôt qu'une chaîne vide muette.

### 6.3 Retirer une personne saisie par erreur (dashboard)

`PATCH /visits/{id}` distingue le champ **omis** du champ **envoyé à `null`** :

| Corps | Effet |
|---|---|
| `{"reason": "…", "badge_number": "B-999"}` | L'agent reste en place |
| `{"reason": "…", "agent_id": null}` | **L'agent est retiré** |

Attention : `null` ne vaut « efface » que pour `agent_id` et `purpose_id`. Sur
`service_id` ou `checked_in_at`, il est refusé en `400 VALIDATION_ERROR` — ces
champs ne peuvent pas être vides.

### 6.4 Effets sur les statistiques

| Endroit | Comportement |
|---|---|
| Export CSV | Colonne « Personne rencontrée » **vide** |
| `GET /dashboard/top-agents` | Ces visites sont **exclues** du classement |
| Répartition par service, séries temporelles, compteurs | **Inchangés** — ces visites comptent partout ailleurs |

Conséquence à afficher clairement : le total de `top-agents` est inférieur au nombre
de visites de la période. C'est un **palmarès de personnes**, pas une répartition —
si vous le présentez comme un camembert à 100 %, l'écart passera pour un bug.

---

## 7. Dashboard web uniquement

### 7.1 Mot de passe d'un agent : saisi plutôt que généré

**Rien de nouveau côté API — le champ existait déjà, il manque simplement dans
l'interface.**

```http
POST /api/v1/users/{user_id}/reset-password
```

```json
{ "mot_de_passe": "MotDePasseSolide2026" }
```

| Corps | Effet |
|---|---|
| `{}` | Le serveur génère un mot de passe et le renvoie dans `mot_de_passe` |
| `{"mot_de_passe": "…"}` | Le mot de passe fourni est appliqué ; la réponse renvoie `mot_de_passe: null` |

**6 caractères minimum**, 128 maximum. Le `null` en réponse est normal : le serveur
ne restitue que ce qu'il a lui-même généré.

> Le minimum est passé de 12 à 6 caractères : l'application est interne et les
> agents saisissent leur mot de passe sur une tablette plusieurs fois par jour.
> Si votre écran affiche encore « 12 caractères minimum », c'est à corriger.
> Le mot de passe **généré** par le serveur, lui, fait toujours 16 caractères.

Dans les deux cas, **toutes les sessions du compte sont coupées** — un mot de passe
réinitialisé l'est souvent parce qu'il a fuité.

Même chose à la création : `POST /users` accepte un `mot_de_passe` optionnel.

> À prévoir dans l'écran : un mot de passe saisi par l'admin reste connu de l'admin.
> Un forçage du changement à la première connexion n'existe pas encore côté API —
> dites-nous si vous le voulez.

### 7.2 Nouveau réglage : conservation des photos de pièces

`GET /api/v1/settings` et `PUT /api/v1/settings` exposent un champ de plus :

```json
{
  "visit_long_duration_alert_minutes": 120,
  "max_failed_login_attempts": 5,
  "account_lockout_minutes": 15,
  "visits_export_max_rows": 50000,
  "document_images_retention_days": 365,
  "updated_at": "2026-08-14T10:00:00+00:00"
}
```

| Contrainte | Valeur |
|---|---|
| Plage | `0` à `3650` jours |
| Défaut | `365` |
| `0` | Désactive la purge (conservation illimitée) |

Libellé suggéré : *« Conservation des photos de pièces d'identité (jours) — comptée
depuis la dernière visite du visiteur. 0 = conservation illimitée. »*

Deux choses à dire clairement dans l'interface :

- La purge **ne supprime que les images**. Visites, visiteurs et journal d'audit
  restent intacts.
- Elle n'est **pas automatique** : elle s'exécute par une tâche planifiée côté
  serveur. Modifier ce réglage ne déclenche rien immédiatement.

### 7.3 Nouveau réglage : clôture automatique des visites

Les mêmes routes `GET /api/v1/settings` et `PUT /api/v1/settings` exposent les
paramètres suivants :

```json
{
  "automatic_visit_closure_enabled": true,
  "automatic_visit_closure_time": "17:00"
}
```

| Champ | Type | Contraintes | Défaut |
|---|---|---|---|
| `automatic_visit_closure_enabled` | booléen | — | `true` |
| `automatic_visit_closure_time` | chaîne | format strict `HH:MM`, de `00:00` à `23:59` | `"17:00"` |

La mise à jour est partielle :

```http
PUT /api/v1/settings
Content-Type: application/json
```

```json
{
  "automatic_visit_closure_enabled": true,
  "automatic_visit_closure_time": "18:00"
}
```

Le réglage est réservé aux administrateurs. Une tâche planifiée clôture les
visites `PRESENT` entrées avant l'heure configurée. Elles deviennent `SORTI`,
avec `checkout_origin: "AUTOMATIC"`, et une fiche de pièce potentiellement oubliée
est créée. Les visites entrées après l'heure limite ne sont pas clôturées
rétroactivement le même jour.

L'heure est actuellement interprétée en UTC par le service de clôture. Le frontend
doit afficher clairement le fuseau utilisé par l'environnement déployé et ne pas
supposer silencieusement l'heure locale de l'appareil.

### 7.4 Supprimer définitivement une visite

```http
DELETE /api/v1/visits/{visit_id}
Authorization: Bearer <access_token>     → 204 No Content, corps vide
```

Réservé aux rôles `ADMIN` et `ORG_ADMIN`. Un `ORG_ADMIN` ne peut agir que sur
une visite de son organisation. La ligne quitte le registre **pour de bon** : elle n'est
plus lisible par `GET /visits/{id}`, disparaît du listing, de l'export et de toutes
les statistiques.

| Réponse | Quand |
|---|---|
| `204` | Supprimée. Aucun corps à parser — ne tentez pas de lire du JSON |
| `403 FORBIDDEN` | Appelée par un agent, un superviseur ou hors organisation |
| `404 VISIT_NOT_FOUND` | Identifiant inconnu, ou visite **déjà supprimée** |

Le `404` du second appel est le comportement normal, pas une erreur à signaler :
si l'utilisateur double-clique, traitez-le comme un succès.

**Ne l'utilisez pas comme bouton « annuler ».** Pour une visite réelle entrée par
erreur, c'est `POST /visits/{id}/cancel` qu'il faut appeler : la visite reste
consultable avec son motif, sort des statistiques de la même façon, et reste
défendable lors d'un audit. `DELETE` ne se justifie que pour ce qui n'aurait jamais
dû exister — un enregistrement de test, un doublon manifeste.

Côté interface, deux conséquences :

- Placez la suppression **loin** de l'annulation, et demandez une confirmation
  explicite. Rien ne permet de revenir en arrière.
- Le visiteur, lui, **n'est pas supprimé** : sa fiche et ses passages précédents
  restent. Supprimer sa seule visite ouverte le rend simplement à nouveau
  enregistrable par `POST /visits`.

L'opération laisse une entrée `visit.deleted` au journal d'audit, avec un
instantané complet de la visite détruite (visiteur, service, horodatages, statut).
C'est la seule trace qui subsiste : elle est visible dans
`GET /audit-logs?action=visit.deleted`, champ `metadata.visite`.

---

### 7.5 Statistiques et dashboard

Le dashboard expose les routes suivantes :

| Méthode | Route | Accès |
|---|---|---|
| `GET` | `/api/v1/dashboard/stats` | Tout compte authentifié ; données limitées à l'organisation lorsqu'elle existe |
| `GET` | `/api/v1/dashboard/stats/timeseries` | `ADMIN`, `ORG_ADMIN`, `SUPERVISEUR` |
| `GET` | `/api/v1/dashboard/stats/by-service` | `ADMIN`, `ORG_ADMIN`, `SUPERVISEUR` |
| `GET` | `/api/v1/dashboard/stats/by-purpose` | `ADMIN`, `ORG_ADMIN`, `SUPERVISEUR` |
| `GET` | `/api/v1/dashboard/stats/peak-hours` | `ADMIN`, `ORG_ADMIN`, `SUPERVISEUR` |
| `GET` | `/api/v1/dashboard/stats/avg-duration` | `ADMIN`, `ORG_ADMIN`, `SUPERVISEUR` |
| `GET` | `/api/v1/dashboard/stats/top-agents` | `ADMIN`, `ORG_ADMIN`, `SUPERVISEUR` |

Les routes analytiques acceptent `date_from`, `date_to` et, pour la série
temporelle, `granularity=day|week|month`. Un `SUPERVISEUR` ne doit afficher que
les indicateurs de son organisation ; le filtrage est appliqué côté serveur.

`top-agents` est un classement des personnes rencontrées et non une répartition
exhaustive : les visites dont `agent` vaut `null` sont volontairement exclues.

### 7.6 Référentiels services, agents et motifs

Lecture pour tout compte authentifié :

```http
GET /api/v1/services
GET /api/v1/services/{service_id}/agents
GET /api/v1/agents?service_id={service_id}
GET /api/v1/purposes
```

`ORG_ADMIN` et `SUPERVISEUR` reçoivent uniquement les services et agents de leur
organisation. `ADMIN` peut consulter toutes les organisations. Les entrées
archivées sont masquées par défaut ; ajouter `include_archived=true` pour les
écrans d'administration.

Écriture pour `ADMIN` et `ORG_ADMIN` :

```http
POST  /api/v1/services
PUT   /api/v1/services/{service_id}
PATCH /api/v1/services/{service_id}/status
POST  /api/v1/agents
PUT   /api/v1/agents/{agent_id}
PATCH /api/v1/agents/{agent_id}/status
POST  /api/v1/purposes
PUT   /api/v1/purposes/{purpose_id}
PATCH /api/v1/purposes/{purpose_id}/status
```

Un `ORG_ADMIN` ne peut créer, modifier ou archiver que les référentiels de son
organisation. L'archivage est logique : aucun bouton de suppression définitive
ne doit être prévu. Les statuts de référentiel sont `active` et `archived`.

## 8. Clôture automatique et pièces oubliées

### 8.1 Identifier l'origine d'une clôture

`VisitRead` expose maintenant :

```json
{
  "statut": "SORTI",
  "checked_out_at": "2026-09-04T17:02:13+00:00",
  "checkout_origin": "AUTOMATIC"
}
```

Valeurs possibles :

| Valeur | Signification |
|---|---|
| `MANUAL` | Sortie enregistrée par un utilisateur via `PUT /visits/{id}/checkout` |
| `AUTOMATIC` | Sortie appliquée par la tâche quotidienne de clôture automatique |
| `null` | Visite historique créée avant l'ajout de ce champ, ou visite encore `PRESENT` |

Le filtre est disponible sur `GET /api/v1/visits` :

```http
GET /api/v1/visits?checkout_origin=AUTOMATIC&statut=SORTI
```

Pour le listing dédié des sorties automatiques :

```http
GET /api/v1/visits/auto-closed?date=2026-09-04&page=1&page_size=20
```

`date` est facultatif et attend `YYYY-MM-DD`. Il filtre la fenêtre
`checked_in_at` de la visite. Le résultat est une pagination standard de
`VisitRead`. Utilisez `checkout_origin` pour l'étiquette et le filtre
« clôturée automatiquement » ; n'utilisez pas uniquement `statut`, car les sorties
manuelles ont aussi le statut `SORTI`.

### 8.2 Lister les fiches de pièces oubliées

Une fiche est créée pour chaque visite clôturée automatiquement. Route :

```http
GET /api/v1/forgotten-document-cases
Authorization: Bearer <access_token>
```

Paramètres disponibles :

| Paramètre | Exemple | Description |
|---|---|---|
| `organization_id` | UUID | Filtre d'organisation ; un admin d'organisation est limité à la sienne |
| `status` | `PENDING` | `PENDING`, `RESOLVED`, `SMS_SENT`, `SMS_FAILED` ou `NO_PHONE` |
| `date_from` | `2026-09-01T00:00:00Z` | Date minimale de création de la fiche |
| `date_to` | `2026-09-04T23:59:59Z` | Date maximale de création de la fiche |
| `search` | `Diop` | Recherche sur nom, prénom ou numéro de document |
| `page` / `page_size` | `1` / `20` | Pagination habituelle |

Exemple de réponse :

```json
{
  "items": [
    {
      "id": "b12f…",
      "organization_id": "7e21…",
      "visit_id": "9a2e…",
      "visitor_id": "6f1c…",
      "status": "PENDING",
      "resolved_at": null,
      "resolved_by": null,
      "resolution_note": null,
      "sms_status": null,
      "sms_sent_at": null,
      "created_at": "2026-09-04T17:02:13+00:00",
      "updated_at": "2026-09-04T17:02:13+00:00",
      "visitor": {
        "id": "6f1c…",
        "prenom": "Aminata",
        "nom": "Diop",
        "numero_document": "10120030201000558",
        "telephone": "+221770000000"
      },
      "resolved_by_user": null
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

Pour afficher le service et les horaires dans l'interface, récupérez la visite
associée avec `GET /api/v1/visits/{visit_id}`. La réponse de la fiche contient
les identifiants `visit_id` et `visitor_id`, mais pas un objet `visit` imbriqué.

### 8.3 Consulter et résoudre une fiche

```http
GET /api/v1/forgotten-document-cases/{case_id}
```

Une fiche peut être marquée comme résolue lorsque la pièce a été remise ou
restituée :

```http
POST /api/v1/forgotten-document-cases/{case_id}/resolve
Content-Type: application/json
```

```json
{
  "resolution_note": "Pièce remise au visiteur après vérification."
}
```

`resolution_note` est facultatif et limité à 500 caractères. La réponse contient
`status: "RESOLVED"`, `resolved_at`, `resolved_by` et la note. Une deuxième
résolution renvoie `409 FORGOTTEN_DOCUMENT_ALREADY_RESOLVED`.

Les rôles authentifiés peuvent consulter et traiter les fiches. Un
`ORG_ADMIN` ne peut accéder qu'aux fiches de son organisation ; le serveur
retourne `403 FORBIDDEN` pour une autre organisation.

### 8.4 États SMS actuellement exposés

Le modèle prévoit les états `SMS_SENT`, `SMS_FAILED` et `NO_PHONE`, ainsi que
`sms_status` et `sms_sent_at` pour une intégration future. **Aucune route
d'envoi SMS n'est encore disponible** : ne construisez pas de bouton qui appelle
`POST /forgotten-document-cases/{id}/send-sms` tant que ce contrat n'a pas été
ajouté au backend.

## 9. Organisations : gestion, charte graphique et logo

### 9.1 Le modèle exposé

```json
{
  "id": "7e21…",
  "nom": "Ministère des Finances",
  "code": "FINANCES",
  "localisation": "Rue René Ndiaye, Dakar",
  "description": null,
  "contact_email": "contact@finances.gouv.sn",
  "contact_phone": "+221 33 889 21 00",
  "is_active": true,
  "logo_url": "/storage/uploads/organizations/7e21…/2026/09/9f3c….png",
  "primary_color": "#1E3A8A",
  "secondary_color": "#0D9488",
  "accent_color": "#F59E0B",
  "theme_config": { "welcome_message": "Bienvenue" },
  "created_at": "2026-09-01T08:00:00Z",
  "updated_at": "2026-09-07T11:22:00Z"
}
```

Les couleurs acceptent `#RGB` ou `#RRGGBB`. `contact_email` est validé comme une
adresse. `code` accepte lettres, chiffres, tiret et souligné, et est normalisé en
majuscules.

**`logo_url` est en lecture seule.** Il ne s'écrit que par
`POST /organizations/{id}/logo` (§9.5). L'envoyer dans un `PUT` n'a aucun effet —
la valeur est ignorée, sans erreur. Exposé en écriture, le champ permettrait de
faire pointer chaque écran du dashboard vers une ressource tierce.

### 9.2 Mise à jour partielle : `null` efface, absent ne touche à rien

`PUT /organizations/{id}` applique désormais une sémantique PATCH stricte :

| Corps envoyé | Effet |
|---|---|
| `{"localisation": "Dakar"}` | renseigne la localisation |
| `{"localisation": null}` | **efface** la localisation |
| `{}` | ne change rien |

C'est ce qui permet à l'interface de retirer une valeur, et pas seulement de la
remplacer. Auparavant un `null` explicite était confondu avec un champ absent :
aucun champ facultatif n'était effaçable une fois renseigné.

`nom` et `code` font exception : un `null` y est ignoré, ils sont obligatoires.
`is_active` n'est plus accepté ici — voir §9.4.

### 9.3 Routes de gestion

| Méthode | Route | Accès |
|---|---|---|
| `GET` | `/organizations` | `ADMIN` : tout le parc ; `ORG_ADMIN` : la sienne |
| `POST` | `/organizations` | `ADMIN` |
| `GET` | `/organizations/{id}` | `ADMIN` ou sa propre organisation |
| `PUT` | `/organizations/{id}` | `ADMIN` ou sa propre organisation |
| `PATCH` | `/organizations/{id}/status` | `ADMIN` |
| `GET` | `/organizations/{id}/stats` | `ADMIN` ou sa propre organisation |
| `GET` | `/organizations/{id}/theme` | `ADMIN` ou sa propre organisation |
| `PUT` | `/organizations/{id}/theme` | `ADMIN` ou sa propre organisation |
| `GET` | `/organizations/{id}/administrators` | `ADMIN` ou sa propre organisation |
| `POST` | `/organizations/{id}/administrators` | `ADMIN` ou sa propre organisation |
| `GET` | `/organizations/{id}/users` | `ADMIN` ou sa propre organisation |
| `POST` | `/organizations/{id}/logo` | `ADMIN` ou sa propre organisation |
| `DELETE` | `/organizations/{id}/logo` | `ADMIN` ou sa propre organisation |
| `GET` | `/organizations/theme?code=…` | **public**, sans token |

Aucune suppression n'est exposée : services, visites et comptes référencent une
organisation en `ON DELETE RESTRICT`. La désactivation remplit ce rôle.

**Liste** — `GET /organizations` accepte `search` (nom, code ou localisation),
`is_active`, `sort` (`asc`/`desc`), `sort_by` (`nom`/`created_at`), `page` et
`page_size`. Un `ORG_ADMIN` peut appeler la même route : il y trouve une page
contenant sa seule organisation, ce qui évite deux écrans distincts.

### 9.4 Activer et désactiver — `PATCH /organizations/{id}/status`

```http
PATCH /api/v1/organizations/{id}/status
Content-Type: application/json

{ "is_active": false, "motif": "Fin de convention" }
```

Réservé à `ADMIN`. `motif` est facultatif et part au journal d'audit.

**La désactivation coupe l'accès de tous les comptes rattachés dès la requête
suivante**, sessions déjà ouvertes comprises : un jeton encore valide est refusé
avec `403 INACTIVE_ORGANIZATION`, et la connexion l'est aussi. Aucun compte n'est
modifié — la réactivation rétablit l'accès tel qu'il était.

L'opération est **refusée tant que des visiteurs sont présents** :

```json
{
  "error_code": "ORGANIZATION_HAS_ACTIVE_VISITS",
  "message": "Des visites sont encore en cours dans cette organisation : clôturez-les avant de la désactiver.",
  "details": { "visites_en_cours": 3, "organization_id": "7e21…" }
}
```

Les agents ne pourraient plus enregistrer les sorties, et le registre resterait
ouvert sur des visites qu'aucune route ne saurait plus clôturer. Côté interface :
affichez le nombre et proposez un lien vers la liste des visites en cours.

Une structure désactivée disparaît aussi de la route publique de thème (§9.6).

### 9.5 Logo — `POST /organizations/{id}/logo`

```http
POST /api/v1/organizations/{id}/logo
Content-Type: multipart/form-data

logo=<fichier>
```

Réponse `201` : un `OrganizationRead` complet, `logo_url` renseigné.

- Formats acceptés : **PNG, JPEG, WebP, GIF**. Le format est déduit du **contenu**
  du fichier, pas de son extension : renommer un document en `.png` ne suffit pas
  et renvoie `400 UNSUPPORTED_IMAGE`. SVG est refusé — c'est un document XML qui
  peut porter du script.
- Taille maximale : **2 Mo** (`400 FILE_TOO_LARGE` au-delà).
- L'image est réencodée en **PNG** et ramenée à **512 px** sur son plus grand
  côté. La transparence est préservée : un logo aplati sur du blanc serait
  inutilisable sur fond sombre.
- L'URL change à chaque dépôt, et l'ancien fichier est supprimé. Ne mettez pas
  `logo_url` en cache au-delà de la réponse — mais l'URL retournée est stable et
  peut être servie avec un cache long.

`DELETE /organizations/{id}/logo` efface le fichier et remet `logo_url` à `null`.
Sans logo, la route est un no-op et répond `200`.

### 9.6 Charte graphique avant connexion — `GET /organizations/theme`

```http
GET /api/v1/organizations/theme?code=MIN-INTERIEUR
```

**Aucun token requis.** C'est la seule route d'organisation ouverte, et elle
existe pour la page de connexion : celle-ci doit porter les couleurs et le logo
de la structure, or elle s'affiche par définition avant toute authentification.

La réponse ne contient que la devanture — `id`, `nom`, `code`, `logo_url`, les
trois couleurs, `theme_config`. Ni contact, ni statistique, ni état
d'activation. Une organisation désactivée ou inconnue répond `404`.

Après connexion, `GET /organizations/{id}/theme` renvoie les mêmes champs pour
l'organisation de l'utilisateur.

### 9.7 Administrateurs d'une organisation

Nommer un administrateur n'est plus réservé à la création de la structure :

```http
POST /api/v1/organizations/{id}/administrators
Content-Type: application/json

{ "nom": "Awa Ndiaye", "identifiant": "orgadmin042", "poste": "Direction" }
```

Réponse `201` :

```json
{
  "user": { "id": "…", "identifiant": "orgadmin042", "role": "ORG_ADMIN", "…": "…" },
  "mot_de_passe": "K7mR-x2Qw9dTn4Vb"
}
```

`mot_de_passe` n'est renvoyé que s'il a été **généré** par le serveur, et une
seule fois : il n'est stocké nulle part en clair. Fournissez `mot_de_passe` dans
la requête pour le choisir — la réponse vaut alors `null`.

`GET /organizations/{id}/administrators` liste les comptes `ORG_ADMIN`, actifs en
tête. Les comptes désactivés y figurent : mieux vaut réactiver un administrateur
existant que d'en recréer un.

`GET /organizations/{id}/users` reprend le moteur de `GET /users`, borné à la
structure, avec `role`, `status`, `search`, `page` et `page_size`. Une
organisation inconnue répond `404`, pas une page vide.

**Garde-fou** : désactiver ou rétrograder le **dernier** `ORG_ADMIN` actif d'une
organisation renvoie `409 LAST_ADMIN`. Le contrôle existait pour `ADMIN` mais ne
s'appliquait pas aux administrateurs d'organisation, qui pouvaient donc
disparaître et laisser la structure ingérable. Prévoyez le message : « nommez un
autre administrateur avant de retirer celui-ci ».

### 9.8 Réglages : plateforme ou organisation

`GET /settings` renvoie les valeurs **effectives** de l'appelant, avec trois
champs de contexte :

```json
{
  "automatic_visit_closure_time": "20:00",
  "automatic_visit_closure_enabled": true,
  "visit_long_duration_alert_minutes": 120,
  "max_failed_login_attempts": 5,
  "…": "…",
  "organization_id": "7e21…",
  "scope": "organization",
  "organization_overrides": ["automatic_visit_closure_time"]
}
```

`organization_overrides` dit lesquels sont réellement surchargés par la
structure ; les autres suivent la valeur de plateforme et **changeront avec
elle**. Affichez la nuance : « suit la plateforme (17:00) » se distingue de
« réglé sur 17:00 ».

Deux portées :

| Portée | Paramètres | Qui écrit |
|---|---|---|
| Organisation | `automatic_visit_closure_enabled`, `automatic_visit_closure_time`, `visit_long_duration_alert_minutes` | `ORG_ADMIN` pour la sienne, `ADMIN` partout |
| Plateforme | `max_failed_login_attempts`, `account_lockout_minutes`, `visits_export_max_rows`, `document_images_retention_days` | `ADMIN` uniquement |

Un `ORG_ADMIN` qui tente d'écrire un paramètre de plateforme reçoit `403`, avec
la liste de ce qui est recevable :

```json
{
  "error_code": "FORBIDDEN",
  "message": "Ces paramètres relèvent de la plateforme et ne peuvent pas être réglés organisation par organisation.",
  "details": {
    "parametres_refuses": ["max_failed_login_attempts"],
    "parametres_autorises": ["automatic_visit_closure_enabled", "automatic_visit_closure_time", "visit_long_duration_alert_minutes"]
  }
}
```

Le refus est explicite et non silencieux : croire avoir durci le verrouillage des
comptes de toute la plateforme alors que rien n'a bougé serait pire que l'erreur.

**Réaligner sur la plateforme** :

```http
DELETE /api/v1/settings/overrides/automatic_visit_closure_time
```

La structure suit de nouveau la valeur commune, **et ses évolutions futures**.
C'est différent d'un `PUT` qui recopierait la valeur du jour : celle-ci figerait
le réglage et la structure cesserait de bénéficier des ajustements ultérieurs.

`ADMIN` peut viser une organisation avec `?organization_id=…` sur `GET`, `PUT` et
`DELETE`.

### 9.9 Journal d'audit

Le journal porte désormais `organization_id`. `ORG_ADMIN` ne lit que le sien ;
`ADMIN` voit tout, y compris les événements de plateforme (`organization_id:
null` — échecs de connexion de comptes non rattachés, administration des
structures).

```http
GET /api/v1/audit-logs?page=1&page_size=20
GET /api/v1/audit-logs/actions
```

Filtres : `action` (par préfixe — `visit` ramène `visit.created`,
`visit.cancelled`…), `entity`, `entity_id`, `actor_id`, `date_from`, `date_to`,
et `organization_id` réservé à `ADMIN` — un `ORG_ADMIN` qui le renseigne sur une
autre structure reçoit `403 CROSS_ORGANIZATION_FORBIDDEN`.

`/audit-logs/actions` est lui aussi borné au périmètre : la seule liste des
verbes présents renseignerait déjà sur ce qui se passe ailleurs.

Nouveaux verbes : `organization.status_changed`, `organization.admin_added`,
`organization.logo_updated`, `organization.logo_removed`.

`metadata` est un objet extensible : ne construisez pas de logique dépendant
d'une clé non documentée.

### 9.10 Motifs de visite : communs ou propres

`Purpose` porte maintenant `organization_id` :

- `null` → motif **commun**, proposé à toutes les structures, administré par
  `ADMIN` ;
- renseigné → motif propre à une organisation.

`GET /purposes` renvoie les deux, fusionnés. Un `ORG_ADMIN` ne peut modifier ni
archiver un motif commun (`403 CROSS_ORGANIZATION_FORBIDDEN`) : l'archiver chez
lui le retirerait à tout le monde. Distinguez-les visuellement dans l'écran
d'administration.

Corollaire utile : deux structures peuvent avoir un motif de même libellé, et
deux services de même code (`DRH` existe dans chaque ministère). L'unicité est
locale à l'organisation.

### 9.11 Stratégie frontend recommandée

- Charger le thème public par `code` **avant** la connexion, puis le thème
  authentifié après.
- Utiliser des valeurs de secours locales si `logo_url` ou une couleur vaut
  `null`.
- Valider `#RGB`/`#RRGGBB` côté interface, tout en traitant le
  `400 VALIDATION_ERROR` du serveur.
- Ne pas supposer que `theme_config` a une structure fixe : c'est un objet
  extensible.
- Distinguer trois `403` dans les messages : `FORBIDDEN` (rôle insuffisant),
  `CROSS_ORGANIZATION_FORBIDDEN` (bonne permission, mauvaise structure),
  `INACTIVE_ORGANIZATION` (structure désactivée — invitez à contacter
  l'administrateur de la plateforme).

## 10. Import en masse des services et des agents

Deux référentiels, deux modèles `.xlsx`, et le même parcours en trois temps :
télécharger le modèle, prévisualiser le classeur rempli, appliquer.

| Étape | Route | Rôles |
|---|---|---|
| Modèle services | `GET /api/v1/services/import/template` | `ADMIN`, `ORG_ADMIN` |
| Modèle agents | `GET /api/v1/agents/import/template` | `ADMIN`, `ORG_ADMIN` |
| Prévisualiser des services | `POST /api/v1/services/import/preview` | `ADMIN`, `ORG_ADMIN` |
| Appliquer des services | `POST /api/v1/services/import` | `ADMIN`, `ORG_ADMIN` |
| Prévisualiser des agents | `POST /api/v1/agents/import/preview` | `ADMIN`, `ORG_ADMIN` |
| Appliquer des agents | `POST /api/v1/agents/import` | `ADMIN`, `ORG_ADMIN` |

### 10.1 Pourquoi la prévisualisation prend un fichier et l'application du JSON

Ce n'est pas une asymétrie de confort. Elle vous donne deux choses :

- le classeur ne peut plus changer entre ce que l'administrateur a vu et ce qu'il
  confirme — l'application ne relit aucun fichier ;
- la grille que vous affichez devient **corrigeable ligne à ligne**. L'utilisateur
  ne repart pas dans Excel pour un code mal saisi : il corrige la cellule dans
  votre tableau et renvoie les lignes.

Renvoyez donc les objets `items[].donnees` de la prévisualisation, tels quels ou
édités, dans le champ `lignes` de l'application.

### 10.2 Télécharger un modèle

Réponse binaire, `Content-Type` :
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, avec un
`Content-Disposition` portant **les deux formes** du nom :

```
Content-Disposition: attachment; filename="modele-services.xlsx"; filename*=UTF-8''modele-services.xlsx
```

⚠️ **Nommez le fichier vous-même côté dashboard.** Si vous récupérez la réponse
en `fetch` puis créez un blob, le navigateur ne lit pas `Content-Disposition` :
c'est votre attribut `download` qui décide, et un nom sans extension — ou en
`.csv` — produit un fichier que le tableur ouvrira comme du texte. On l'a vu :
un modèle enregistré en `export.csv` s'affiche dans Google Sheets comme une
colonne de caractères illisibles commençant par `PK`. Le fichier était intact ;
seul son nom était faux.

```js
const r = await fetch('/api/v1/services/import/template', { headers: auth });
const url = URL.createObjectURL(await r.blob());
const a = Object.assign(document.createElement('a'), {
  href: url, download: 'modele-services.xlsx',   // ← l'extension vient d'ici
});
a.click(); URL.revokeObjectURL(url);
```

**Ouvrir le classeur.** Excel et LibreOffice l'ouvrent par double-clic. Google
Sheets ne lit pas le `.xlsx` par « Importer un CSV » : déposez-le dans Drive puis
*Ouvrir avec → Google Sheets*, ou dans un classeur existant *Fichier → Importer →
Importer*. Pour le renvoyer, *Fichier → Télécharger → Microsoft Excel (.xlsx)*.

**Le serveur juge sur le contenu, pas sur le nom.** Un classeur valide reste
accepté même si son extension a été abîmée en chemin ; à l'inverse, un vrai CSV
renommé `.xlsx` est refusé. Vous n'avez donc pas à valider l'extension avant
l'envoi — laissez le serveur répondre, son message nomme ce qu'il a reçu.

Le classeur porte deux feuilles : la grille de saisie (en-têtes seuls) et une
notice qui décrit chaque colonne, son caractère obligatoire et un exemple. Les
exemples ne sont **pas** dans la grille — laissés là, ils finiraient importés.

Colonnes attendues :

| Modèle | Colonnes | Obligatoires |
|---|---|---|
| Services | `Code`, `Nom`, `Étage`, `Code parent` | `Code`, `Nom` |
| Agents | `Nom`, `Code service`, `Fonction`, `Bureau` | `Nom`, `Code service` |

Les en-têtes sont reconnus sans tenir compte de la casse, des accents ni des
séparateurs, et quelques synonymes sont acceptés (`libelle` pour `Nom`, `poste`
pour `Fonction`, `service` pour `Code service`…). Un administrateur peut donc
réutiliser un fichier existant sans le recomposer entièrement.

### 10.3 Prévisualiser

`multipart/form-data`, champ `fichier`. Paramètres de requête : `organization_id`
et `mettre_a_jour`.

```json
{
  "applique": false,
  "organization_id": "7e21…",
  "organization_nom": "Ministère de l'Intérieur",
  "mettre_a_jour": false,
  "total": 3, "a_creer": 2, "a_mettre_a_jour": 0, "inchangees": 1, "erreurs": 0,
  "items": [
    { "ligne": 2, "statut": "create", "donnees": { "code": "SG", "nom": "Secrétariat Général" } },
    { "ligne": 3, "statut": "unchanged", "donnees": { "…": "…" }, "entity_id": "a1b2…" },
    { "ligne": 4, "statut": "error", "donnees": { "…": "…" },
      "error_code": "IMPORT_PARENT_UNKNOWN", "message": "Le service parent « FANTOME » est introuvable…" }
  ]
}
```

Affichez **`organization_nom`** au-dessus du bouton de confirmation. Un import
massif dans la mauvaise structure ne doit pas se découvrir après coup.

`ligne` est le numéro dans le classeur d'origine : c'est ce qui permet à
l'utilisateur de retrouver sa cellule. Gardez-le visible dans votre grille.

Les quatre statuts : `create`, `update`, `unchanged`, `error`.

### 10.4 Appliquer

```json
{ "organization_id": "7e21…", "mettre_a_jour": false,
  "lignes": [{ "code": "SG", "nom": "Secrétariat Général" }] }
```

La réponse a **exactement la même forme** que la prévisualisation, avec
`applique: true`. C'est délibéré : l'import étant tout ou rien, ce qui a été fait
est ce qui avait été annoncé, et vous pouvez réutiliser le même composant
d'affichage.

⚠️ **Tout ou rien.** Une seule ligne en erreur répond `422 IMPORT_PLAN_REJECTED`
et **rien n'est écrit** — pas même les lignes valides. Ne présentez donc jamais
un import partiel : c'est soit tout, soit rien.

C'est l'inverse de `POST /visits/sync`, qui applique item par item. La différence
est voulue : une visite hors-ligne n'existe que sur la tablette et ne peut pas
être renvoyée ; un classeur, lui, se corrige et se retéléverse.

`details.lignes` du 422 liste les lignes fautives (50 au maximum) avec leur
`error_code` et leur message.

### 10.5 Ce qu'un import ne fait jamais

- **Il ne supprime ni n'archive rien.** Un service absent du fichier reste en
  place. Ne laissez pas croire à une synchronisation : ce n'est pas un miroir du
  fichier, c'est un ajout.
- **Il ne crée pas de service depuis le fichier des agents.** Un code inconnu
  sort en `error` (`SERVICE_NOT_FOUND`) : une faute de frappe fabriquerait sinon
  une direction fantôme. Enchaînez les deux imports dans cet ordre.
- **Il n'efface pas une valeur avec une cellule vide.** Vide signifie « je ne
  renseigne pas », pas « efface ». À `mettre_a_jour: false`, une entrée existante
  n'est pas touchée du tout et ressort `unchanged`.

### 10.6 Organisation visée

`organization_id` suit la règle des écritures, plus stricte que celle des filtres
de lecture (§1.2) :

| Rôle | `organization_id` |
|---|---|
| `ADMIN` | **obligatoire** — `400 ORGANIZATION_REQUIRED` s'il est omis, `404 ORGANIZATION_NOT_FOUND` s'il est inconnu |
| `ORG_ADMIN` | facultatif ; une autre organisation répond `403 CROSS_ORGANIZATION_FORBIDDEN` |

Un service appartient toujours à une structure : sans elle, il n'apparaîtrait dans
la liste d'aucun poste de contrôle. L'organisation n'est pas une colonne du
fichier — un même modèle sert donc à toutes les structures.

### 10.7 Hiérarchie des services

`Code parent` peut désigner un service déjà en base **ou** un service créé plus
bas dans le même fichier : l'ordre des lignes est indifférent, le serveur trie.

Un cycle est refusé (`SERVICE_HIERARCHY_CYCLE`), y compris quand il se referme par
la base — `A → B` dans le fichier alors que `B → A` existe déjà. La hiérarchie est
reconstruite à chaque `GET /services` ; une boucle y tournerait sans fin.

### 10.8 Erreurs propres à l'import

| Code | HTTP | Quand |
|---|---|---|
| `UNSUPPORTED_IMPORT_FILE` | 400 | Pas un `.xlsx` — extension ou contenu. `.csv` et `.xls` sont refusés |
| `IMPORT_COLUMNS_MISSING` | 400 | En-têtes obligatoires absents. `details` liste **ce qui a été lu** |
| `IMPORT_UNCOMPUTED_FORMULA` | 400 | Formules sans résultat en cache : les cellules seraient importées vides |
| `FILE_TOO_LARGE` | 400 | Au-delà de `MAX_UPLOAD_SIZE_MB` |
| `ORGANIZATION_REQUIRED` | 400 | `ADMIN` sans `organization_id` |
| `CROSS_ORGANIZATION_FORBIDDEN` | 403 | `ORG_ADMIN` visant une autre structure |
| `ORGANIZATION_NOT_FOUND` | 404 | `organization_id` inconnu |
| `IMPORT_TOO_MANY_ROWS` | 422 | Au-delà de `referentiel_import_max_rows` (5 000 par défaut) |
| `IMPORT_PLAN_REJECTED` | 422 | Au moins une ligne en erreur — rien n'est écrit |

Codes portés par une **ligne** (dans `items[].error_code`, jamais en HTTP) :
`IMPORT_FIELD_REQUIRED`, `IMPORT_VALUE_TOO_LONG`, `IMPORT_DUPLICATE_IN_FILE`,
`IMPORT_PARENT_UNKNOWN`, `SERVICE_HIERARCHY_CYCLE`, `SERVICE_NOT_FOUND`,
`ARCHIVED_REFERENTIEL`.

### 10.9 Journal d'audit

Un import écrit **deux sortes de traces** : une par entité (`service.created`,
`agent.created`, `agent.updated`…) avec `source: "import"` dans les métadonnées,
et une de synthèse `referentiel.imported` portant les compteurs.

Si votre écran d'audit propose un filtre « imports », visez `action=referentiel`.
Sans ça, un import de quatre cents agents noie la journée sous les traces
unitaires.

## 11. Checklist d'intégration

### App mobile — par ordre de priorité

- [ ] **Traiter `409 VISITOR_ALREADY_PRESENT`** sur `POST /visits` : écran « déjà
      présent », bouton de clôture depuis `details.visit_id`, puis rejeu
- [ ] **Relâcher la validation du NIN** en 13 caractères alphanumériques
- [ ] **Protéger tous les accès à `visit.agent`** — il peut être `null`
- [ ] Vérifier qu'aucun repli sur `numero_document` ne subsiste quand `nin` est nul
- [ ] Rendre le sélecteur de personne rencontrée facultatif, avec une option
      explicite « Aucune / je viens pour le service »
- [ ] Écran de recherche `GET /visitors` (3 caractères min, debounce, tri déjà fait
      côté serveur)
- [ ] Enregistrement via `visitor_id` + confirmation des champs `visitor_passage`
- [ ] Griser l'enregistrement quand `visite_ouverte_id` est non nul, proposer la
      clôture
- [ ] Capture recto/verso sur le parcours de **saisie manuelle** uniquement, via
      `POST /uploads/document?face=…`
- [ ] Présenter les items `conflict` de `POST /visits/sync` à l'agent

### Dashboard web

- [ ] **Import en masse des référentiels** (§10) : télécharger le modèle,
      déposer le classeur, afficher la grille de prévisualisation — corrigeable
      ligne à ligne — puis confirmer. Dites que l'opération est **tout ou rien**
      et qu'elle n'archive jamais rien : ce n'est pas une synchronisation
- [ ] Afficher `organization_nom` du plan avant la confirmation : un import
      massif dans la mauvaise structure ne doit pas se découvrir après coup
- [ ] Implémenter la connexion JSON, le renouvellement du token et la déconnexion
- [ ] Charger `/auth/me` ou `/me` au démarrage et conserver `role` +
      `organization_id`
- [ ] Appliquer la matrice `ADMIN` / `ORG_ADMIN` / `SUPERVISEUR` /
      `AGENT_CONTROLE` aux menus et aux routes
- [ ] Ne jamais proposer `SUPER_ADMIN` dans les écrans de création ou de
      modification de compte
- [ ] Ajouter la gestion des comptes et des sessions via `/users`
- [ ] Ajouter les écrans de gestion des organisations, du thème et du branding
- [ ] Ajouter les référentiels services, agents et motifs avec archivage logique
- [ ] Ajouter les écrans de statistiques globales ou organisationnelles selon le
      rôle
- [ ] Ajouter le journal d'audit pour `ADMIN` et `ORG_ADMIN` ; ne pas simuler
      un filtre d'organisation tant que le backend ne fournit pas ce périmètre
- [ ] **Protéger tous les accès à `visit.agent`** dans le registre et l'export
- [ ] Sélecteur de personne rencontrée facultatif, et `{"agent_id": null}` pour la
      retirer d'une visite mal saisie
- [ ] Présenter `top-agents` comme un **palmarès**, pas une répartition : son total
      est inférieur au nombre de visites
- [ ] Champ de saisie du mot de passe dans l'écran de réinitialisation (6 car. min,
      128 max)
- [ ] Réglage `document_images_retention_days` dans l'écran des paramètres
- [ ] Réglages `automatic_visit_closure_enabled` et
      `automatic_visit_closure_time` dans l'écran des paramètres
- [ ] Afficher `checkout_origin` dans le registre et ajouter le filtre
      « clôture automatique »
- [ ] Ajouter le listing `GET /visits/auto-closed` avec filtre par date
- [ ] Ajouter la liste des pièces oubliées via
      `GET /forgotten-document-cases` avec filtres de statut, dates et recherche
- [ ] Ajouter le détail et la résolution via
      `GET /forgotten-document-cases/{id}` et
      `POST /forgotten-document-cases/{id}/resolve`
- [ ] Afficher les états `PENDING`, `RESOLVED`, `NO_PHONE`, `SMS_SENT` et
      `SMS_FAILED` ; ne pas afficher d'action SMS tant que la route d'envoi n'existe
- [ ] Charger le thème via `GET /organizations/{id}/theme` et appliquer les
      valeurs de secours quand un champ de branding vaut `null`
- [ ] Permettre la modification du logo, des couleurs et de `theme_config` via
      `PUT /organizations/{id}/theme`
- [ ] Afficher `document_recto_url` / `document_verso_url` sur le détail d'une visite
      (avec repli sur `mrz_image_url` pour les visites antérieures)
- [ ] **Suppression définitive** d'une visite (`DELETE /visits/{id}`) : confirmation
      explicite, bouton distinct de l'annulation, `404` au second appel traité
      comme un succès

---

## 12. Récapitulatif des codes d'erreur nouveaux ou concernés

| Code | HTTP | Route | Quand |
|---|---|---|---|
| `VISITOR_ALREADY_PRESENT` | 409 | `POST /visits`, `/visits/sync` | Le visiteur a déjà une visite `PRESENT` |
| `VISITOR_NOT_FOUND` | 404 | `POST /visits` | `visitor_id` inconnu |
| `VALIDATION_ERROR` | 400 | `POST /visits` | `visitor` + `visitor_id` ensemble, ou aucun des deux ; `visitor_passage` avec `visitor` |
| `VALIDATION_ERROR` | 400 | `PATCH /visits/{id}` | `service_id` ou `checked_in_at` envoyé à `null` — ces champs ne s'effacent pas |
| `AGENT_SERVICE_MISMATCH` | 409 | `POST /visits` | `agent_id` fourni mais n'appartenant pas au service. Inchangé : omettre l'agent est légal, en fournir un incohérent ne l'est pas |
| `VALIDATION_ERROR` | 400 | `GET /visitors` | `search` de moins de 3 caractères |
| `VALIDATION_ERROR` | 400 | `POST /uploads/document` | `face` autre que `recto`/`verso` |
| `UNSUPPORTED_IMAGE` | 400 | `POST /uploads/document` | Extension non autorisée, ou fichier vide |
| `FILE_TOO_LARGE` | 400 | `POST /uploads/document` | Au-delà de 10 Mo |
| `VISIT_NOT_FOUND` | 404 | `DELETE /visits/{id}` | Identifiant inconnu, ou visite déjà supprimée |
| `FORBIDDEN` | 403 | `DELETE /visits/{id}` | Rôle autre qu'`ADMIN` |
| `FORGOTTEN_DOCUMENT_CASE_NOT_FOUND` | 404 | `/forgotten-document-cases/{id}` | Fiche inconnue |
| `FORGOTTEN_DOCUMENT_ALREADY_RESOLVED` | 409 | `POST /forgotten-document-cases/{id}/resolve` | Fiche déjà résolue |
| `VALIDATION_ERROR` | 400 | `PUT /settings` | Heure de clôture hors format `HH:MM` ou valeur hors contraintes |
| `VALIDATION_ERROR` | 400 | Organisations / thème | Couleur non conforme à `#RGB` ou `#RRGGBB` |
| `FORBIDDEN` | 403 | Routes protégées par rôle | Le rôle courant ne possède pas le niveau requis |
| `CROSS_ORGANIZATION_FORBIDDEN` | 403 | Toute route cloisonnée | Bonne permission, mauvaise organisation. **Nouveau** : distinct de `FORBIDDEN`, le rôle est suffisant |
| `UNSCOPED_ACCOUNT` | 403 | Toute route authentifiée | **Nouveau** : compte non-`ADMIN` sans organisation de rattachement. À corriger côté administration |
| `INACTIVE_ORGANIZATION` | 403 | `POST /auth/login`, toute route authentifiée | **Nouveau** : l'organisation du compte est désactivée. Effet immédiat, sessions ouvertes comprises |
| `ORGANIZATION_REQUIRED` | 400 | `POST /users`, `POST /services` | **Nouveau** : rôle ou service créé sans organisation. Affichez un sélecteur, pas un refus |
| `ORGANIZATION_HAS_ACTIVE_VISITS` | 409 | `PATCH /organizations/{id}/status` | **Nouveau** : désactivation refusée, des visiteurs sont présents. `details.visites_en_cours` donne le nombre |
| `ORGANIZATION_NOT_FOUND` | 404 | `/organizations/*` | Organisation inconnue, ou désactivée sur la route publique de thème |
| `DUPLICATE_ORGANIZATION_CODE` | 409 | `POST`/`PUT /organizations` | Code déjà attribué (comparaison insensible à la casse) |
| `UNSUPPORTED_IMAGE` | 400 | `POST /organizations/{id}/logo` | **Nouveau** : le contenu du fichier n'est pas une image PNG/JPEG/WebP/GIF, quelle que soit son extension |
| `FILE_TOO_LARGE` | 400 | `POST /organizations/{id}/logo` | **Nouveau** : logo au-delà de 2 Mo |
| `DUPLICATE_IDENTIFIANT` | 409 | `POST /users`, `POST /organizations/{id}/administrators` | Identifiant déjà utilisé |
| `DUPLICATE_REFERENTIEL` | 409 | `POST /services`, `POST /purposes` | Code ou libellé déjà pris **dans cette organisation** — plus sur toute la plateforme |
| `LAST_ADMIN` | 409 | Désactivation/rétrogradation utilisateur | Dernier `ADMIN` de la plateforme, ou dernier `ORG_ADMIN` d'une organisation |
| `SELF_MODIFICATION_FORBIDDEN` | 409 | Administration utilisateur | Opération sensible sur son propre compte |
| `UNSUPPORTED_IMPORT_FILE` | 400 | `POST /services/import/preview`, `/agents/import/preview` | **Nouveau** : le **contenu** n'est pas un classeur `.xlsx`. Un vrai `.csv` ou `.xls` est refusé ; un `.xlsx` valide mal nommé est accepté. `message` nomme ce qui a été reçu |
| `IMPORT_COLUMNS_MISSING` | 400 | Prévisualisation d'import | **Nouveau** : en-têtes obligatoires absents. `details.colonnes_lues` dit ce qui a été trouvé — souvent une ligne de titre au-dessus des en-têtes |
| `IMPORT_UNCOMPUTED_FORMULA` | 400 | Prévisualisation d'import | **Nouveau** : le classeur porte des formules sans résultat en cache ; ces cellules seraient importées vides |
| `FILE_TOO_LARGE` | 400 | Prévisualisation d'import | Classeur au-delà de 10 Mo |
| `ORGANIZATION_REQUIRED` | 400 | Routes d'import | **Nouveau** : `ADMIN` sans `organization_id`. Affichez un sélecteur, pas un refus |
| `ORGANIZATION_NOT_FOUND` | 404 | Routes d'import | **Nouveau** : `organization_id` inconnu. Répondait `500` auparavant sur les créations unitaires |
| `IMPORT_TOO_MANY_ROWS` | 422 | Routes d'import | **Nouveau** : au-delà de `referentiel_import_max_rows` (5 000 par défaut). `details.max_rows` donne le plafond |
| `IMPORT_PLAN_REJECTED` | 422 | `POST /services/import`, `/agents/import` | **Nouveau** : au moins une ligne en erreur — **rien n'est écrit**. `details.lignes` liste les fautives |


## 13. Où trouver le reste

- **Contrat complet et à jour** : `/docs` (Swagger) sur l'environnement de recette.
  Les nouveaux champs y sont décrits un par un.
- **Pourquoi ces choix** : `ARCHITECTURE_DECISIONS.md`, ADR-016 (NIN alphanumérique),
  ADR-017 (réenregistrement et double présence), ADR-018 (recto/verso et conservation),
  ADR-019 (personne rencontrée facultative), ADR-020 (périmètre d'organisation),
  ADR-021 (paramètre de requête inconnu refusé), ADR-022 (import en masse).

### 13.1 Migration de la base

Les migrations sont désormais disponibles jusqu'à la révision Alembic
`e5b7c9d1a204`. La migration `d2f4a8b91c77` couvre la clôture automatique, les
pièces oubliées et le branding ; `e5b7c9d1a204` ajoute la valeur
`ORG_ADMIN` à l'ENUM des rôles. La correction récente
concernait uniquement l'exécution PostgreSQL :

- compatibilité des timestamps avec le fuseau horaire ;
- création explicite des types ENUM PostgreSQL `checkout_origin` et
  `forgotten_document_status`.

Cette correction ne modifie pas le contrat HTTP. Le frontend n'a aucune migration
locale à effectuer et ne doit pas manipuler directement les ENUM ou la base de
données. Continuez à utiliser les chaînes JSON documentées (`"MANUAL"`,
`"AUTOMATIC"`, `"PENDING"`, etc.).

Les timestamps renvoyés par l'API restent des dates ISO 8601 avec décalage de
fuseau, par exemple `2026-09-04T17:02:13+00:00`. Utilisez un parseur de date
tenant compte du fuseau et évitez de comparer directement ces valeurs avec
l'heure locale sans conversion explicite.

Une question sur un contrat, un cas de terrain qui ne rentre pas dans ce qui est
décrit ici : remontez-le, c'est le bon moment.


---

## 14. Évolutions d'octobre 2026 — durcissement SaaS

Ces changements accompagnent l'ouverture de la plateforme à plusieurs ministères.
Aucun champ ne disparaît ; trois comportements changent. Décisions détaillées :
ADR-023 et ADR-024.

### 14.1 URLs de fichiers signées — mobile et dashboard

En production, `signature_url`, `document_recto_url`, `document_verso_url`,
`mrz_image_url` et l'`url` renvoyée par `/uploads` portent une signature valable
**une heure** :

```
/storage/uploads/org/<organisation>/signatures/2026/10/8f2c….png?md5=Xy3…&expires=1760630400
```

- **Affichez l'URL telle quelle** (préfixée de `baseUrl`), sans retirer ni
  recomposer la requête : sans elle, le serveur répond `403`.
- **Renvoyez-la telle quelle** dans `POST /visits` : l'API retire elle-même la
  signature et l'hôte éventuel avant d'enregistrer.
- **`410 Gone` sur une image** : l'URL a expiré. Relisez la visite (ou la fiche)
  pour en obtenir une fraîche, puis réaffichez. Ne mettez pas ces URLs en cache
  au-delà de l'heure.
- Les **logos d'organisation** restent publics, sans signature.

Une URL qui ne désigne pas un fichier du stockage (lien externe, chemin
arbitraire) est refusée en `400 VALIDATION_ERROR`.

### 14.2 Références d'une autre organisation — mobile et dashboard

`POST /visits`, `POST /visits/sync` et `PATCH /visits/{id}` refusent désormais
toute référence qui n'appartient pas à l'organisation de la visite :

| Cas | Réponse |
|---|---|
| `service_id` ou `purpose_id` d'une autre organisation (un motif **commun** reste accepté) | `403 CROSS_ORGANIZATION_FORBIDDEN` |
| Fichier déposé par une autre organisation | `403 CROSS_ORGANIZATION_FORBIDDEN` |
| `visitor_id` d'une personne jamais venue dans l'organisation | `404 VISITOR_NOT_FOUND` |

Dans `POST /visits/sync`, ces cas sortent en `status: "error"` sur la ligne
concernée ; le reste du lot est enregistré. En pratique, ils ne surviennent
qu'avec un cache de référentiels resté sur une autre organisation (compte changé
sur la tablette) : videz le cache à la connexion d'un autre compte.

**Comportements assouplis**, sans action requise :
- `VISITOR_ALREADY_PRESENT` ne se déclenche plus que pour une visite ouverte
  **dans la même organisation** : une personne présente dans un autre ministère
  peut entrer chez vous.
- `client_reference` n'a plus besoin d'être unique que **dans votre organisation**.

### 14.3 Sessions — mobile

- Un refresh token n'est accepté que si sa session est active. **Une seule
  reconnexion** peut être demandée après la mise à jour, aux comptes dont la
  session précède le suivi des sessions.
- Sur un `401` de `POST /auth/refresh`, renvoyez vers l'écran de connexion : la
  session a été coupée (tablette révoquée, compte désactivé, mot de passe
  réinitialisé).
- Enregistrez toujours le `refresh_token` renvoyé par `/auth/refresh` quand il
  n'est pas `null`. L'ancien continue de fonctionner jusqu'à son expiration, mais
  tombe avec la session.

### 14.4 OCR serveur de secours — mobile et dashboard

- `GET /ocr/status` → `{"enabled": bool}`, ouvert à tout compte connecté.
- Si ML Kit échoue et que `enabled` vaut `true`, envoyez la photo à
  `POST /ocr/scan` (`receiveTimeout` ≥ 60 s). Désactivé, la route répond
  `503 SERVER_OCR_DISABLED` : proposez la saisie manuelle.
- **Dashboard (`ADMIN` uniquement)** : une case à cocher sur le paramètre
  `server_ocr_enabled` de `GET/PUT /settings`.

### 14.5 Nouveaux codes et statuts

| Code | HTTP | Où | Conduite à tenir |
|---|---|---|---|
| `CROSS_ORGANIZATION_FORBIDDEN` | 403 | Création, synchro, correction de visite | Rafraîchir les référentiels ; ne pas réessayer tel quel |
| `SERVER_OCR_DISABLED` | 503 | `POST /ocr/scan` | Saisie manuelle |
| — | 403 / 410 | Chargement d'une image | `403` : URL modifiée. `410` : relire la visite |
| — | 429 | Toute route | Trop de requêtes : attendre et réessayer avec temporisation |