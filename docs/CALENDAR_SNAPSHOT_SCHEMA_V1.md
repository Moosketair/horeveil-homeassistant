# Horéveil — Schéma JSON `calendar_snapshot` v1

Chaque instantané représente toutes les occurrences V2 de sa fenêtre dans le
périmètre fourni par l’appelant. Une liste vide (`"occurrences": []`) représente
un calendrier vide. Le sérialiseur utilise la projection métier existante comme
entrée. Les horaires indiquent des prévisions de sonnerie.

L’objet facultatif `alarms` est une extension additive de la v1. Les champs
existants, les identifiants et les bornes gardent leur sens. Un lecteur v1 peut
ignorer ce nouvel objet. Un lecteur strict doit l’accepter comme champ facultatif
pour traiter à la fois les anciens et les nouveaux instantanés.

## Enveloppe

| Champ | Type | Signification |
| --- | --- | --- |
| `schema_version` | entier | `1`. |
| `event` | chaîne | `calendar_snapshot`. |
| `installation_id` | UUID | Identité stable fournie par la future livraison. |
| `revision` | entier positif sur 64 bits | Révision fournie par la future synchronisation. |
| `generated_at` | chaîne ISO 8601 UTC | Instant de génération. |
| `window_start` | chaîne ISO 8601 UTC | Borne incluse. |
| `window_end_exclusive` | chaîne ISO 8601 UTC | Même heure locale 30, 60 ou 90 jours plus tard dans `timezone`, selon les règles de `ZonedDateTime`. Borne exclue. |
| `timezone` | chaîne | Identifiant `ZoneId` Java de la projection, commun à toutes les lignes. |
| `expires_at` | chaîne ISO 8601 UTC | Premier instant où l’état est périmé. Strictement après `generated_at` et au plus tard à `window_end_exclusive`. |
| `alarms` | objet facultatif | Catalogue de la destination, indexé par UUID d’alarme. Les nouveaux envois le fournissent, avec `{}` si aucune alarme n’est autorisée. |
| `occurrences` | tableau | Liste complète des occurrences, éventuellement vide. |

## Catalogue des alarmes

Le consentement calendrier et les autorisations individuelles de la destination
couvrent ensemble les occurrences et leurs métadonnées. Chaque alarme autorisée
encore présente dans le catalogue V2 figure une seule fois, même si elle n’a
aucun créneau dans la fenêtre. Les alarmes supprimées et celles dont le partage
a été retiré sont absentes. Les libellés sont relus à chaque nouvelle demande.

| Champ | Type | Signification |
| --- | --- | --- |
| `name` | chaîne | Nom enregistré de l’alarme. Le lecteur peut afficher un libellé générique si le nom est vide. |
| `category` | chaîne facultative | Nom de la catégorie à laquelle l’alarme est rattachée. Champ omis sans catégorie. |
| `mode` | chaîne facultative | Nom du mode V2 auquel l’alarme est rattachée dans cette catégorie. Il décrit le rattachement au moment de la génération, indépendamment de la sélection du mode et de son état futur. |

## Occurrence

| Champ | Type | Signification |
| --- | --- | --- |
| `occurrence_id` | chaîne | Clé `UUID d’alarme/date nominale`, unique dans l’instantané et stable après un report. L’identité entre installations inclut `installation_id`. |
| `alarm_id` | UUID | Identifiant de l’alarme V2. Clé de jointure vers `alarms` lorsque ce catalogue est présent. |
| `nominal_date` | chaîne `AAAA-MM-JJ` | Date du créneau d’origine. Elle peut être hors fenêtre après un report. |
| `sound_start` | chaîne ISO 8601 UTC | Début sonore prévisionnel. Seul cet instant détermine l’appartenance à la fenêtre. |
| `target_at` | chaîne ISO 8601 UTC, facultative | Heure cible lorsqu’elle diffère du début sonore, par exemple avec un crescendo anticipé. En l’absence du champ, la cible vaut `sound_start`. |

Le JSON UTF-8 entier est limité à **1 048 576 octets**. Au-delà, `encode()`
signale `CalendarSnapshotTooLarge` et ne retourne aucun corps partiel. Le
sérialiseur rejette aussi les doublons et les incohérences de borne, de fuseau ou
d’identité, ainsi qu’une référence manquante dans un catalogue fourni. Le
catalogue et les occurrences comptent ensemble dans la limite.

## Exemple

La fenêtre de 30 jours traverse le passage à l’heure d’été à Paris. La seule
occurrence est un report du 27 au 28 mars, avec cinq minutes de crescendo.

```json
{
  "schema_version": 1,
  "event": "calendar_snapshot",
  "installation_id": "00000000-0000-0000-0000-000000000001",
  "revision": 7,
  "generated_at": "2027-03-27T08:00:00Z",
  "window_start": "2027-03-27T08:00:00Z",
  "window_end_exclusive": "2027-04-26T07:00:00Z",
  "timezone": "Europe/Paris",
  "expires_at": "2027-03-28T08:00:00Z",
  "alarms": {
    "00000000-0000-0000-0000-000000000002": {
      "name": "Réveil du matin",
      "category": "Travail",
      "mode": "Matin"
    }
  },
  "occurrences": [
    {
      "occurrence_id": "00000000-0000-0000-0000-000000000002/2027-03-27",
      "alarm_id": "00000000-0000-0000-0000-000000000002",
      "nominal_date": "2027-03-27",
      "sound_start": "2027-03-28T01:45:00Z",
      "target_at": "2027-03-28T01:50:00Z"
    }
  ]
}
```
