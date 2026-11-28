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

La reprise sur Galaxy S25+ est attestée par la recette physique fournie pour Android
2.7.4 build 2 (`fe2a703da929`). Elle couvre l’association existante, la
synchronisation automatique après modification, l’activation et la désactivation,
le partage par réveil et l’indépendance des webhooks. Cette attestation sert de
base au lot d’interface.

## Étape 3C4C.6 — Panneau Home Assistant

La branche locale `feature/3c4c6-native-ha-panel` part du tag
`0.4.0-beta.1` (`247dd3e`). Le panneau reprend la navigation, les icônes et les
variables de thème Home Assistant. Les formulaires et dialogues utilisent des
contrôles HTML pour assurer le fonctionnement au clavier sans importer de
composant frontend interne. La mise en page passe sur une colonne en vue étroite.

- Le téléphone affiche son libellé Home Assistant ou un nom dérivé de son
  identifiant, son état d’association et la dernière réception du calendrier
  attribuable à cette liaison. La date décrit une réception passée. Les
  associations actuelles ne conservent pas de nom système ni de modèle. Une
  valeur provisoire envoyée lors de la demande Android tient lieu de libellé
  dans le protocole existant. Un futur lot pourrait définir un champ de modèle
  explicite, sa persistance et sa disponibilité pour les associations anciennes.
- Le libellé est enregistré dans le Store privé `horeveil.pairing_labels` et
  rattaché à l’identifiant d’association. Un champ vide rétablit le nom généré.
  La commande réutilise le contrôle d’administration sécurisé et écrit seulement
  dans ce Store. L’éligibilité dans le sélecteur correspond au profil signé déjà
  exigé par le serveur.
- L’invitation affiche le QR et le code manuel pendant l’attente du téléphone,
  puis le code de comparaison et l’approbation dans HA, suivis de la confirmation
  Android. Le code et le QR sont retirés quand ils cessent d’être utiles.
  L’expiration et l’annulation rendent la création d’une nouvelle invitation
  disponible.
- Depuis HTTP, le panneau propose une adresse HTTPS interne ou externe
  configurée dans Home Assistant et validée comme origine HTTPS. Le navigateur
  reste responsable de la connexion et de la validation du certificat. Sans
  origine exploitable, il indique où retrouver les réglages réseau.
- La section Calendrier indique la source d’écriture par entrée et demande
  confirmation avant le changement de source, qui efface sa projection. Le
  webhook local demeure sélectionnable. Le téléphone gère le consentement au
  calendrier et la sélection des réveils. La révocation exige une confirmation.

### Vérifications du lot

- 68 tests Python réussis sous Windows avec Python 3.13, aiohttp sur TLS et
  un double de Home Assistant.
  Les tests du lot couvrent le refus d’un administrateur en HTTP et d’un
  non-admin, la validation du libellé, l’échec d’écriture, le redémarrage, la
  conservation octet pour octet du registre, l’accès après renommage, la
  révocation et l’éligibilité des profils. Les régressions existantes couvrent
  aussi l’activation et la synchronisation signées, le rejeu, le changement de
  source, les webhooks et la persistance de la projection.
- Chrome 158 avec Playwright, API `hass` simulée et données de démonstration.
  Rendu sur ordinateur à 1 200 px et mobile à 390 px, thèmes clair, sombre et
  personnalisé. Le navigateur a exercé le QR avec le module embarqué et Web
  Crypto, les confirmations, l’annulation, l’expiration, le renommage, les
  deux sources, le dialogue de révocation au clavier et les deux variantes HTTP.
  Les captures proviennent de ce banc. Le rendu dans le frontend Core et le
  scan depuis l’application Android restent à confirmer avec ce panneau.

### Recette courte sur une instance de validation

