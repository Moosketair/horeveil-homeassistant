# Relayer le calendrier Horéveil avec Node-RED

Utilisez l’entrée HTTPS de recette autorisée pour le téléphone et une URL Home Assistant interne pour le second trajet. Le montage ci-dessous est à reproduire manuellement dans Node-RED.

1. Dans le service Node-RED, configurez `HOREVEIL_HA_WEBHOOK_URL` avec l’URL locale complète de Home Assistant suivie du chemin affiché par Horéveil. Renseignez `HOREVEIL_HA_RELAY_TOKEN` avec le jeton affiché dans la même boîte. Stockez ces valeurs dans l’environnement du service, hors du flux exporté. Voir la [documentation Node-RED](https://nodered.org/docs/user-guide/environment-variables/).
2. Sur l’entrée POST de recette, conservez le corps brut en `msg.payload`. Si le nœud **HTTP In** dispose de l’option **Skip body parsing**, activez-la sur cette route. Une valeur `Buffer` préserve exactement le JSON et la précision des entiers 64 bits. Si votre entrée distribue plusieurs événements, utilisez la valeur de `event` pour sélectionner `calendar_snapshot` tout en transmettant le corps d’origine. Voir le [nœud HTTP In](https://github.com/node-red/node-red/blob/master/packages/node_modules/%40node-red/nodes/core/network/21-httpin.html).
3. Ajoutez un nœud **Function** à deux sorties. La première va vers **HTTP Request**, configuré en POST, avec l’URL renseignée par `msg.url` (champ URL vide) et une réponse de type texte. La seconde va directement vers **HTTP Response** pour les erreurs locales. Utilisez le code suivant.

```javascript
const url = env.get("HOREVEIL_HA_WEBHOOK_URL");
const token = env.get("HOREVEIL_HA_RELAY_TOKEN");
if (!url || !token || !(Buffer.isBuffer(msg.payload) || typeof msg.payload === "string")) {
    msg.statusCode = 503;
    msg.headers = {};
    msg.payload = "";
    return [null, msg];
}
msg.url = url;
msg.followRedirects = false;
msg.headers = {
    "Content-Type": "application/json",
    "X-Horeveil-Token": token
};
return [msg, null];
```

4. Après **HTTP Request**, ajoutez un nœud **Function** relié à **HTTP Response** pour renvoyer une réponse fiable au téléphone.

```javascript
let result;
try {
    result = JSON.parse(msg.payload)?.result;
} catch {
    result = undefined;
}
const accepted = msg.statusCode === 200 && ["accepted", "duplicate"].includes(result);
const rejected = [400, 403, 408, 409, 413, 415, 503].includes(msg.statusCode);
msg.statusCode = accepted ? 204 : (rejected ? msg.statusCode : 502);
delete msg.url;
msg.headers = {};
msg.payload = "";
return msg;
```

5. Reliez aussi un nœud **Catch**, limité au nœud **HTTP Request**, à un **HTTP Response** via une fonction qui efface l’URL, les en-têtes et le corps puis répond `503`. Si disponible, activez l’envoi des erreurs réseau vers **Catch** dans le nœud de requête. Gardez les nœuds **Debug** désactivés sur ce trajet et évitez de journaliser le message complet, le jeton ou l’URL.
