# Guide d'intégration front — évolutions d'août 2026

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

### Les trois seuls points qui cassent quelque chose

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

Tout le reste est additif : le code existant continue de fonctionner à l'identique.

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

### 7.3 Supprimer définitivement une visite

```http
DELETE /api/v1/visits/{visit_id}
Authorization: Bearer <access_token>     → 204 No Content, corps vide
```

Réservé au rôle `ADMIN`. La ligne quitte le registre **pour de bon** : elle n'est
plus lisible par `GET /visits/{id}`, disparaît du listing, de l'export et de toutes
les statistiques.

| Réponse | Quand |
|---|---|
| `204` | Supprimée. Aucun corps à parser — ne tentez pas de lire du JSON |
| `403 FORBIDDEN` | Appelée par un agent de contrôle ou un superviseur |
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

## 8. Checklist d'intégration

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

- [ ] **Protéger tous les accès à `visit.agent`** dans le registre et l'export
- [ ] Sélecteur de personne rencontrée facultatif, et `{"agent_id": null}` pour la
      retirer d'une visite mal saisie
- [ ] Présenter `top-agents` comme un **palmarès**, pas une répartition : son total
      est inférieur au nombre de visites
- [ ] Champ de saisie du mot de passe dans l'écran de réinitialisation (12 car. min)
- [ ] Réglage `document_images_retention_days` dans l'écran des paramètres
- [ ] Afficher `document_recto_url` / `document_verso_url` sur le détail d'une visite
      (avec repli sur `mrz_image_url` pour les visites antérieures)
- [ ] **Suppression définitive** d'une visite (`DELETE /visits/{id}`) : confirmation
      explicite, bouton distinct de l'annulation, `404` au second appel traité
      comme un succès

---

## 9. Récapitulatif des codes d'erreur nouveaux ou concernés

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

---

## 10. Où trouver le reste

- **Contrat complet et à jour** : `/docs` (Swagger) sur l'environnement de recette.
  Les nouveaux champs y sont décrits un par un.
- **Pourquoi ces choix** : `ARCHITECTURE_DECISIONS.md`, ADR-016 (NIN alphanumérique),
  ADR-017 (réenregistrement et double présence), ADR-018 (recto/verso et conservation),
  ADR-019 (personne rencontrée facultative).

Une question sur un contrat, un cas de terrain qui ne rentre pas dans ce qui est
décrit ici : remontez-le, c'est le bon moment.