1. Installer la branche sur une copie de recette de Home Assistant. Ouvrir
   **Horéveil** avec un compte administrateur en HTTPS, sur ordinateur puis sur
   mobile. Comparer les thèmes clair et sombre. Depuis l’accès HTTP, vérifier le
   lien vers l’origine configurée ou l’aide réseau lorsque celle-ci manque.
2. Renommer un téléphone déjà associé. Redémarrer Home Assistant, vérifier le
   libellé et envoyer le calendrier depuis ce téléphone. Vérifier la mise à jour
   de l’heure de réception et le consentement par réveil réglé sur Android.
3. Sur une entrée de recette, choisir **Webhook local**, puis le téléphone
   HTTPS. Confirmer chaque changement, vérifier l’effacement de la projection et
   demander une livraison par la source choisie.
4. Avec un téléphone de recette, créer et scanner une invitation, comparer les
   codes, approuver dans HA puis confirmer sur Android. Essayer aussi
   l’expiration et l’annulation d’une autre invitation. Révoquer uniquement le
   téléphone de recette et vérifier que ses envois directs sont refusés.

## Étape 3C4C.6.1 — Réserves du contre-audit de `842fc2f`

Le contre-audit avait constaté trois défauts de concurrence et d’affichage.
Il avait confirmé les contrôles d’accès et le caractère définitif de la
révocation. La correction réutilise ces mécanismes.

- Le renommage et la révocation prennent le verrou du Store de libellés dans
  le même ordre. Si le renommage s’achève en premier, la révocation efface le
  libellé. Si la révocation s’achève en premier, le renommage est refusé.
  Une panne du Store des libellés permet toujours de révoquer le téléphone.
  Le démarrage suivant réconcilie le Store avec le registre dès que l’écriture
  est de nouveau possible.
- Core valide l’origine HTTPS du lien du panneau lors de son enregistrement.
  Il examine les URL externe puis interne. Un port 0, des identifiants ou un
  chemin supplémentaire rendent une URL inadmissible. Le navigateur n’ajoute
  que le chemin fixe du panneau. Après un changement d’adresse réseau,
  redémarrer Core pour recalculer ce lien.
- Le panneau distingue un refus explicite, une opération confirmée suivie
  d’un échec d’actualisation et une réponse perdue. Il retire les informations
  concernées tant qu’elles sont périmées. Le bouton **Réessayer** relit
  l’état sans répéter le changement, le renommage ni la révocation.

### Recettes ciblées

- `python -m unittest discover -s tests -p 'test_pairing*.py'` vérifie les
  deux ordres de concurrence avec des barrières, la reprise après échec de
  nettoyage et les origines HTTPS validées à l’enregistrement du panneau.
- `python tests/panel_mutation_browser.py --channel chrome` rejoue les
  trois résultats d’un changement de source, y compris une déconnexion,
  ainsi que l’actualisation après renommage et révocation.
- Avec l’environnement Python 3.14 de Core et Playwright,
  `python tests/real_pairing_smoke.py --native-panel --channel chrome`
  utilise un profil jetable, la boucle locale, des certificats de test et un
  client signé synthétique. L’option `--screenshots <dossier>` enregistre les
  vues après l’association, hors du parcours de saisie des codes.

Les 40 tests Python ciblés et la recette Playwright avec API simulée ont
réussi. Le trajet signé a aussi été exécuté sur Core 2026.11.2, avec
validation du webhook local. Dans le frontend réel 20261028.6 sur Chrome,
le menu ouvre Horéveil. La recette HTTPS valide l’annulation, l’expiration,
le QR décodé par ZXing et accepté par le parseur Kotlin de recette, les deux
confirmations et le choix de la source. Le libellé, la source et l’heure de
réception persistent après l’arrêt et le redémarrage de Core sur le même
profil. Après révocation, le client signé reçoit un refus HTTP 401.
Les captures du frontend réel couvrent les thèmes clair et sombre et une
vue mobile de 390 px. Sur le profil HTTP séparé, le panneau ignore l’URL
externe configurée au port 0, propose l’origine HTTPS interne et les
commandes d’association sont refusées par Core en HTTP.

