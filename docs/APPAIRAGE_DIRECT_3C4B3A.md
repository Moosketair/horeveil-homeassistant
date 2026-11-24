# Horéveil · recette d’appairage sur Home Assistant isolé

Le raccordement est désactivé par défaut. Il se limite à une recette sur l’hôte de l’instance de test, avec un simulateur de téléphone. Aucun changement Android, Node-RED ou Home Assistant de production n’est requis. [Protocole HTTP](APPAIRAGE_DIRECT_3C4B2.md)

## Conditions d’ouverture

Home Assistant doit écouter uniquement sur `127.0.0.1` et exiger lui-même un certificat client. Horéveil vérifie la configuration HTTP réellement appliquée avant d’enregistrer les trois vues et les quatre commandes WebSocket. Sur chaque appel, il vérifie les deux extrémités de la socket, le TLS mutuel, l’empreinte SHA-256 du certificat client et sa clé RSA d’au moins 2048 bits. Il refuse les requêtes Cloud et les en-têtes de relais. Un serveur sans ces garanties garde les routes fermées. Les commandes WebSocket exigent en plus un administrateur HA authentifié, actif et propriétaire de l’invitation. [TLS dans le Core](https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/components/http/server.py) · [Traitement des proxys](https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/components/http/forwarded.py) · [require_admin](https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/components/websocket_api/decorators.py)

Utiliser un profil Core jetable, hors Cloud et sans proxy ni transfert de port vers la boucle locale. Une redirection TCP transparente organisée par l’administrateur pourrait masquer la provenance réseau. Conserver donc la clé privée du certificat client uniquement sur cet hôte de test. Préparer une CA de test, un certificat serveur avec le SAN IP `127.0.0.1` et un certificat client RSA réservé à la recette. Les usages étendus sont respectivement `serverAuth` et `clientAuth`. Placer les clés hors du dépôt et protéger leur lecture. Configurer l’instance isolée ainsi, avec des chemins absolus adaptés.

```yaml
http:
  server_host: ["127.0.0.1"]
  server_port: 8123
  ssl_certificate: /chemin/prive/serveur.pem
  ssl_key: /chemin/prive/serveur.key
  ssl_peer_certificate: /chemin/prive/ca.pem

horeveil:
  local_pairing_test_client_sha256: "<SHA-256 du certificat client DER, 64 hexadécimaux minuscules>"
```

Calcul de l’empreinte avec OpenSSL, sans utiliser la clé privée.

```sh
openssl x509 -in client.pem -noout -fingerprint -sha256
```

Retirer les deux-points et convertir l’empreinte en minuscules. Redémarrer le profil de test. Depuis une autre machine, le port doit être inaccessible. Sur la boucle locale, une connexion sans certificat client doit échouer à l’étape TLS. Un autre certificat accepté par la CA doit recevoir `404` sur les vues. Avec le certificat épinglé, un appel à `GET /api/horeveil/v1/pair/status` sans authentifiant Horéveil doit recevoir `401`.

## Échanges

Se connecter à `wss://127.0.0.1:8123/api/websocket` avec le certificat et sa clé, vérifier la CA serveur puis s’authentifier avec un compte administrateur du profil de test. Garder le jeton HA dans le client administrateur en mémoire. Le simulateur utilise le certificat de recette et les seuls authentifiants Horéveil du protocole HTTP.

| Type WebSocket | Champs en plus de `id` et `type` | Résultat |
| --- | --- | --- |
| `horeveil/pairing_test/invite` | `origin` confirmé, ici `https://127.0.0.1:8123` | `invitation_id`, `origin`, `expires_at`, `code_encrypted`. |
| `horeveil/pairing_test/status` | `invitation_id` | `issued`, `awaiting_approval` ou `approved`. Après réservation, `pairing_id` et `comparison_code_encrypted`. Une invitation consommée, annulée ou expirée est indisponible. |
| `horeveil/pairing_test/approve` | `invitation_id`, `pairing_id`, `confirmed: true` après comparaison | `approved`. |
| `horeveil/pairing_test/cancel` | `invitation_id` | `cancelled` pour une invitation encore en cours. |

Les codes sont chiffrés dans les réponses WebSocket, car le Core peut journaliser des trames complètes. Décoder le champ Base64 URL sans remplissage et le déchiffrer avec la clé privée client en RSA-OAEP, SHA-256 pour OAEP et MGF1, sans label. Le texte clair est en ASCII. Les commandes n’envoient aucun code en retour à HA. [Journalisation WebSocket du Core](https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/components/websocket_api/http.py)

1. Générer une invitation, déchiffrer son code en mémoire et envoyer `POST /pair/claim` selon le protocole, avec des UUID et un secret aléatoires de test. Répéter la même requête pour vérifier la stabilité de la réponse.
2. Essayer `POST /pair/activate` avec le secret du simulateur et `confirmed: true`. Le résultat attendu est `403`. Obtenir le code de comparaison par WebSocket, le déchiffrer, puis le comparer avec celui calculé à partir du défi côté simulateur.
3. Approuver par WebSocket, puis confirmer côté simulateur et vérifier `active`. Répéter l’activation et la lecture du statut avec le même identifiant. Vérifier qu’une ancienne candidature non activée est refusée après redémarrage, tandis que l’association persistée reste lisible.
4. Retirer le bloc `horeveil` de ce profil puis redémarrer. Vérifier l’absence des routes et des commandes. Arrêter ensuite le profil de test et supprimer ses certificats et données jetables. Le listener d’arrêt invalide également les invitations encore en mémoire.

Pour la recette automatisée, lancer `python -m unittest discover -s tests -p test_pairing_ha.py -v` après installation de `requirements-test.txt`. Elle utilise de vraies connexions TLS et des doublures des API Home Assistant.
