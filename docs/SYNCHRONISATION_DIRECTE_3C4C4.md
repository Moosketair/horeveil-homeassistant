# Horéveil — Synchronisation directe du calendrier

## Autorisations et écritures

Après l’appairage, le téléphone et Home Assistant doivent chacun autoriser l’envoi du calendrier. Android garde les sélections par alarme et l’horizon séparément des webhooks. Le panneau Home Assistant associe la capacité d’écriture à un téléphone appairé et à une entrée précise. Cette capacité ne permet aucun appel aux services, aux entités ni aux autres API Home Assistant.

Une entrée accepte le webhook local ou un téléphone sélectionné. Les entrées existantes conservent le webhook au départ. Le changement de source efface la projection et l’horodatage visibles, garde les identifiants d’entité et persiste le choix. Lors d’un retour vers un téléphone déjà utilisé, son dernier numéro de révision reste un plancher. Une révision plus récente réamorce la projection. Une liaison directe sélectionnée sur plusieurs entrées chargées est refusée jusqu’à correction du choix.

Le transport est `POST /api/horeveil/direct/v1/calendar` avec le corps JSON [calendar_snapshot v1](CALENDAR_SNAPSHOT_SCHEMA_V1.md) d’origine, limité à 1 Mio. Le profil signé réutilise la clé P-256, l’origine HTTPS et le serveur de l’association. Le tag est `horeveil-calendar-v1`. La signature couvre la méthode, la destination canonique, le type et le digest du corps et l’identifiant du serveur. La liaison doit être active, la signature valide, son nonce inédit et l’identité d’installation du corps cohérente. Les redirections sont refusées par Android.

Home Assistant accepte une nouvelle révision après vérification de son écriture dans le Store. Une révision identique avec un nonce neuf peut recevoir `duplicate`. Le même nonce est refusé, y compris après redémarrage. L’autorisation, la réservation du nonce, l’écriture et la révocation sont ordonnées par le registre. Les erreurs de signature, la révocation, l’absence d’autorisation, les conflits de révision et l’indisponibilité du Store ne produisent aucun acquittement positif.

Android utilise la projection et le travail WorkManager des calendriers existants. Corps chiffré, révision, consentement et acquittement sont suivis par destination. En cas d’indisponibilité, WorkManager diffère la livraison. Une projection périmée ou modifiée est reconstruite et chaque nouvel essai direct est signé avec un nonce neuf. Une destination webhook et Home Assistant ont des travaux de livraison indépendants. Android peut retarder le traitement en arrière-plan selon son état de veille, la connectivité et les restrictions d’énergie.

## Retrait et conservation

| Action | Résultat |
| --- | --- |
| Retirer une alarme ou la supprimer | L’ancien corps en attente est invalidé. La prochaine projection complète retire l’alarme. |
| Désactiver le partage direct sur Android | Le contenu en attente est remplacé par une projection vide. La livraison de ce retrait se poursuit jusqu’à l’acquittement ou jusqu’à une intervention nécessaire côté association. |
| Oublier la liaison sur Android | La liaison, les consentements et le corps en attente sont effacés sur le téléphone. La révocation dans le panneau HA retire les données distantes. |
| Révoquer dans Home Assistant | La liaison est révoquée durablement et sa projection est retirée du récepteur. Pour une entrée déchargée, le retrait est effectué avant sa prochaine remise en service. Une erreur de stockage peut différer la suppression sur disque. Le webhook est réactivable par un choix explicite de source. |
| Supprimer une destination webhook Android | Les envois vers cette destination cessent. L’association Home Assistant reste disponible. L’effacement chez le destinataire dépend du système tiers. |

Un retrait hors connexion prend effet côté Home Assistant à la prochaine livraison réussie, ou par révocation sur le serveur. Une publication déjà en cours peut précéder le retrait. La révision du retrait empêche ensuite la réapparition de la projection précédente. La suppression de la projection active n’efface pas l’historique du Recorder, les anciennes sauvegardes ni les copies externes.

## Recette Home Assistant Container et Galaxy S25+

