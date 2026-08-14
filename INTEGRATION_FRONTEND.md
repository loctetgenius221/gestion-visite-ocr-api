# Guide d'intégration front — évolutions du 14 août 2026

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

### Les deux seuls points qui cassent quelque chose

1. **`POST /visits` peut désormais répondre `409 VISITOR_ALREADY_PRESENT`.** Si vous
   ne traitez pas ce code, l'agent verra une erreur générique là où il devrait voir
   « déjà présent depuis 10h30 — clôturer ? ». **C'est le point à traiter en premier.**
2. **Une validation client « NIN = 13 chiffres » rejettera des cartes valides.**
   Le champ est alphanumérique.

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

## 6. Dashboard web uniquement

### 6.1 Mot de passe d'un agent : saisi plutôt que généré

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

**12 caractères minimum**, 128 maximum. Le `null` en réponse est normal : le serveur
ne restitue que ce qu'il a lui-même généré.

Dans les deux cas, **toutes les sessions du compte sont coupées** — un mot de passe
réinitialisé l'est souvent parce qu'il a fuité.

Même chose à la création : `POST /users` accepte un `mot_de_passe` optionnel.

> À prévoir dans l'écran : un mot de passe saisi par l'admin reste connu de l'admin.
> Un forçage du changement à la première connexion n'existe pas encore côté API —
> dites-nous si vous le voulez.

### 6.2 Nouveau réglage : conservation des photos de pièces

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

---

## 7. Checklist d'intégration

### App mobile — par ordre de priorité

- [ ] **Traiter `409 VISITOR_ALREADY_PRESENT`** sur `POST /visits` : écran « déjà
      présent », bouton de clôture depuis `details.visit_id`, puis rejeu
- [ ] **Relâcher la validation du NIN** en 13 caractères alphanumériques
- [ ] Vérifier qu'aucun repli sur `numero_document` ne subsiste quand `nin` est nul
- [ ] Écran de recherche `GET /visitors` (3 caractères min, debounce, tri déjà fait
      côté serveur)
- [ ] Enregistrement via `visitor_id` + confirmation des champs `visitor_passage`
- [ ] Griser l'enregistrement quand `visite_ouverte_id` est non nul, proposer la
      clôture
- [ ] Capture recto/verso sur le parcours de **saisie manuelle** uniquement, via
      `POST /uploads/document?face=…`
- [ ] Présenter les items `conflict` de `POST /visits/sync` à l'agent

### Dashboard web

- [ ] Champ de saisie du mot de passe dans l'écran de réinitialisation (12 car. min)
- [ ] Réglage `document_images_retention_days` dans l'écran des paramètres
- [ ] Afficher `document_recto_url` / `document_verso_url` sur le détail d'une visite
      (avec repli sur `mrz_image_url` pour les visites antérieures)

---

## 8. Récapitulatif des codes d'erreur nouveaux ou concernés

| Code | HTTP | Route | Quand |
|---|---|---|---|
| `VISITOR_ALREADY_PRESENT` | 409 | `POST /visits`, `/visits/sync` | Le visiteur a déjà une visite `PRESENT` |
| `VISITOR_NOT_FOUND` | 404 | `POST /visits` | `visitor_id` inconnu |
| `VALIDATION_ERROR` | 400 | `POST /visits` | `visitor` + `visitor_id` ensemble, ou aucun des deux ; `visitor_passage` avec `visitor` |
| `VALIDATION_ERROR` | 400 | `GET /visitors` | `search` de moins de 3 caractères |
| `VALIDATION_ERROR` | 400 | `POST /uploads/document` | `face` autre que `recto`/`verso` |
| `UNSUPPORTED_IMAGE` | 400 | `POST /uploads/document` | Extension non autorisée, ou fichier vide |
| `FILE_TOO_LARGE` | 400 | `POST /uploads/document` | Au-delà de 10 Mo |

---

## 9. Où trouver le reste

- **Contrat complet et à jour** : `/docs` (Swagger) sur l'environnement de recette.
  Les nouveaux champs y sont décrits un par un.
- **Pourquoi ces choix** : `ARCHITECTURE_DECISIONS.md`, ADR-016 (NIN alphanumérique),
  ADR-017 (réenregistrement et double présence), ADR-018 (recto/verso et conservation).

Une question sur un contrat, un cas de terrain qui ne rentre pas dans ce qui est
décrit ici : remontez-le, c'est le bon moment.
