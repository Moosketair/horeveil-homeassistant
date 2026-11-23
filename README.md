# Horéveil pour Home Assistant

L’étape 3C.1 reçoit les instantanés `calendar_snapshot` v1 de l’application Android via un relais Node-RED local. Elle conserve le dernier instantané par récepteur et calcule sa fraîcheur. La création d’une entité calendrier est prévue à l’étape 3C.2.

Version de l’intégration **0.1.1**. Home Assistant **2026.10 ou ultérieur**. La première publication est distribuée via la branche `main` du dépôt personnalisé.

Ce dépôt est distribué sous [licence MIT](LICENSE).

<!-- ha-marketplace:exclude-start -->
## Installation

### Home Assistant 2026.11 et ultérieur · Marketplace

1. Ouvrez **Paramètres → Marketplace**, puis **Ajouter à partir d’un lien**.
2. Connectez votre compte GitHub si Home Assistant vous le demande, puis ajoutez [Moosketair/horeveil-homeassistant](https://github.com/Moosketair/horeveil-homeassistant). Sélectionnez **Intégration** si le type est demandé.
3. Ouvrez **Horéveil** et sélectionnez **Installer**. Le téléchargement utilise le dernier commit de `main`. Redémarrez si Home Assistant le demande.

Consultez la [documentation Marketplace](https://www.home-assistant.io/integrations/marketplace/#custom-repositories).

### Home Assistant 2026.10 · HACS

1. Dans **HACS → ⋮ → Dépôts personnalisés**, ajoutez [Moosketair/horeveil-homeassistant](https://github.com/Moosketair/horeveil-homeassistant) avec le type **Intégration**.
2. Ouvrez **Horéveil**, sélectionnez **Télécharger** et choisissez `main` si un choix est affiché. Redémarrez Home Assistant.

Consultez la [documentation HACS](https://hacs.netlify.app/docs/faq/custom_repositories/).

### Installation manuelle

Copiez `custom_components/horeveil` dans le répertoire de configuration de Home Assistant, de façon à obtenir `<config>/custom_components/horeveil/manifest.json`, puis redémarrez.
<!-- ha-marketplace:exclude-end -->

## Configuration

1. Dans **Paramètres → Appareils et services → Ajouter une intégration**, recherchez **Horéveil**.
2. Copiez le chemin du webhook et le jeton affichés dans la boîte de configuration, puis validez. Gardez ces deux valeurs dans votre gestionnaire de secrets. Chaque entrée est associée à la première installation Android dont le snapshot est accepté. Créez une entrée distincte par installation.
3. Configurez le [relais Node-RED](docs/NODE_RED.md). Dans Android, utilisez **Synchroniser maintenant** sur la destination de recette. Vérifiez côté relais que Home Assistant répond avec `{"result":"accepted"}` ou `{"result":"duplicate"}`.

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

Le stockage natif `Store` garde le texte JSON d’origine et la date de réception séparément, avec écriture atomique. Les états internes sont `empty`, `fresh` et `expired`. Dès `expires_at`, le snapshot cesse d’être disponible comme donnée courante. Il reste sur disque pour conserver l’identité et la dernière révision, y compris après redémarrage. La suppression de l’entrée efface son snapshot. Pour changer d’installation ou remplacer un secret perdu, supprimez l’entrée, créez-en une nouvelle, puis demandez une synchronisation Android.

## Tests Python ciblés

Résultat local sous Python 3.13.14 et aiohttp 3.14.5 — 17 tests réussis, 0 échec, 0 erreur.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Les tests partent de l’exemple Android copié dans le contrat. Ils utilisent de vraies requêtes HTTP via aiohttp sur la boucle locale et un double de test pour les API Home Assistant, avec des écritures sur disque pour la restauration. Les signatures et comportements utilisés ont été vérifiés dans le code source de Home Assistant Core 2026.11.2. La recette sur une instance Home Assistant et un relais Node-RED réels reste à faire. Voir la [documentation du config flow](https://contributors.home-assistant.io/core/integrations/config-flow/) et le [code de Store](https://raw.githubusercontent.com/home-assistant/core/2026.11.2/homeassistant/helpers/storage.py).