Sous Windows, les deux bibliothèques audio requises au démarrage du frontend
ont été compilées dans le banc. Core et ses commandes d’administration sont
exercés avec leurs implémentations réelles. Le scan par la caméra Android
reste à vérifier en recette utilisateur.

Les métadonnées locales et la structure distribuée par HACS sont valides.
Hassfest retourne zéro intégration invalide et actionlint valide le workflow.
La validation du téléchargement HACS de la bêta dépend de sa publication
ultérieure.

### Notes de version 0.4.0-beta.2

- Panneau adapté aux thèmes Home Assistant et à l’affichage mobile. Il affiche
  les téléphones associés, leur libellé personnalisé et la dernière réception
  du calendrier lorsque celle-ci est disponible.
- Parcours d’association guidé jusqu’à la confirmation Android. En HTTP, le
  panneau utilise une origine HTTPS validée par Core ou indique où configurer
  l’accès sécurisé.
- Renommage et révocation coordonnés. Le panneau distingue les résultats
  confirmés de ceux qui nécessitent une actualisation. La source du calendrier
  reste configurable entre webhook local et téléphone HTTPS.

## Étape 3C4C.6.2 — Résultat indéterminé après une écriture

Sur `86b88d7`, deux sondes ont fait réussir l’écriture sur disque puis échouer
sa relecture de contrôle. La source et le libellé écrits ont été retrouvés
après restauration. Les commandes avaient pourtant renvoyé
`calendar_source_unavailable` et `rename_unavailable`. Chrome affichait
alors un refus et conservait les anciennes valeurs.

- `storage_outcome_unknown` signale désormais une écriture dont le résultat
  persistant est invérifiable. Les refus établis gardent leurs codes. Le
  changement de source confirmé retourne `selected` et le renommage confirmé
  `renamed`. La révocation conserve son traitement de `registry_unavailable`.
- Le panneau masque les données potentiellement périmées et propose
  **Réessayer**. Cette action appelle seulement `info`. Un libellé
  temporairement illisible est signalé comme indisponible jusqu’à une lecture
  réussie. Pour une source incertaine, le récepteur bloque les écritures et
  l’entrée doit être rechargée ou Core redémarré. Une actualisation affiche
  ensuite la source restaurée depuis le Store.
- Les sondes permanentes vérifient la réponse WebSocket, la persistance et
  l’absence d’écriture lors de la relecture. Six scénarios Chrome avec API
  simulée couvrent le succès, le refus, l’échec d’actualisation, la perte de
  réponse, la déconnexion et le résultat indéterminé sur stockage, y compris
  la cohérence du message de révocation.

Sous Windows, les 42 tests Python ciblés sur l’association et les 30 tests du
récepteur ont réussi avec Python 3.13. Les six scénarios Playwright et le
contrôle de syntaxe JavaScript ont réussi. La recette Core réelle de 3C4C.6.1
reste la référence pour les parcours ordinaires. Le lot est remis pour
contre-vérification des deux sondes ciblées.

## Étape 3C4C.6.3 — Unicité de la source signée

Sur `a48f2d4`, l’écriture de l’affectation à A réussissait puis sa relecture
échouait. Le sélecteur consultait la source précédente en mémoire. B
acceptait le même téléphone. Après rechargement de A, l’envoi signé recevait
`403 source_not_authorized`.

La sélection et le routage utilisent désormais le même décompte des
affectations, sous la coordination existante du registre. Un récepteur
chargé expose sa source confirmée ou les deux sources possibles si l’écriture
reste indéterminée. Pour une entrée configurée et déchargée, la décision
repose sur le Store. Un envoi signé exige une seule entrée revendiquant le
téléphone, chargée et dont l’affectation est confirmée.

