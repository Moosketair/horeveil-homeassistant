# Appairage Horéveil via Home Assistant et Nginx Proxy Manager

Android appelle les trois routes du protocole v1 sur un sous-domaine HTTPS dédié. NPM les transmet au serveur HTTP natif de Core. Le panneau utilise l’adresse HTTPS habituelle de Home Assistant et sa session administrateur. L’invitation dure cinq minutes et nécessite la comparaison du code, l’approbation dans HA et la confirmation sur le téléphone. Les permissions commencent toutes désactivées. Le mode de recette mTLS reste disponible séparément.

## Identifier le trajet Docker sur Debian

Depuis Debian, relever d’abord les noms réels des conteneurs avec `docker ps`. Les commandes suivantes lisent uniquement les paramètres réseau. Remplacer `homeassistant`, `npm` et `proxy-net` par les noms observés. Dans Dockge Enhanced, comparer ces résultats aux champs `network_mode`, `networks`, `ports` et `volumes` de chaque pile.

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
docker inspect --format '{{.Name}} mode={{.HostConfig.NetworkMode}} ports={{json .NetworkSettings.Ports}}{{range $n,$v := .NetworkSettings.Networks}}{{printf " %s=%s gateway=%s" $n $v.IPAddress $v.Gateway}}{{end}}' homeassistant npm
docker network inspect --format '{{.Name}} driver={{.Driver}} interne={{.Internal}} ipam={{json .IPAM.Config}}{{range .Containers}}{{printf " %s=%s" .Name .IPv4Address}}{{end}}' proxy-net
ip -br address
ss -lnt
```

- **HA en mode `host`**. Core écoute sur Debian. Configurer l’amont NPM avec une adresse privée de Debian accessible depuis NPM. La boucle `127.0.0.1` dans NPM appartient à son conteneur. Vérifier l’adresse effectivement vue par Core après routage ou NAT.
- **HA en mode bridge**. HA et NPM peuvent communiquer sur un réseau privé commun. Pour des piles Dockge séparées, déclarer le même réseau Docker externe dans les deux fichiers Compose lors de la mise en place, si ce réseau est effectivement adapté. N’exposer aucun port supplémentaire sur Internet. Relever l’adresse NPM stable sur ce trajet. Refaire les vérifications après recréation ou changement de réseau.

Un réseau Docker commun donne de la connectivité. Une liste de membres, le drapeau `internal` et l’absence de publication de port ne suffisent pas à démontrer l’isolement de bouts en bouts. Vérifier les interfaces, les autres membres, les règles du pare-feu et les traductions d’adresse. Si Core ne distingue plus NPM du reste des clients après NAT, garder le mode éteint. [Installation Home Assistant Container](https://www.home-assistant.io/installation/linux/#install-home-assistant-container) · [Réseaux bridge Docker](https://docs.docker.com/engine/network/drivers/bridge/)

## Préparer Home Assistant Container

1. Garder l’entrée Horéveil et le volume `/config` existants. Relever l’IP source exacte de NPM telle que Core la voit, après tout NAT éventuel. Elle doit être stable, distincte des autres conteneurs et couverte par les `trusted_proxies` de HA avec `use_x_forwarded_for` actif. Éviter d’élargir la confiance à tout `proxy-net`.
2. Préférer un amont HTTPS existant avec vérification de la CA et du nom de Core dans NPM. Si Core écoute seulement en HTTP, utiliser `upstream: isolated_http` et `isolation_verified: true` uniquement après avoir vérifié que le trajet emprunte un réseau sous votre contrôle, réservé à NPM et à l’amont, et que le pare-feu exclut les autres sources sur ce trajet. Un bridge partagé, même privé, ne suffit pas. Home Assistant vérifie le pair et le secret par requête. La déclaration d’isolement confirme une vérification réseau effectuée par l’administrateur, que l’intégration ne peut pas faire depuis Core. En cas de doute, laisser le mode désactivé.
3. Générer localement un secret aléatoire de 32 octets (`openssl rand -hex 32`). Calculer le SHA-256 des **64 caractères hexadécimaux ASCII sans retour à la ligne**. Mettre seulement l’empreinte dans `secrets.yaml`. Ajouter cette section sous la clé `horeveil` existante de `configuration.yaml`. Les valeurs ci-dessous sont des exemples à remplacer. Retirer les options de recette mTLS si elles existent avant de sélectionner ce mode.

```yaml
horeveil:
  native_pairing:
    origin: https://pairing.example.invalid
    proxy_ip: 172.19.0.5
    relay_secret_sha256: !secret horeveil_relay_sha256
    upstream: tls
