# Horéveil · panneau d’association administrateur

Le panneau **Horéveil** est proposé dans la barre latérale du profil Core de recette. Ouvrir Home Assistant depuis l’hôte de test avec le certificat client épinglé et une session administrateur active. Le panneau et son module ne sont enregistrés que si le mode de test peut démarrer. Chaque commande WebSocket vérifie séparément l’administration HA et la connexion locale mTLS. Les opérations existantes pour le simulateur en ligne de commande restent disponibles. [Préparation du profil de recette](APPAIRAGE_DIRECT_3C4B3A.md)

## Parcours

1. **Ajouter un téléphone** propose l’URL Internet HTTPS configurée dans HA si elle constitue une origine valide. Corriger le champ et contrôler le domaine affiché. Aucune requête de vérification ne part vers cette URL. Pour la recette, choisir **Utiliser l’adresse de recette**, puis confirmer la création.
2. Le panneau affiche l’adresse, le code manuel, l’état et l’expiration de l’invitation de cinq minutes. Utiliser l’adresse et le code depuis le simulateur sur le même hôte. La réception d’une candidature remplace le code manuel par le code de comparaison.
3. Comparer le code affiché avec celui calculé par le simulateur. Cocher l’attestation et approuver, ou refuser la demande. Le simulateur doit ensuite confirmer l’activation. La liste des téléphones associés présente les identifiants d’association. **Révoquer** exige une confirmation et rend le secret de cette association inutilisable après persistance.
4. Les deux modes et leurs cases présentent les futurs choix. **Consultation seule** est sélectionné à l’ouverture et toutes les cases sont décochées. Les choix de cet aperçu ne sont pas envoyés au serveur et les deux ensembles de permissions restent vides.

Les routes sont accessibles uniquement sur la boucle locale de l’hôte de recette. Le panneau affiche donc l’indisponibilité du QR Android. Le futur QR devra encoder `{v, origin, code, expires_at}` uniquement lorsque les routes pourront être jointes et validées depuis Android. [Conception du QR](APPAIRAGE_DIRECT_3C4A.md)

## Choix d’interface et confidentialité

Le config flow natif convient à un formulaire ou à une succession d’étapes. Ce parcours demande aussi un état de candidature actualisé, un code temporaire et une liste d’associations révocables. Un panneau personnalisé natif HA héberge un seul composant Web local, sans framework, CDN ou compilation. Le frontend HA contient un composant `ha-qr-code` utilisé dans ses propres écrans. Sa présence dans ces écrans ne garantit pas son chargement dans un panneau personnalisé. La génération et le scan seront qualifiés avec le futur transport Android. [Formulaires HA](https://contributors.home-assistant.io/core/integrations/flow-steps/) · [API de panneaux](https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/components/panel_custom/__init__.py) · [Composant QR du frontend](https://github.com/home-assistant/frontend/blob/dev/src/components/ha-qr-code.ts)

Le navigateur crée une paire RSA-OAEP éphémère. La clé privée WebCrypto est non exportable. Seule la clé publique est envoyée avec `invite` et `status`. Les codes sont chiffrés avant de passer dans les trames Home Assistant, puis affichés en mémoire dans le panneau. Aucun code ne va dans l’URL, le stockage du navigateur ou une requête vers un tiers. Le code est effacé à l’expiration, à la fermeture du panneau ou au changement de session. Après un rechargement, le code ne peut pas être réaffiché. Une invitation encore en attente expire au terme de son délai.

Les commandes de gestion `info` et `revoke` utilisent le même espace `horeveil/pairing_test`. `info` retourne les origines et les seuls identifiants d’association actifs. `revoke` exige `pairing_id` et `confirmed: true`. La recette se limite à ces liaisons de test. Le webhook Node-RED et les entités existantes utilisent leur parcours 0.3.0.