| Transition indéterminée | Téléphones réservés pendant l’incertitude |
| --- | --- |
| Webhook → téléphone | Nouveau téléphone |
| Téléphone → webhook | Ancien téléphone |
| Téléphone → autre téléphone | Ancien et nouveau téléphones |

Un refus avant tentative d’écriture ne crée aucune réservation. Après
rechargement ou redémarrage, la lecture du Store retient uniquement la
source effectivement persistée et libère l’autre possibilité. Le chargement
d’une entrée dont le téléphone a été révoqué garde le refus et le nettoyage
de la projection prévus.
Le changement de source efface uniquement la projection de l’entrée ciblée.
L’actualisation du panneau est une lecture.

Un conflit déjà présent, même si une des entrées est déchargée, bloque
l’envoi signé. Le panneau explique le conflit et permet de changer
explicitement la source d’une des entrées. Les projections restent lisibles
jusqu’à cette décision. Si le Store d’une entrée déchargée est illisible,
l’unicité des affectations signées ne peut pas être établie. Les écritures et
les nouvelles affectations signées sont suspendues jusqu’à une lecture
exploitable. Les webhooks indépendants restent disponibles. Avec une
incertitude bornée aux sources d’un récepteur chargé, les autres téléphones
restent utilisables.

### Vérification ciblée

- La sonde historique passe avec le correctif. La matrice sur Stores disque
  couvre les trois transitions avec ancienne ou nouvelle valeur persistée,
  le refus préalable, l’écriture qui lève une erreur, la relecture qui
  échoue, le rechargement, la reconstruction du runtime, la concurrence,
  le conflit déjà présent et la révocation. Elle vérifie aussi le webhook
  et la livraison signée indépendants et compare les fichiers avant et
  après lecture.
- `python tests/qualify_native_transport.py --signed --assignment-reload`
  a réussi en HTTPS sur Core 2026.11.2 isolé, sous Windows avec Python
  3.14. Le serveur refuse l’affectation à B avant et pendant le déchargement
  de A. Le rechargement Core restaure A, dont l’envoi signé est accepté. Le
  webhook historique et le refus après révocation ont également été vérifiés.
- Le scénario Playwright supplémentaire vérifie le message de conflit,
  l’indisponibilité des téléphones réservés et l’accès au webhook pour
  résoudre le conflit.

Résultats sous Windows : 46 tests Python d’association et 30 tests de
récepteur réussis avec Python 3.13. Sept scénarios Chrome avec Playwright
1.65.2 et le contrôle de syntaxe JavaScript ont réussi.

## Étape 3C4C.6.4 — Lecture sûre du stockage et récupération

La sonde sur `ada94b1` reproduit la corruption avec le Store réel. Core
déplace le JSON de A en quarantaine et renvoie `None`. B accepte alors le
téléphone. La restauration de A crée un conflit et l’envoi signé reçoit
`403 source_not_authorized`.

Le lecteur commun vérifie l’absence sur disque avant de lui donner un sens.
Il est utilisé par la restauration des récepteurs, le décompte des
affectations des entrées déchargées et les lectures initiales du registre,
de sa garde et de l’état anti-rejeu. Le panneau signale l’indisponibilité et
indique la restauration du stockage. La règle d’unicité de 3C4C.6.3 reste
applicable.

| Observation | Traitement |
| --- | --- |
| Valeur valide | Elle fait autorité. Une quarantaine historique peut rester à côté du fichier restauré. |
| `None`, fichier principal absent et aucune trace de quarantaine | Absence confirmée. Une nouvelle entrée peut commencer avec le webhook. |
| `None` avec un fichier voisin `.corrupt.*` | Quarantaine détectée à chaque lecture, y compris après redémarrage. |
| `None` avec fichier principal présent | Résultat indéterminé. Cela couvre notamment un cache négatif de Core ou une lecture en mode `load_empty`. |
| Erreur d’accès ou version majeure non lisible | Stockage indisponible. |
| Valeur ou format d’affectation invalide | Lecture refusée. |

