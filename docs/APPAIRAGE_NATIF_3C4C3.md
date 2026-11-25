# Horéveil — Recette de l’appairage natif 3C4C.3

Home Assistant `feature/3c4c3-native-pairing` part de `322ee3a`, la qualification des vues natives et de RFC 9421. Android `codex/3c4c3-native-pairing` part du dépôt principal propre à `0bd42b0`, qui contient déjà le scanner, la saisie manuelle et le stockage privé d’appairage. La branche NPM de sauvegarde reste à `01b92d8`.

## Prérequis de la recette

- Une instance de test Home Assistant avec l’intégration Horéveil et son frontend. L’adresse HTTPS interne ou externe configurée dans Home Assistant doit être joignable depuis Android et son certificat reconnu normalement par le téléphone. Un proxy déjà présent utilise la configuration standard Home Assistant. Le trajet de ce proxy vers Core doit rester protégé. Aucun nouveau port, domaine, fichier YAML ou secret de relais n’est requis pour le nouveau parcours.
- L’APK Debug de la branche Android, version `2.7.4`, build `2`, et une session administrateur sur l’instance de recette. Conserver l’entrée HA existante. Pour une installation Debug déjà signée avec le même certificat, mettre à jour l’APK par-dessus. Le commit Debug figure dans Réglages → Fiabilité.

## Parcours manuel

1. Ouvrir **Horéveil** depuis la fiche de l’intégration ou la barre latérale HA. Choisir l’une des adresses HTTPS proposées, confirmer, puis afficher le QR de cinq minutes.
2. Sur Android, ouvrir **Réglages → Intégrations → Home Assistant**. Scanner le QR ou saisir l’adresse et le code. Vérifier l’adresse affichée et confirmer le contact. Attendre le même code de comparaison des deux côtés.
3. Comparer les codes, approuver dans Home Assistant, puis confirmer sur Android. Le téléphone affiche une liaison active après vérification. Le calendrier et les autres droits de partage restent désactivés pour cette association. Vérifier avec un compte HA non administrateur que les opérations du panneau sont refusées.
4. Répéter avec deux téléphones pour la même invitation, une invitation expirée, puis un refus via **Annuler l’invitation** dans HA. Sur Android, annuler avant confirmation et vérifier qu’aucune liaison n’est activée. Couper aussi le réseau avant puis après confirmation et réessayer. Un brouillon interrompu par le redémarrage de HA nécessite un nouveau QR. Après activation, les relances de HA et d’Android restaurent la liaison si sa clé existe toujours.
5. Révoquer côté HA et vérifier que l’ancienne liaison échoue dès la prochaine vérification du téléphone. Oublier la liaison sur Android puis créer une invitation neuve. En cas de clé perdue ou d’application réinstallée, révoquer l’ancienne liaison côté HA et relancer explicitement l’appairage. Un oubli local d’une liaison active ne la révoque pas côté serveur.
6. Vérifier la réception Node-RED et les entités existantes avec une autre publication de calendrier. Le [contrat `calendar_snapshot` v1](CALENDAR_SNAPSHOT_SCHEMA_V1.md) continue à s’appliquer au webhook local.

## Profil signé et sécurité

Le format QR reste `v:1` avec `origin`, `code` et `expires_at`. Le client livré utilise exclusivement `/api/horeveil/direct/v1/{claim,enroll,status,activate,cancel}` et exige la réponse `profile: signed-v1`. La candidature initiale est authentifiée par le code QR et contient le digest du secret temporaire du téléphone. L’enrôlement, le statut, la confirmation et l’annulation utilisent POST et le justificatif de candidature, avec une preuve RFC 9421 liée à la clé P-256 d’Android Keystore. Le statut et l’annulation signent `{}`. La confirmation signe `{"confirmed":true}`. L’enrôlement suit le [transcript qualifié](QUALIFICATION_TRAJET_NATIF_3C4C2.md). L’administrateur ne peut approuver qu’après enrôlement. La clé reste figée jusqu’à la confirmation et ne donne accès à aucune API générale HA.

La signature couvre la méthode, l’URL canonique, le type et le digest du corps et l’identifiant du serveur. La cible est reconstruite depuis l’origine confirmée de la liaison et le chemin fixe. Le certificat reste vérifié et les redirections sont refusées par Android. Les en-têtes réseau n’authentifient aucune opération. Le registre persiste un nonce avant toute transition, avec au plus 64 nonces vivants par liaison et 4 096 pour l’instance. Une fenêtre pleine renvoie `429`. Les nonces expirés sont purgés au prochain enregistrement. Le plancher de temps conservé sur disque empêche une réactivation par recul d’horloge après purge. Un réessai crée un nouveau nonce. L’approbation et la clé restent cohérentes sous le même verrou que la révocation. Une liaison révoquée n’est jamais réactivée par une restauration.

L’intégration charge `http-message-signatures` 3.0.0 depuis son manifeste. Android utilise Authlete 1.8 et Nimbus 10.10 pour convertir la signature DER de Keystore au format `R‖S` du RFC. La clé privée reste dans Keystore et ne requiert pas de biométrie par requête. [Authlete](https://github.com/authlete/http-message-signatures) · [Nimbus](https://central.sonatype.com/artifact/com.nimbusds/nimbus-jose-jwt/10.10) · [pyauth](https://pypi.org/project/http-message-signatures/3.0.0/)

Les options YAML de l’ancien banc sélectionnent toujours son profil de recette. Une configuration de recette historique et le client Android signé n’utilisent pas les mêmes routes. Le profil grand public s’ouvre par défaut depuis le frontend de Home Assistant avec une adresse HTTPS existante.

## Vérifications locales

- 65 tests Python réussis. Les nouveaux cas couvrent notamment deux candidatures concurrentes, l’enrôlement et l’activation avec la même clé, le rejeu après redémarrage, le remplacement de clé refusé, la révocation et la saturation de la fenêtre de nonces.
- Core 2026.11.2 a exercé les routes du composant installé, l’approbation via WebSocket, le refus du rejeu et la révocation. La recette `tests/qualify_native_transport.py --signed` est passée en HTTPS direct et avec Nginx 1.30.8 utilisant l’include standard de NPM 2.17.1. Sur ce dernier trajet, une adresse WAN est simulée. Le webhook historique refuse l’écriture et reste utilisable en local.
- `tests/real_upgrade_030.py` a réouvert une entrée 0.3.0 sans changer le calendrier, les quatre capteurs ni le webhook. Le nouveau panneau démarre sans association préexistante sur ce profil.
- La validation officielle `hassfest` a été exécutée localement. Les métadonnées et le workflow HACS ont également été vérifiés localement. L’action HACS officielle, le conteneur NPM complet, Link et le Galaxy S25+ restent à qualifier pour cette version.

## Fichiers Home Assistant

- `custom_components/horeveil/` — `__init__.py`, `manifest.json`, `pairing.py`, `pairing_ha.py`, `pairing_http.py`, `pairing_panel.js`, `pairing_signed.py`.
- `tests/` — `qualify_native_transport.py`, `real_upgrade_030.py`, `test_pairing_ha.py`, `test_pairing_signed.py`, `test_receiver.py`.
- `requirements-test.txt` et ce guide.

## Synchronisation directe

La [recette 3C4C.4](SYNCHRONISATION_DIRECTE_3C4C4.md) décrit la publication signée du calendrier après autorisation sur les deux appareils.