```

## Configurer NPM

1. Créer un Proxy Host avec le certificat du sous-domaine dédié et le même amont HA privé déjà joignable. Désactiver **Force SSL**, **Cache Assets** et **Websockets Support** sur ce seul hôte. Le trafic HTTP d’appairage doit recevoir `404` sans redirection. Utiliser le champ **Advanced** de cet hôte et adapter le [modèle fourni](npm-horeveil-pairing.conf). Pour un amont HTTP isolé, retirer les quatre directives `proxy_ssl_*` du modèle et choisir `http` dans NPM. Pour HTTPS, conserver la vérification du certificat et fournir son nom et sa CA. Privilégier l’adresse IP privée de l’amont dans le champ de destination NPM.
2. Dans le volume utilisateur `/data/nginx/custom`, créer `horeveil-relay-secret.inc`, accessible uniquement au service et aux administrateurs, avec la directive ci-dessous. Remplacer la valeur par le secret, sans l’inscrire dans le champ Advanced. Protéger les sauvegardes du volume et éviter les dumps de configuration ou les traces des requêtes. Le modèle remplace les en-têtes d’origine et de relais au lieu de propager ceux du client.

```nginx
proxy_set_header X-Horeveil-Relay-Secret <secret hexadécimal généré localement>;
```

3. Sur tous les autres hôtes publics qui relaient HA, refuser `/api/horeveil/v1/pair/` et empêcher tout accès WAN au webhook historique. Si aucun webhook n’est requis sur l’entrée WAN habituelle, le bloc ci-dessous suffit sur cet hôte. Si d’autres webhooks publics sont nécessaires, garder leur configuration et isoler explicitement le webhook Horéveil avec une règle privée adaptée. Ne pas exposer le port Core sur Internet. Vérifier que Node-RED conserve son accès privé au webhook Horéveil avant toute activation.

```nginx
location ^~ /api/horeveil/v1/pair/ { return 404; }
location ^~ /api/webhook/ { access_log off; return 404; }
```

Dans NPM v2.17.1, le champ Advanced est injecté dans le bloc `server`. Le générateur supprime son `location /` automatique lorsqu’il trouve un `location / { … }` dans ce champ. Conserver la règle de refus du modèle et éviter les Custom Locations supplémentaires sur l’hôte dédié. Après enregistrement, contrôler la syntaxe avec `nginx -t` dans le conteneur, puis vérifier les réponses depuis le LAN et le WAN. [Générateur NPM v2.17.1](https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.17.1/backend/internal/nginx.js#L259-L265) · [Configuration personnalisée NPM](https://nginxproxymanager.com/advanced-config/) · [Proxys de confiance HA](https://www.home-assistant.io/integrations/http/#reverse-proxies)

## Activer, vérifier et révoquer

- Redémarrer Core et vérifier les refus sur le domaine habituel, le port direct, le domaine dédié avant activation et le port HTTP. Depuis l’extérieur, vérifier que le webhook ne reçoit rien, puis vérifier une réception Node-RED sur son chemin privé. Bloquer l’activation si un de ces contrôles échoue.
- Ouvrir le panneau Horéveil avec une session administrateur en HTTPS. Vérifier le domaine affiché avant d’activer l’accès et de générer le QR. Depuis les données mobiles, scanner le QR sur le Galaxy, comparer les codes, approuver dans HA puis confirmer sur Android. Révoquer et vérifier que les requêtes du téléphone sont refusées. Le panneau ne déduit pas la joignabilité à partir de l’URL.
- Après redémarrage, l’accès et les invitations en attente sont désactivés. Les associations persistent, y compris les révocations. Pour changer le secret de relais, couper l’accès depuis le panneau, remplacer le fichier NPM et son empreinte HA, valider et recharger Nginx, puis redémarrer Core. Refaire les contrôles d’accès avant de réactiver. Le secret individuel des téléphones et leurs associations restent valides.

La recette locale `tests/real_pairing_native.py --nginx <binaire> --browser --channel chrome --component` utilise Core et Nginx réels avec les règles Advanced dans un profil jetable. Le mode `--component` ouvre le panneau dans le frontend authentifié. La recette ne démarre pas le conteneur NPM et ne vérifie pas le pare-feu de l’installation. Une extension ultérieure pourra fournir un profil LAN/VPN avec HTTPS et des vues natives réservées à ce trajet. Aucune prise en charge Home Assistant Cloud n’est annoncée.

## Rejouer la validation NPM sur Linux

Sur une machine de test avec Docker Engine et Python 3.14, depuis un checkout avec l’historique Git, installer les dépendances dans un environnement virtuel dédié et lancer les deux recettes ci-dessous. GitHub Actions utilise les mêmes commandes.

```bash
python -m pip install 'homeassistant==2026.11.2' 'home-assistant-frontend==20261028.6'
python -u tests/real_pairing_npm.py
python -u tests/real_upgrade_030.py
```

La première recette démarre l’image officielle NPM 2.17.1 avec un réseau et des ports locaux temporaires. Elle applique le modèle via l’API d’administration NPM, vérifie `nginx -t` et teste l’appairage vers un Core jetable sur l’hôte, avec TLS vérifié sur l’amont. Ce trajet reproduit la frontière réseau d’un Core en mode `host`. La seconde crée un véritable profil avec Horéveil 0.3.0 puis vérifie son ouverture avec le code candidat, le webhook privé et les entités. Pour le QR, la recette navigateur peut être rejouée avec la sonde du parseur Android `0bd42b0` et un checkout Android local en lecture seule. Voir [la procédure de préparation de la sonde](APPAIRAGE_DIRECT_3C4B5.md).
