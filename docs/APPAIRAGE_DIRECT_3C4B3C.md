# Horéveil · recette d’intégration du panneau

Dans le profil Core de recette, créer un récepteur Horéveil de test pour faire apparaître son entrée dans **Paramètres → Appareils et services → Horéveil**. Depuis cette entrée, ouvrir le panneau de configuration Horéveil. Le lien natif porte le domaine de l’intégration. Le panneau reste également accessible dans la barre latérale des administrateurs. [Procédure mTLS du profil de test](APPAIRAGE_DIRECT_3C4B3A.md) · [Parcours du panneau](APPAIRAGE_DIRECT_3C4B3B.md)

## Recette répétable

Les commandes suivantes utilisent un environnement Python 3.14 séparé. Installer le frontend ciblé avec Core. Le mode navigateur utilise Chrome ou un Chromium installé par Playwright.

```powershell
py -3.14 -m venv .venv/core-recette
$testPython = ".venv/core-recette/Scripts/python.exe"
& $testPython -m pip install "homeassistant==2026.11.2" "home-assistant-frontend==20261028.6" "playwright==1.65.2"
& $testPython tests/real_pairing_smoke.py
& $testPython tests/real_pairing_smoke.py --browser --channel chrome
```

Le script démarre un vrai Core à partir d’un dossier temporaire, sur un port libre de `127.0.0.1`. Il génère les certificats et les comptes de test. Les appels passent par les véritables vues HTTP, WebSocket, comptes et stores Home Assistant. L’expiration est testée après contrôle de l’échéance de cinq minutes puis avance de l’horloge du registre. Cette recette reste séparée de la suite Python habituelle pour éviter d’installer Core sur chaque exécution.

Vérifier le refus sans certificat et avec un en-tête de relais, l’absence du panneau et le refus des commandes pour un compte non administrateur. Avec l’administrateur, corriger l’origine suggérée, annuler une invitation puis en laisser expirer une. Créer ensuite une liaison avec le simulateur, la confirmer après comparaison et la révoquer. Les deux ensembles de permissions doivent rester vides. Les clés, codes et jetons de test restent dans le profil temporaire ou en mémoire. Les routes et le panneau restent désactivés hors recette. Le QR Android reste indisponible.

## Résultat sur le poste de développement

Core `2026.11.2` et le frontend fourni `20261028.6` ont été exécutés sur un profil jetable. Core a publié le lien de configuration pour le domaine `horeveil` et l’a masqué à l’utilisateur non administrateur. Le parcours du panneau est passé dans Chrome avec la véritable session HA et le simulateur mTLS. Le lien depuis la fiche Intégrations n’a pas pu être cliqué sur ce poste Windows. L’appel natif `get_services` échoue faute de `pymicro_vad`, et l’installation des extensions audio a rencontré l’absence du compilateur C++ requis pour `pyspeex-noise`. Pour rejouer le clic, utiliser un environnement de test où les dépendances natives de Core sont disponibles.

Pour vérifier le reste sur Windows, le mode ci-dessous charge le module servi par Core dans le frontend authentifié. Il utilise le vrai `hass.callWS` sans passer par le routeur de la fiche.

```powershell
& $testPython tests/real_pairing_smoke.py --browser --channel chrome --component
```

Le processus Chrome jetable accepte le certificat de transport temporaire généré par Playwright. Les clients HTTP et WebSocket indépendants vérifient la CA de recette et présentent le certificat client à Core. Aucun certificat n’est ajouté au magasin du système.
