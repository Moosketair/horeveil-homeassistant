# Événements d’alarme Horéveil · proposition pour la prochaine étape

Le calendrier reste une projection des heures prévues. Les événements ci-dessous décrivent des actions ou transitions observées par Android. Ils seront envoyés dans un flux distinct de `calendar_snapshot` v1. L’étape 3C.3A documente ce flux.

## Message proposé

| Champ | Règle |
| --- | --- |
| `schema_version`, `event` | Version `1`, famille `alarm_event`. |
| `installation_id` | Identité Android déjà utilisée par le calendrier. |
| `event_id` | UUID créé une seule fois et réutilisé lors de chaque réessai. |
| `occurrence_id` | Identité stable de l’occurrence, y compris après report. |
| `event_type` | `ringing`, `snoozed`, `dismissed`, `rescheduled` ou `skipped`. |
| `occurred_at` | Instant UTC où Android observe la transition. |
| `next_at` | Nouvelle échéance UTC, requise pour `snoozed` et `rescheduled`, omise pour les autres types. |
| `expires_at` | Date limite UTC de validité de l’état représenté par l’événement. |
| `stream_id`, `sequence` | Époque UUID du flux de la destination et compteur croissant attribué à la mise en file. |

`ringing` commence lorsque le moteur confirme le démarrage réel de la sonnerie. `snoozed` et `dismissed` constatent respectivement son report temporaire et son arrêt. `rescheduled` change l’échéance d’une occurrence prévue et `skipped` l’annule. Une mutation du planning et une transition de sonnerie gardent des traces distinctes. Home Assistant conserve le type observé, l’instant source `occurred_at`, l’instant local de réception et la limite de fraîcheur. Sans état frais, la sonnerie est inconnue. Une échéance de snooze atteinte ne déclenche aucune transition par elle-même.

## Livraison, ordre et reprise

- Activer le partage pour chaque destination avec un consentement distinct. À la révocation, annuler les envois en attente pour cette destination. Lier le flux au récepteur déjà authentifié et à son installation. Un changement d’époque nécessite une réassociation explicite et invalide l’ancienne époque.
- Attribuer `event_id` et `sequence` dans une file persistante par destination. Reprendre les messages dans l’ordre après redémarrage, avec des réessais bornés dans le temps et le même corps. Le moteur d’alarme ne dépend pas du réseau ni de l’accusé de réception.
- Persister atomiquement la séquence reçue, l’identifiant (avec une empreinte du corps) et l’état applicable, puis accuser réception. Un doublon identique renvoie le même succès sans réappliquer la transition. Un identifiant réutilisé avec un contenu différent est un conflit. Un événement ancien ou hors délai ne remplace jamais l’état récent.
- Conserver un curseur par flux et une fenêtre bornée de déduplication. Une séquence inférieure au curseur ne modifie aucun état, même après purge de son identifiant. En cas de trou, un événement plus récent et frais peut être appliqué seul. Les événements portent l’état résultant de leur occurrence et ne nécessitent pas de rejouer un événement manquant. La réception peut confirmer qu’un événement valide a été ignoré définitivement pour éviter les réessais inutiles.
- Borner `ringing` par une durée maximale cohérente avec la sonnerie Android. Home Assistant fixe sa date limite au plus tôt entre `expires_at` et la borne locale de sécurité calculée à la réception. Une horloge source trop en avance ne prolonge pas cette borne. Un `dismissed` ou un `snoozed` plus récent clôt immédiatement la sonnerie. Après redémarrage, évaluer l’expiration avant de restaurer tout état frais. L’absence d’arrêt livré ne peut donc pas maintenir une fausse sonnerie.

## Adaptations futures

- **Android** — Publier les transitions depuis le moteur d’alarme et les commandes effectives de report et de saut. Ajouter les options de consentement et la file durable. Réutiliser le transport HTTPS et l’infrastructure de réessai.
- **Node-RED** — Relayer le JSON brut selon la famille `event` vers le récepteur habilité. Renvoyer à Android le résultat de Home Assistant et garder corps, en-têtes et URL privées hors des journaux.
- **Home Assistant** — Ajouter un traitement dédié au flux, avec validation, stockage, déduplication et échéances. Conserver le calendrier v1 et les événements réels indépendants, même si un événement arrive avant le snapshot correspondant. Journaliser uniquement des codes de résultat et des compteurs.