Les Stores Horéveil concernés n’activent pas `load_empty` et utilisent le
format 1.1. Le point d’extension de migration de Core refuse les versions
inconnues. Il évite la réécriture par défaut d’une version mineure dont
Horéveil ne connaît pas le format. L’examen de Core 2026.10.0 et 2026.11.2
confirme la convention de quarantaine et les branches de lecture utilisées.
[Store 2026.10.0](https://github.com/home-assistant/core/blob/2026.10.0/homeassistant/helpers/storage.py#L311-L427)
· [Store 2026.11.2](https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/helpers/storage.py#L406-L515)

Une entrée dont l’affectation est illisible ne peut pas être chargée comme
webhook vide. Les opérations signées susceptibles de la concerner restent
bloquées, tandis que les webhooks des entrées indépendantes fonctionnent.
Pour récupérer, conserver le fichier de quarantaine, restaurer une sauvegarde
valide de l’entrée avec Core arrêté, puis redémarrer et actualiser le panneau.
Si la restauration révèle deux affectations valides au même téléphone, les
envois signés restent bloqués jusqu’au choix explicite d’une autre source
sur l’une des entrées. La lecture seule ne modifie ni le Store valide ni la
projection.

La sonde permanente se lance avec `python tests/real_storage_recovery.py`
dans l’environnement Core 2026.11.2. L’option `--baseline` rejoue le défaut
sur le code archivé de `ada94b1`. Sur Windows, la recette fournit uniquement
une date compatible avec les noms de fichiers locaux. Le décodage JSON,
le renommage en quarantaine et le retour `None` sont ceux du Store réel.
Sans cet ajustement sur Windows, le nom de quarantaine est refusé par le
système de fichiers. Le lecteur classe alors le Store comme indisponible
et le fichier corrompu reste en place.

Sur Core 2026.11.2, la sonde corrigée a vérifié le refus de B après la
quarantaine, après lectures répétées, après rechargement et après l’arrêt
puis le redémarrage de Core sur le même profil. Le webhook indépendant a
accepté un instantané. La restauration valide a rendu les envois à A,
conservé la quarantaine et confirmé que la relecture ne réécrit pas le
Store. Un conflit restauré a été refusé puis résolu par le choix explicite
du webhook sur B. La sonde du Store réel a aussi vérifié l’absence
légitime, le refus d’une version mineure inconnue et le cas `load_empty`
avec fichier présent.

Sous Windows, 47 tests Python d’association et 31 tests de réception ou
de lecture ont réussi avec Python 3.13. Les sept scénarios Chrome avec
Playwright et le contrôle de syntaxe JavaScript ont réussi. Les sondes
Core ont utilisé Python 3.14. Le lot est remis pour contre-vérification ciblée.

## Étape 3C4C.7.B — Métadonnées des téléphones et panneau

La base `46ab5b2` correspond à la préversion publiée `0.4.0-beta.2`.
L’utilisateur a validé les sept essais réels de 3C4C.6. Le lot 3C4C.7.B
est livré localement sur `feature/3c4c7-phone-metadata`. Android est traité
dans une étape suivante.

Le [schéma v1](CALENDAR_SNAPSHOT_SCHEMA_V1.md#métadonnées-du-téléphone)
définit les quatre champs facultatifs. Un calendrier signé autorisé alimente
la fiche après acceptation et écriture du calendrier. Une répétition
autorisée avec le même corps JSON octet pour octet peut compléter une
écriture de fiche manquée, avec l’accusé `duplicate` habituel. Un doublon
équivalent dont la sérialisation diffère garde le même accusé. Une révision
refusée, conflictuelle ou obsolète ne met pas la fiche à jour.

Les métadonnées et leur dernière révision sont dans le Store privé
`horeveil.phone_metadata`, indexées par `pairing_id`. Le registre,
les libellés et les projections utilisent leurs Stores existants. Le
verrou du registre ordonne les deux voies d’écriture de la fiche avec
la révocation. La tombstone est persistée avant le nettoyage de la fiche.
Le panneau ne publie que les informations des associations signées actives.
Si le nettoyage échoue, il est retenté au redémarrage. Un Store de
métadonnées indisponible laisse le calendrier valide conserver son accusé
habituel. Les erreurs sont journalisées sans valeurs de la fiche.

### Vérifier l’association sans calendrier

1. Pendant l’action manuelle Android **Vérifier l’association**, effectuer
   le `POST /api/horeveil/direct/v1/status` signé habituel avec `{}`.
   Le corps de la réponse historique reste identique. Un serveur compatible
   ajoute l’en-tête `Horeveil-Phone-Metadata: 1` sur une réponse `200`
   pour une association active.
2. Après avoir vérifié la réponse, l’identité et le serveur de l’association,
   et uniquement si cet en-tête vaut `1`, Android peut envoyer au même
   endpoint un second POST signé, avec un nonce inédit. Il conserve la
   méthode, l’origine, le tag `horeveil-status-v1`, les composants de
   signature et le profil historiques. Les deux seules clés autorisées
   à la racine de ce nouveau corps sont `calendar_revision` et
   `phone_metadata`. La limite existante du corps du status reste de 4 Kio.

   ```json
   {
     "calendar_revision": 42,
     "phone_metadata": {
       "manufacturer": "Samsung",
       "model": "SM-S936B",
       "app_version": "2.7.4",
       "android_version": "17"
     }
   }
   ```

3. La réponse `200` conserve le corps standard du status actif.
   `Horeveil-Phone-Metadata-Result` indique `stored` si les valeurs valides
   sont persistées ou déjà présentes pour cette révision, `ignored` si
   la révision est trop ancienne ou qu’aucun champ n’est exploitable, et
   `unavailable` si le Store ne confirme pas l’opération.
   Une révision invalide ou des clés racine inattendues valent `400`.
   Une association absente, en attente ou révoquée ne peut effectuer
   cette écriture. Les vérifications de signature et de nonce habituelles
   s’appliquent. Cette opération n’accède ni aux récepteurs ni aux
   fonctions d’affectation de source.

`calendar_revision` est un entier JSON de 0 à 2⁶³−1 inclus. Android doit
lire **la plus haute révision déjà allouée** pour la destination calendrier
associée à ce `pairing_id`, y compris une révision chiffrée encore en
attente de livraison. Il envoie 0 uniquement si aucune révision n’a été
allouée pour cette association. L’implémentation Android pourra lire
`calendarRevision` dans la destination Home Assistant du `WebhookStore`.
La vérification manuelle conserve la sérialisation des actions de
l’association. Elle ne met pas le status enrichi en file et ne crée
aucune tâche périodique.

Home Assistant compare cette valeur à la dernière révision qui a alimenté
la fiche. Un calendrier doit avoir une révision supérieure. Une vérification
manuelle peut enrichir la fiche à révision égale. Si plusieurs vérifications
manuelles utilisent la même révision, la dernière traitée l’emporte pour
les seuls champs valides qu’elle contient. Sans champ valide, le seuil
n’avance pas. Ainsi un instantané déjà alloué au moment d’une vérification
manuelle réussie peut encore alimenter le calendrier si ses règles métier
l’autorisent, sans restaurer ses anciennes métadonnées. Les deux voies
suivent la même révision par association, indépendamment de l’horloge.

L’Android 2.7.4 continue d’envoyer des calendriers et des status historiques.
Un Android enrichi ne tente le second POST que si le serveur annonce la
capacité. La bêta 2 accepte déjà les instantanés v1 enrichis, en conserve le
JSON et ignore cette extension pour la projection. Le panneau présente le
libellé, les champs disponibles et la dernière réception du calendrier dans
le fuseau configuré de Home Assistant. La date renseigne sur la réception
du calendrier, indépendamment de l’état d’association.

### Vérifications du 27 novembre 2026

- `python -m unittest discover -s tests -p "test_*.py"` sous Python 3.13.
  Les 81 tests réussissent. Les scénarios signés testent les deux voies,
  les reprises, la compatibilité du status historique, la séparation de
  la projection et la révocation. Le scénario HTTPS et WebSocket couvre
  le libellé, une écriture concurrente à la révocation, un nettoyage
  défaillant et sa reprise après reconstruction du runtime.
- `python tests/panel_mutation_browser.py --channel chrome`. Les 9
  scénarios réussissent, dont le contraste sous accent blanc, le focus
  clavier, l’actualisation et ses erreurs, ainsi que le fuseau de
  l’instance quand le navigateur utilise un autre fuseau. L’option
  `--screenshots` génère deux captures sur données de test.
- `node --check custom_components/horeveil/pairing_panel.js` et
  `git diff --check` réussissent.

Les résultats couvrent des doubles Home Assistant, du trafic de test signé
et un navigateur local. La recette Core réel et Android du lot reste à
faire. La contre-relecture indépendante ciblée porte sur la possibilité
d’écrire exclusivement sa propre fiche après authentification, l’ordre
avec la révocation et l’absence de droits supplémentaires.

## Étape 3C4C.7.D — Contre-vérification indépendante du 27 novembre 2026

**Verdict.** Validé sous réserve. La préparation locale de la bêta 3 peut
continuer. La publication est à différer jusqu’à correction et
contre-essai du défaut ci-dessous.

La contre-vérification porte sur Home Assistant `2dd8c32` et Android
`6be3672`. Le HEAD Android `603f3e0` ajoute uniquement du suivi. L’APK
Debug 2.7.4 build 2 contient `6be3672776e0` dans son DEX et son SHA-256
correspond au journal Android.

Le contrat des quatre champs facultatifs et leurs limites UTF-8 concordent
sur les cas vérifiés. L’enrichissement est réservé au HTTPS signé direct.
Il précède le chiffrement de la file et la signature du corps. Les octets
déjà en file, l’empreinte métier, la périodicité et les règles de projection
sont conservés. Le status enrichi est conditionné à une liaison active et
à l’en-tête de capacité. Il ne peut écrire que la fiche du téléphone
authentifié et n’accède ni au calendrier ni à l’affectation des sources.

Deux sondes indépendantes ont couvert l’écriture indisponible, la reprise
sur un doublon exact, les status manuels à révision égale, les deux ordres
d’arrivée entre calendrier et status, la révocation et le redémarrage. Un
ancien instantané peut alimenter le calendrier sans écraser une fiche déjà
actualisée. Un nouveau status à la révision courante reste admissible. Les
56 tests Python ciblés et les neuf scénarios Chrome ont réussi. Les preuves
Android réutilisées couvrent 50 tests JVM, cinq scénarios instrumentés et
le Lint à zéro erreur. Les quatre combinaisons signées du lot précédent
utilisaient des fixtures.

Sur l’émulateur API 37, deux profils distincts de Core 2026.11.2 ont
exercé les échanges réels avec `46ab5b2` et `2dd8c32`. La référence
antérieure disponible `a1d244e97415` a établi l’association et envoyé le
status historique. L’installation par-dessus de `6be3672776e0` a
conservé l’association et sa clé. Le nouvel APK a effectué un status sans
source calendrier en un appel sur la bêta 2 et en deux sur `2dd8c32`.
Une défaillance injectée du Store a laissé la liaison vérifiée tout en
signalant l’échec facultatif. Chaque serveur a accepté un instantané
enrichi de 30 événements. Le panneau de Core a conservé le libellé
personnalisé et les événements lors du status suivant. Sur `2dd8c32`,
la fiche, le libellé et le calendrier étaient présents après redémarrage.
L’instrumentation a fourni une autorité de certification éphémère de
recette. L’exécution sur S25+, Android 8 et l’installation HACS de la
bêta 3 restent à qualifier.

**Défaut P2 reproduit sur émulateur et Core réel.** Avec un calendrier
déjà publié, faire échouer l’écriture de la marque d’oubli dans
`HomeAssistantPairing.forget()`, après la suppression de la destination
calendrier dans DataStore. L’association reste lisible avec le même
`pairing_id`, mais sa révision locale retombe à zéro. Dans deux nouveaux
processus Android, **Vérifier la liaison** confirme le lien. Core ignore
les deux mises à jour de révision zéro, la fiche conserve l’ancienne
version injectée pour la recette et Android n’affiche aucun avis sur
l’échec d’actualisation. Sans nouvelle révision calendrier, cette fiche
ne se rétablit pas par une nouvelle vérification de la liaison. La
reproduction injecte une erreur de stockage entre les deux écritures
Android. Le calendrier déjà reçu est toujours présent dans Core.

## Étape 3C4C.7.E — Correction du P2 le 28 novembre 2026

Android retire la destination, le consentement et le corps en attente
dans la même transaction DataStore qui conserve la plus haute révision
allouée pour ce `pairing_id`. L’écriture séparée de la marque d’oubli
peut alors échouer sans perdre le seuil. La répétition de l’oubli
conserve le seuil. Une réactivation explicite du partage pour le même
lien repart au-dessus de ce seuil, avec une nouvelle sélection des
alarmes. Une autre association a son propre compteur. La préférence
résiduelle contient seulement un identifiant d’association et une
révision. Elle est remplacée lors d’un oubli ultérieur qui conserve
un nouveau seuil.

La liaison active et le résultat de l’actualisation sont indépendants dans
Android. `stored` confirme l’enregistrement. `ignored` indique une
révision trop ancienne ou l’absence de champs valides côté serveur.
Une erreur facultative est signalée séparément. Le contrat signé
et les contrôles de révision Home Assistant du lot 3C4C.7.B s’appliquent
également à l’APK corrigé.

Sur Core réel 2026.11.2 et émulateur Android API 37, la sonde ciblée
`.venv/3c4c7e_p2.py` réutilise les profils du contre-audit. Avec l’APK
`6be3672776e0`, l’interruption produit deux status signés
`(0, ignored)` et conserve la fiche ancienne. Avec `9ee5ef74f8a6`,
la même injection produit deux status `(1, stored)` et restaure la
fiche. Un status volontairement ancien reste `ignored` et cet état
est distinct dans Android. Le calendrier et ses événements restent
présents après l’interruption, le partage local reste désactivé et
une vérification après révocation reçoit un refus. Trois tests Python
ciblés sur la révision, les reprises et la révocation réussissent.
Les résultats Android sont détaillés dans le suivi de son dépôt.

### Notes de version 0.4.0-beta.3

- La fiche des téléphones associés peut afficher le fabricant, le modèle
  et les versions d’Horéveil et d’Android. Elle est alimentée par le
  calendrier signé ou par la vérification manuelle de la liaison depuis
  un APK Android compatible. Le libellé défini dans Home Assistant reste
  prioritaire.
- Le panneau améliore le contraste du bouton d’invitation, place
  l’actualisation dans l’en-tête de la liste et affiche les réceptions
  en français dans le fuseau de l’instance.
- Le compagnon Android de recette corrige la perte du compteur après
  un oubli interrompu et distingue une actualisation enregistrée,
  ignorée ou échouée. L’ancienne génération de clients conserve les
  parcours signés habituels.

Préparation locale pour le contre-essai indépendant du P2. La version
stable 0.3.0 et le tag public 0.4.0-beta.2 restent les références
installables à ce stade.
