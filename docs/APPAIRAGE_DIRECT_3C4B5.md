# Horéveil · QR d’association Android

Le panneau administrateur propose l’origine Internet HTTPS configurée dans Home Assistant, puis affiche l’hôte et le port retenus. Pour créer une invitation scannable, confirmer une origine desservie par le relais HTTPS activé. Le panneau ne sonde pas cette adresse et ne garantit pas qu’elle soit joignable depuis le téléphone. Le mode de recette sur `127.0.0.1` garde la saisie manuelle pour son simulateur. [Configuration du relais HTTPS](APPAIRAGE_DIRECT_3C4B4.md)

Le QR contient exactement l’objet JSON UTF-8 attendu par `PairingInvitation.qr` dans `PairingProtocol.kt`, au commit Android `0bd42b0`. Il contient `v` entier égal à 1, `origin` HTTPS, `code` Crockford temporaire de 26 caractères et `expires_at` au format ISO 8601 avec décalage horaire. L’adresse et le code affichés pour la saisie manuelle sont les mêmes. La validité est de cinq minutes. Régénérer annule l’ancienne invitation avant d’en créer une nouvelle pour la même origine. Le panneau efface l’image à l’expiration, à l’annulation, à la réservation, à la désactivation du relais et à la fermeture du panneau. L’approbation administrateur reste une action distincte.

Le navigateur dessine le QR en mémoire après déchiffrement de l’invitation. Le module local reprend l’actif officiel ES6 avec son en-tête de licence, un export de module et des espaces de fin de ligne retirés. Le code temporaire ne part ni vers un service de QR externe, ni vers une nouvelle route HA. L’actif statique est servi par le même contrôle mTLS que le panneau et aucune donnée d’invitation ne se trouve dans cet actif. [Project Nayuki 1.8.0, MIT](https://github.com/nayuki/QR-Code-generator/releases/tag/v1.8.0)

## Recette

Installer `zxing-cpp==3.1.1` dans l’environnement de [recette Core isolée](APPAIRAGE_DIRECT_3C4B3C.md). Avec le JDK Android Studio et le cache Gradle local, préparer la sonde Kotlin puis lancer la recette Nginx. Le script ne lit le dépôt Android que via `git show` et compile la sonde dans le projet HA, sous `.venv`.

```powershell
& $testPython -m pip install "zxing-cpp==3.1.1"
& ./tests/prepare_android_qr_probe.ps1 -Java "C:/Program Files/Android/Android Studio/jbr/bin/java.exe"
& $testPython tests/real_pairing_smoke.py --browser --channel chrome --component --nginx "C:/chemin/nginx.exe"
```

La recette décode le canevas Chrome, compare son contenu exact avec la saisie manuelle, puis utilise le code décodé sur Nginx. La sonde lit le même PNG avec ZXing Java et exécute le parseur Kotlin de référence. Aucun scan avec la caméra physique du Galaxy S25+ n’est effectué par cette recette.
