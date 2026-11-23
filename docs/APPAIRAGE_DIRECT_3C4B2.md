# Horéveil · protocole d’appairage interne

Les trois gestionnaires HTTP sont exercés sur un serveur de test. Home Assistant ne les enregistre ni au démarrage ni à la création d’une entrée Horéveil. L’adaptateur refuse les requêtes par défaut. Un futur raccordement devra vérifier le TLS et la provenance locale à partir de données d’entrée fiables, puis fournir un mécanisme d’activation explicite et une interface administrateur vérifiée. Le webhook historique conserve son accès local. [Conception](APPAIRAGE_DIRECT_3C4A.md) · [Registre](APPAIRAGE_DIRECT_3C4B1.md)

## Échanges de test

| Route | Requête | Réponse utile |
| --- | --- | --- |
| `POST /api/horeveil/v1/pair/claim` | En-tête `X-Horeveil-Invitation` avec le code Crockford canonique de 26 caractères. JSON avec `claim_id`, `installation_id`, `phone_label` et `credential_digest`. | `pairing_id`, `server_id`, `challenge` et `state`. Une répétition exacte retrouve les mêmes identifiants et le même défi jusqu’à la consommation, avec l’état courant. |
| `GET /api/horeveil/v1/pair/status` | En-tête `X-Horeveil-Authorization: v1 <pairing_id>.<secret>`. | `pairing_id`, `server_id` et `state` parmi `awaiting_approval`, `approved`, `active`. |
| `POST /api/horeveil/v1/pair/activate` | Même en-tête, JSON `{"confirmed":true}` après confirmation explicite dans Android. | Les mêmes champs que le statut. `active` est renvoyé après persistance, ou lors d’un réessai authentifié d’une liaison déjà active. |

Les UUID utilisent leur forme canonique en minuscules. Le secret du téléphone fait 32 octets. Son SHA-256 est transmis dans la réservation et sert à vérifier le secret brut au statut et à l’activation. Le secret, l’empreinte et le défi sont encodés en Base64 URL sans remplissage sur le réseau. Les réponses ne contiennent ni secret ni empreinte et ne sont pas mises en cache. Les corps JSON sont limités à 4 Kio. L’adaptateur compte les requêtes par fenêtre de 60 secondes, avec 30 par source locale validée et 120 au total. Une limite atteinte renvoie `429` avec `Retry-After`. Une absence d’accès de confiance renvoie `404`. Les identifiants invalides ou expirés renvoient `401`, un conflit `409`, et une activation sans approbation `403`.

## Approbation et reprise

L’interface HA future relira la candidature par identifiant d’invitation via une opération interne qui reçoit l’utilisateur authentifié de Home Assistant. Seul l’administrateur actif propriétaire de l’invitation peut relire et approuver la candidature. Il compare le code affiché sur les deux écrans avant de confirmer. Le code encode les 40 premiers bits du HMAC-SHA-256 en Crockford, avec l’empreinte du secret comme clé. Le message ASCII joint `horeveil-pair-v1`, `server_id`, `pairing_id`, `claim_id` et `challenge` avec `|`. Android peut le calculer localement. Aucun jeton HA ni point d’entrée administrateur n’est fourni au téléphone.

Le délai de cinq minutes court dès la création de l’invitation. Réserver ou approuver ne le prolonge pas. Annulation et redémarrage effacent les candidatures en attente. Une activation persistée reste consultable après redémarrage avec le même identifiant et le même secret. Si une réponse à la réservation se perd, renvoyer exactement la même candidature. Si la réponse à l’activation se perd, relire le statut ou réessayer l’activation. En cas de `401` après redémarrage sur une candidature inachevée, repartir d’une nouvelle invitation. Un téléphone révoqué perd immédiatement son accès au statut et à l’activation.
