# Horéveil pour Home Assistant

Horéveil reçoit les instantanés `calendar_snapshot` v1 de l’application Android par synchronisation directe ou via un relais local comme Node-RED. Chaque entrée expose un calendrier en lecture seule et quatre capteurs alimentés par son dernier instantané valide.

Version de recette **0.4.0-beta.2** pour Home Assistant **2026.10 ou ultérieur**, préparée localement. La version stable **0.3.0** reste disponible sur `main`. [Notes de la préversion](docs/SYNCHRONISATION_DIRECTE_3C4C4.md#notes-de-version-040-beta2).

Ce dépôt est distribué sous [licence MIT](LICENSE).

<!-- ha-marketplace:exclude-start -->
## Installation

### Home Assistant 2026.11 et ultérieur · Marketplace

1. Ouvrez **Paramètres → Marketplace**, puis **Ajouter à partir d’un lien**.
2. Connectez votre compte GitHub si Home Assistant vous le demande, puis ajoutez [Moosketair/horeveil-homeassistant](https://github.com/Moosketair/horeveil-homeassistant). Sélectionnez **Intégration** si le type est demandé.
3. Ouvrez **Horéveil** et sélectionnez **Installer**. Le téléchargement utilise le dernier commit de `main`. Redémarrez si Home Assistant le demande.

Consultez la [documentation Marketplace](https://www.home-assistant.io/integrations/marketplace/#custom-repositories).

### HACS · Installer la préversion

1. Dans **HACS → ⋮ → Dépôts personnalisés**, ajoutez [Moosketair/horeveil-homeassistant](https://github.com/Moosketair/horeveil-homeassistant) avec le type **Intégration**.
2. Sauvegardez Home Assistant et relevez les identifiants des cinq entités Horéveil. Conservez l’entrée d’intégration existante.
3. Dans **Horéveil → ⋮ → Retélécharger** (ou **Télécharger** à la première installation), ouvrez **Besoin d’une autre version ?**, choisissez **0.4.0-beta.2** après sa publication et redémarrez Home Assistant. La **0.4.0-beta.1** reste disponible jusque-là.
4. Vérifiez le calendrier, les quatre capteurs et le webhook local avant de sélectionner un téléphone comme source directe. Le [guide de recette](docs/SYNCHRONISATION_DIRECTE_3C4C4.md) détaille la sauvegarde, le retour arrière et les premiers essais Android.

Consultez la [documentation HACS](https://hacs.netlify.app/docs/faq/custom_repositories/).

### Installation manuelle

Copiez `custom_components/horeveil` dans le répertoire de configuration de Home Assistant, de façon à obtenir `<config>/custom_components/horeveil/manifest.json`, puis redémarrez.
<!-- ha-marketplace:exclude-end -->

## Configuration

1. Dans **Paramètres → Appareils et services → Ajouter une intégration**, recherchez **Horéveil**.
2. Validez la création de l’entrée. Les identifiants affichés servent uniquement si vous utilisez le webhook local.
3. Ouvrez le panneau **Horéveil**, associez le téléphone via le QR ou la saisie manuelle et approuvez les codes des deux côtés. Dans **Synchronisation du calendrier**, autorisez ce téléphone comme source de l’entrée.
4. Sur Android, ouvrez **Réglages → Intégrations → Home Assistant**, activez le partage et sélectionnez les alarmes. La synchronisation utilise une adresse HTTPS existante de Home Assistant. Le [guide 3C4C.4](docs/SYNCHRONISATION_DIRECTE_3C4C4.md) décrit la mise à jour et la recette.

### Webhook local

L’entrée utilise le webhook tant que vous n’avez pas choisi un téléphone comme source directe. Pour conserver ce mode, utilisez les identifiants affichés pendant la configuration, stockez-les dans votre gestionnaire de secrets, puis configurez le [relais Node-RED](docs/NODE_RED.md). Le premier instantané accepté lie la projection à son installation Android. Les destinations webhook de l’application restent disponibles indépendamment de la source choisie dans Home Assistant.

Le webhook natif accepte les POST sur le réseau local. L’en-tête `X-Horeveil-Token` apporte une authentification indépendante du chemin secret. Gardez le trajet Node-RED → Home Assistant sur le LAN. Si un proxy public dessert Home Assistant, bloquez-y le chemin de ce récepteur et excluez ce chemin, les en-têtes et le corps des journaux.

Le Core peut journaliser des identifiants de webhook lors de certains rejets, voire le début d’un corps en mode debug. Pour écarter ces traces natives, ajoutez ce réglage à la section `logger` de `configuration.yaml` avant la mise en service. Il s’applique à tous les webhooks natifs. Voir le [code du webhook Home Assistant](https://raw.githubusercontent.com/home-assistant/core/2026.11.2/homeassistant/components/webhook/__init__.py) et la [configuration de Logger](https://www.home-assistant.io/integrations/logger/).

```yaml
logger:
  logs:
    homeassistant.components.webhook: critical
```

## Réception et stockage

Le récepteur applique le [contrat Android v1](docs/CALENDAR_SNAPSHOT_SCHEMA_V1.md), y compris sa limite de 1 Mio et le calcul de la fenêtre en jours locaux. Les extensions JSON sont conservées. Une révision identique est un doublon si son contenu JSON est égal, indépendamment de l’ordre des clés et des espaces. L’ordre des tableaux compte.

| HTTP | Résultat |
| --- | --- |
| 200 | `accepted` après écriture confirmée, ou `duplicate` sans nouvelle écriture. |
| 400 / 408 / 413 / 415 | JSON invalide, délai de lecture dépassé, corps trop grand ou type/encodage HTTP non pris en charge. |
| 403 | Jeton absent ou incorrect. |
| 409 | Révision ancienne, conflit à révision identique ou autre installation. |
| 503 | Récepteur ou stockage indisponible. |

Home Assistant peut répondre avec un 200 vide pour un chemin inconnu ou un appel distant bloqué. Le relais doit confirmer la présence du résultat attendu avant de répondre positivement à Android.

Le stockage natif `Store` garde le texte JSON d’origine et la date de réception séparément, avec écriture atomique. Les états internes sont `empty`, `fresh` et `expired`. Dès `expires_at`, le snapshot cesse d’être disponible comme donnée courante. Il reste sur disque pour conserver l’identité et la dernière révision, y compris après redémarrage. La suppression de l’entrée efface son snapshot. Un changement de source dans le panneau efface la projection actuelle et conserve l’entrée ainsi que ses entités.

## Calendrier Horéveil

Dans le panneau **Calendrier**, sélectionnez **Horéveil**. Chaque occurrence commence à `sound_start` et suit le fuseau d’affichage de Home Assistant, passage à l’heure d’été et retour à l’heure d’hiver compris. Le créneau dure une minute à l’écran. S’il traverse un recul d’horloge, il est raccourci pour se terminer avant le changement d’heure. La durée affichée est indépendante de la durée réelle de la sonnerie.

Le titre reprend le nom de l’alarme ou **Réveil Horéveil** si le nom ou le catalogue manque. La description indique la catégorie et le mode disponibles. Une révision acceptée remplace les événements précédents. Un report conserve l’identifiant de l’occurrence et déplace son créneau. Un snapshot frais sans occurrence donne un calendrier vide. À l’expiration, ou avant la première réception, le calendrier est indisponible et ne renvoie aucun événement.

### Capteurs de calendrier

| Capteur | Valeur |
| --- | --- |
| Prochain réveil | Horodatage UTC du premier `sound_start` strictement futur. Attributs `name`, `category`, `mode`, `occurrence_id` et `target_at` si transmis. État `unknown` quand le snapshot est frais et qu’il ne reste aucun réveil. |
| Nombre de réveils à venir | Nombre de débuts strictement futurs, y compris `0` pour un snapshot frais vide. |
| Dernière réception du calendrier | Horodatage UTC relevé par Home Assistant pour la dernière révision acceptée et persistée. Disponible après redémarrage et après expiration du snapshot. |
| Fraîcheur du calendrier | `fresh`, `expired` ou `empty`. `empty` indique qu’aucun snapshot n’a encore été accepté. |

Les deux premiers capteurs sont indisponibles si le snapshot manque ou a expiré. Ils évoluent à chaque révision acceptée, au passage d’un début de sonnerie prévu et à l’expiration. Le mode est le rattachement de l’alarme dans le catalogue Android. Le capteur de réception conserve sa valeur lors d’un doublon ou d’un échec de persistance.

Les [événements de sonnerie et de commandes réelles](docs/EVENEMENTS_TEMPS_REEL.md) font l’objet d’une proposition distincte.

### Recette sur Home Assistant

1. Installez manuellement la branche de recette par-dessus la version installée et redémarrez Home Assistant en conservant l’entrée Horéveil existante. Avec un snapshot encore frais, vérifiez que le calendrier est visible avant tout nouvel envoi.
2. Depuis le Galaxy S25+, partagez deux réveils de recette et synchronisez. Dans **Calendrier**, vérifiez leurs heures de début et ouvrez leurs détails pour contrôler le nom, la catégorie et le mode. Pour une alarme avec crescendo anticipé, comparez l’heure au début sonore.
3. Reportez une occurrence et retirez l’autre du partage. Après la réponse `accepted`, vérifiez que la vue ouverte affiche le report et que le créneau retiré a disparu. Retirez ensuite le dernier réveil partagé en gardant le partage du calendrier actif et vérifiez le calendrier vide.
4. Sur une entrée de recette, envoyez un snapshot synthétique avec une date `expires_at` proche et suspendez les envois. Après cette échéance, vérifiez que le calendrier est indisponible et que l’action `calendar.get_events` ne renvoie rien pour la période testée. Vérifiez le même état après redémarrage, puis la récupération à la réception d’une révision fraîche.
5. Avec deux réveils proches, vérifiez le prochain réveil et le compteur dans les entités Horéveil. Laissez passer le premier début sonore et vérifiez que les deux capteurs évoluent sans nouvel envoi. Après un redémarrage et après expiration, vérifiez que la dernière réception affiche toujours le même horodatage.

## Guides de recette

La [synchronisation directe 3C4C.4](docs/SYNCHRONISATION_DIRECTE_3C4C4.md) utilise le [profil d’appairage signé 3C4C.3](docs/APPAIRAGE_NATIF_3C4C3.md). Les guides 3C4B décrivent les bancs antérieurs.

## Tests Python ciblés

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Les tests partent de l’exemple Android copié dans le contrat. Ils utilisent de vraies requêtes HTTP via aiohttp sur la boucle locale et un double de test pour les API Home Assistant, avec des écritures sur disque pour la restauration. Le guide 3C4C.4 indique les commandes pour Core isolé et les vérifications réellement exécutées. Les validations HACS et hassfest sont configurées dans GitHub Actions.
