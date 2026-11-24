# Horéveil · association HTTPS via Nginx

L’accès direct est éteint par défaut et après chaque redémarrage. L’administrateur doit confirmer l’origine HTTPS dans le panneau pour ouvrir un listener séparé limité à `127.0.0.1`. Le panneau utilise la session administrateur HA et le certificat client administrateur épinglé. L’association du téléphone utilise le code valable cinq minutes, l’approbation et la confirmation déjà disponibles en recette. Le mode précédent reste disponible avec sa configuration. [Recette locale d’origine](APPAIRAGE_DIRECT_3C4B3A.md)

## Configuration opt-in

Le serveur HTTP HA doit déjà prendre en charge le mTLS natif pour l’administration locale. Le panneau s’ouvre depuis une session HA administrateur connectée directement à la boucle locale avec son certificat épinglé. L’activation est bloquée si HA ne vérifie pas les certificats clients ou si les empreintes administrateur et Nginx sont identiques. L’intégration ne change pas la configuration HTTP de HA. Vérifier que les clients HA existants, notamment Node-RED, sont compatibles avec les prérequis avant de changer une installation existante.

```yaml
horeveil:
  direct_pairing:
    port: 18443
    certificate: /chemin/prive/horeveil-upstream.pem
    key: /chemin/prive/horeveil-upstream.key
    client_ca: /chemin/prive/proxy-ca.pem
    admin_certificate_sha256: "<empreinte SHA-256 DER du certificat administrateur>"
    proxy_certificate_sha256: "<empreinte SHA-256 DER du certificat client Nginx>"
```

Les empreintes sont sur 64 caractères hexadécimaux minuscules. Le listener vérifie la chaîne client puis le certificat exact de Nginx. Utiliser une CA interne dédiée au trajet privé et un certificat serveur contenant le nom DNS `horeveil-gateway.example.invalid` dans le SAN, ou adapter le nom et le SAN ensemble. Les deux clés privées restent hors dépôt. Le panneau suggère l’URL Internet HA sans la sonder. Remplacer cette adresse par l’origine du vhost dédié si nécessaire, confirmer l’activation et créer l’invitation. Désactiver le relais ferme le listener et annule les invitations de cette origine. Les associations déjà validées restent dans le registre et peuvent être révoquées.

## Vhost et accès

- **Nginx.** Installer [le vhost dédié](nginx-horeveil-pairing.conf) dans le contexte `http`, avec le module Real IP disponible. Nginx et HA doivent partager la même boucle IPv4. Le vhost ne relaie que les trois chemins exacts d’association. Utiliser un certificat serveur reconnu par le téléphone. Le port HTTP de cette origine, s’il existe, doit refuser le trafic d’association. Ne mettre ni trace de corps, ni traces d’en-têtes, ni redirection sur ces routes. Le vhost et tous les autres accès publics à HA doivent bloquer l’exposition du webhook Horéveil historique. Vérifier cette règle sur tout proxy déjà en place, sans publier l’URL du webhook. [TLS amont et règles proxy Nginx](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
- **En-têtes.** Le vhost écarte les en-têtes du client et écrit sa propre indication HTTPS, son origine fixe et l’adresse du pair socket Nginx. Le listener ne considère ces indications qu’après vérification du certificat Nginx. Un `Host` ou `X-Forwarded-For` fourni par un client n’autorise aucun accès. Le relais applique des limites par source et globales. Un proxy en cascade, un port privé accessible depuis le réseau ou un vhost qui ne peut pas appliquer ces règles sortent du périmètre d’activation.
- **LAN et VPN.** Le même vhost peut être joignable uniquement sur le LAN ou le VPN, avec un nom et un certificat approuvés par le téléphone. Le listener privé reste sur la boucle locale. Aucun accès direct au port HA ou au listener privé n’est nécessaire au téléphone.
- **Home Assistant Cloud.** Compatibilité non vérifiée. Le tunnel Cloud HA ne fournit pas le trajet Nginx mTLS décrit ici. Garder ce mode fermé et conserver le webhook local. [Architecture du tunnel HA](https://contributors.home-assistant.io/architecture/platform/cloud/)

Android n’a besoin que de HTTPS standard face à Nginx, avec contrôles de chaîne et de nom. Le certificat client du mode de recette est techniquement possible via Android KeyChain, mais demanderait une clé par téléphone, une sélection par l’utilisateur et une gestion de son renouvellement. Il reste réservé à la recette et à l’administration locale. Le futur client doit refuser HTTP et les redirections et transmettre son secret uniquement à l’origine confirmée. Aucun client Android ni QR n’est livré ici. [Android KeyChain](https://developer.android.com/reference/android/security/KeyChain)

Le secret unique et l’origine sont vérifiés pour le statut de la liaison, y compris lors d’un réessai. La révocation est effective après persistance. Le contrôle de consultation interne exige les deux autorisations, initialement vides. Le relais ne sert encore ni calendrier, ni données d’alarme, ni télécommande.

## Recette isolée

Utiliser le Core et le frontend indiqués dans la [recette native](APPAIRAGE_DIRECT_3C4B3C.md). Fournir le binaire officiel Nginx au script. Le profil, les certificats de test, les comptes et les ports sont temporaires.

```powershell
& $testPython tests/real_pairing_smoke.py --nginx "C:/chemin/nginx.exe" --browser --channel chrome --component
```

Omettre `--component` sur un environnement capable d’ouvrir la fiche Intégrations, pour vérifier le clic natif. Sur ce poste Windows, le blocage des dépendances audio de `get_services` décrit dans la recette précédente reste applicable.

La recette a réussi avec Core 2026.11.2, Nginx 1.30.8 et Chrome. Elle couvre le refus avant opt-in, la vérification TLS, le filtrage des routes et des en-têtes, l’activation du relais depuis le panneau authentifié, l’association, les réessais et la révocation. Le test Nginx utilise les directives du vhost fourni et des ports de boucle locale.