1. Dans **Paramètres → Système → Sauvegardes**, créer une sauvegarde manuelle incluant Home Assistant, la conserver hors du serveur et garder la clé de secours si elle est chiffrée. Relever les cinq identifiants d’entité Horéveil. Depuis HACS, ouvrir **Horéveil → ⋮ → Retélécharger → Besoin d’une autre version ?**, choisir **0.4.0-beta.1**, puis redémarrer Home Assistant. Conserver l’entrée existante et vérifier ses cinq entités, son dernier instantané frais et le webhook avant de changer de source.
2. Installer l’APK Debug avec le même certificat que l’application existante. Utiliser une adresse HTTPS déjà configurée dans HA, accessible depuis le téléphone et reconnue par le système. Vérifier l’accès depuis le LAN puis le réseau mobile ou le VPN selon le trajet habituel. Une installation uniquement HTTP doit disposer d’un trajet HTTPS avant cette recette.
3. Ouvrir **Horéveil** dans Home Assistant, sélectionner l’adresse et générer un QR. Sur Android, ouvrir **Réglages → Intégrations → Home Assistant**, scanner ou saisir l’invitation et comparer les codes. Approuver dans Home Assistant puis confirmer sur Android. Vérifier que le partage est initialement désactivé.
4. Dans **Synchronisation du calendrier** du panneau, sélectionner le téléphone pour l’entrée existante. Activer le partage sur Android, choisir une seule alarme de recette puis utiliser **Synchroniser maintenant**. Vérifier la date de réception, l’alarme dans le calendrier et les capteurs. Ajouter une deuxième alarme, modifier puis supprimer une occurrence et vérifier la synchronisation automatique. Retirer tous les consentements et vérifier le calendrier vide.
5. Couper le réseau et modifier la sélection. Vérifier l’état en attente et le bon déclenchement d’une alarme de recette. Rétablir l’accès, vérifier la livraison des seules données encore autorisées puis désactiver le partage. Réactiver, redémarrer Core et le téléphone, puis vérifier la reprise et la restauration sur la même entrée.
6. Révoquer le téléphone depuis le panneau. Vérifier la disparition des données actives, puis un refus de synchronisation sur Android. Vérifier qu’une destination webhook indépendante continue à fonctionner. Refaire un appairage uniquement si souhaité.
7. Pour vérifier spécifiquement le webhook Node-RED historique, remettre l’entrée en source **Webhook local** et envoyer depuis un relais local avec les identifiants déjà configurés. Vérifier le JSON `accepted` ou `duplicate`. Depuis le WAN, vérifier l’absence de changement du récepteur avec une charge de recette autorisée à être utilisée sur ce banc. Core peut répondre 200 sans acquitter le webhook. L’accès historique dépend du contrôle local natif Core et de son jeton. Utiliser le proxy HA existant avec sa configuration de confiance normale. Garder les identifiants, les chemins privés et les données hors des traces de recette.

### Retour arrière

Pour revenir au webhook avec ce code, révoquer ou désactiver le partage direct puis choisir **Webhook local** sur l’entrée. Demander un envoi local valide et vérifier les entités avant tout retour à l’ancien composant. Un Store dont la projection a été effacée doit être repeuplé avec un instantané webhook compatible avant de réinstaller la version 0.3.0, ou restauré depuis la sauvegarde cohérente prise avant l’essai. Les réglages webhook d’Android restent disponibles.

Pour revenir à **0.3.0** depuis HACS, choisir `main` dans **Retélécharger → Besoin d’une autre version ?**, puis redémarrer Home Assistant. Si ce choix est indisponible ou si le webhook ne peut pas repeupler la projection, restaurer la sauvegarde préalable depuis Home Assistant. Une restauration complète revient aussi sur les autres changements effectués depuis cette sauvegarde.

## Vérifications du 25 novembre 2026

- 67 tests Python réussis. Les cas ajoutés couvrent la sélection de la source, le mauvais identifiant d’installation, le corps falsifié, le rejeu après redémarrage, le doublon, les révisions antérieures, l’échec de persistance et la révocation concurrente.
- Avec Core 2026.11.2, `tests/qualify_native_transport.py --signed` a exercé HTTPS natif puis Nginx 1.30.8 avec l’include standard NPM 2.17.1. Admin et non-admin, consentement serveur, acceptation, rejeu, retrait, révocation, conflit webhook/direct et traitement du webhook WAN ont été vérifiés. Le conteneur NPM complet reste à tester pour ce jalon.
- Avec l’APK instrumenté sur API 37, `tests/qualify_native_transport.py --signed --adb <adb> --android-serial emulator-PORT` a testé l’appairage Keystore, la projection, le POST RFC 9421 et le retrait acquitté par Core. Le certificat de recette est approuvé uniquement par le client du test instrumenté. La vérification TLS et le contrôle du nom restent actifs. Sur ce banc, la livraison est appelée par le test. La cadence réelle de WorkManager reste à vérifier sur le téléphone.
- `tests/real_upgrade_030.py` a vérifié la mise à niveau d’une entrée publiée 0.3.0, ses données, ses cinq entités et son webhook. Hassfest local a validé l’intégration. Les métadonnées HACS ont été contrôlées localement. L’action HACS officielle reste à exécuter lors d’une publication autorisée.

## Étape 3C4C.5

Valider l’installation au-dessus de 0.3.0 dans Home Assistant Container et la recette S25+ avec le proxy réel. Répéter sur NPM en conteneur avec configuration standard, puis sur une instance Home Assistant Link et un téléphone Android 8. Tester la reprise réelle WorkManager après Doze, redémarrage et transition Wi-Fi, réseau mobile ou VPN. Vérifier le retour au webhook, l’onglet Android des consentements et la conservation des identifiants. Relever les compatibilités observées avant toute publication.
