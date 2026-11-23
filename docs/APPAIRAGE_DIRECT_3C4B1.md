# Horéveil · fondations internes de l’appairage direct

Cette étape fournit un registre que le futur parcours d’appairage ouvrira à la demande. La version 0.3.0 ne charge pas ce registre au démarrage et aucune configuration utilisateur n’est nécessaire. Le récepteur, le calendrier, les capteurs et le contrat `calendar_snapshot` v1 conservent leur fonctionnement. [Conception 3C.4A](APPAIRAGE_DIRECT_3C4A.md)

## Invitations et associations

Une invitation est liée à un administrateur et à une origine HTTPS confirmée. Son code Crockford de 26 caractères représente 128 bits aléatoires. Le gestionnaire retourne le code à la création et ne garde que son empreinte en mémoire. L’expiration après cinq minutes s’appuie sur une horloge monotone. Il peut y avoir une invitation par administrateur et huit par instance HA. L’annulation, l’expiration et le redémarrage rendent le code inutilisable. Une activation consomme l’invitation sous verrou avant de persister l’association. En cas d’échec d’écriture, il faut créer une nouvelle invitation.

Le registre attribue un `server_id` stable et un `pairing_id` distinct à chaque liaison. Un seul appairage actif par `installation_id` est permis sur l’instance. Le registre conserve uniquement le SHA-256 du secret de 256 bits fourni par le téléphone et vérifie sa possession en temps constant. Les deux côtés démarrent avec des permissions de consultation et de contrôle vides. La révocation retire l’empreinte et les droits de la liaison et conserve son état révoqué. La liaison d’un autre téléphone continue de fonctionner.

## Redémarrage et restauration

Le registre privé `.storage/horeveil.pairings` est couplé à un marqueur de génération dans `.cache/horeveil.pairing_guard`. Les deux sont écrits avec le mécanisme atomique de HA. Le marqueur est avancé en premier. Une lecture après chaque écriture valide la persistance avant succès. Un registre et un marqueur concordants restaurent les liaisons après un redémarrage normal. Si le marqueur manque ou diffère, les liaisons chargées sont révoquées avant que le registre soit utilisable. L’identité serveur est préservée si son registre est présent. La perte du marqueur impose un nouvel appairage.

Les sauvegardes natives Core et Supervisor excluent `.cache/*`. Une restauration qui conserve le marqueur actuel détecte un registre plus ancien. Une restauration qui supprime le marqueur refuse aussi les anciennes liaisons. Une image disque externe qui restaure simultanément le registre et son ancien marqueur peut reproduire un état indiscernable d’un redémarrage. Pour ce cas, exclure le marqueur des images ou le supprimer avant le démarrage après restauration. Les tests simulent le remplacement du registre et la perte du marqueur. [Exclusions Core](https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/components/backup/const.py) · [Exclusions Supervisor](https://github.com/home-assistant/supervisor/blob/main/supervisor/homeassistant/module.py)

## Raccordements futurs

- Proposer l’URL Internet configurée dans HA, si disponible. L’administrateur peut la modifier ou choisir une origine locale. La détection ne crée aucune invitation. L’administrateur confirme l’origine, validée en HTTPS, avant la création. Le parcours futur prend en charge la réservation, la comparaison de code et la preuve de possession avant d’appeler l’activation interne.
- Prévoir une bascule explicite par `installation_id` depuis Node-RED. Vérifier l’association et le premier snapshot direct avant la bascule, puis conserver une seule entrée publiée et ses identifiants d’entités. Tant que la bascule n’est pas confirmée, le relais reste la source affichée.
- Lier les futures projections, files et événements privés au `pairing_id`. Après révocation persistée, retirer immédiatement cette projection de l’affichage et purger ses données actives avec une opération rejouable au démarrage. Le registre actuel ne contient aucune de ces données.
