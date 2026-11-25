import { qrcodegen } from "./pairing-qrcode.js";

const COMMAND = "horeveil/pairing_test/";

const template = `
  <style>
    :host { display:block; color:var(--primary-text-color); }
    main { max-width:720px; margin:auto; padding:24px 16px; }
    section { padding:20px; margin:16px 0; background:var(--card-background-color);
      border:1px solid var(--divider-color); border-radius:12px; }
    h1, h2 { margin:0 0 12px; } h2 { font-size:1.15rem; }
    p { line-height:1.5; } label { display:block; margin:10px 0; }
    input[type=url] { box-sizing:border-box; width:100%; padding:10px; font:inherit;
      color:inherit; background:var(--secondary-background-color);
      border:1px solid var(--divider-color); border-radius:6px; }
    button { font:inherit; padding:8px 12px; margin:4px 8px 4px 0; border:1px solid var(--divider-color);
      border-radius:6px; color:inherit; background:var(--secondary-background-color); cursor:pointer; }
    button:disabled { opacity:.55; cursor:default; }
    output, code, li { overflow-wrap:anywhere; }
    #code, #comparison { font-size:1.15rem; letter-spacing:.08em; }
    #qr { margin:16px 0; }
    #qr-canvas { display:block; width:min(100%, 320px); height:auto; image-rendering:pixelated; }
    fieldset { margin:12px 0; border:1px solid var(--divider-color); }
    .muted { color:var(--secondary-text-color); }
    [hidden] { display:none !important; }
  </style>
  <main>
    <h1>Association Horéveil</h1>
    <p id="notice" role="status">Chargement de l’association…</p>
    <div id="content" hidden>
      <section aria-labelledby="add-title">
        <h2 id="add-title">Ajouter un téléphone</h2>
        <form id="address">
          <label for="origin">Adresse HTTPS de Home Assistant</label>
          <input id="origin" type="url" required autocomplete="off" spellcheck="false" list="https-origins">
          <datalist id="https-origins"></datalist>
          <p>Domaine ou hôte <output id="host" for="origin">—</output></p>
          <p class="muted">L’adresse Internet configurée dans HA est une suggestion. Son accessibilité depuis le téléphone n’est pas vérifiée.</p>
          <button id="local" type="button">Utiliser l’adresse de recette</button>
          <button id="create" type="submit" disabled>Confirmer et créer l’invitation</button>
        </form>
        <p id="availability"></p>
        <div id="direct" hidden>
          <p id="direct-state"></p>
          <label id="direct-confirm-label"><input id="direct-confirm" type="checkbox">
            Je confirme l’origine et le relais HTTPS configuré.
          </label>
          <button id="direct-toggle" type="button" disabled>Activer l’accès direct</button>
        </div>
        <div id="invitation" hidden>
          <p>État <strong id="state"></strong></p>
          <p>Valable 5 minutes. Expiration <time id="expires"></time></p>
          <div id="manual">
            <p>Adresse <output id="invitation-origin"></output><br>Code manuel <code id="code"></code></p>
            <figure id="qr" hidden>
              <canvas id="qr-canvas" width="0" height="0" aria-label="QR temporaire d’association Horéveil"></canvas>
              <figcaption>Scannez depuis Horéveil Android. La joignabilité HTTPS depuis le téléphone reste à vérifier.</figcaption>
            </figure>
            <p id="qr-unavailable" class="muted">La recette sur la boucle locale utilise le code manuel avec un simulateur sur cet hôte.</p>
            <button id="regenerate" type="button">Régénérer l’invitation</button>
          </div>
          <div id="review" hidden>
            <p>Code de sécurité <code id="comparison"></code></p>
            <label><input id="compared" type="checkbox"> J’ai comparé le code avec celui affiché sur l’appareil.</label>
            <button id="approve" type="button" disabled>Approuver</button>
          </div>
          <button id="cancel" type="button">Annuler l’invitation</button>
        </div>
      </section>
      <section aria-labelledby="permissions-title">
        <h2 id="permissions-title">Autorisations prévues</h2>
        <p>Ces choix sont un aperçu. Ils ne sont pas enregistrés et n’accordent aucun droit pendant la recette.</p>
        <label><input type="radio" name="mode" value="read" checked> Consultation seule</label>
        <p class="muted">HA pourra lire uniquement les données explicitement autorisées.</p>
        <label><input id="control-mode" type="radio" name="mode" value="control"> Consultation et contrôle</label>
        <p class="muted">Chaque action autorisée pourra modifier les réglages ou occurrences sur le téléphone.</p>
        <fieldset>
          <legend>Données à partager</legend>
          <label><input type="checkbox"> Calendrier</label>
          <label><input type="checkbox"> Alarmes</label>
          <label><input type="checkbox"> Catégories</label>
          <label><input type="checkbox"> Modes</label>
          <label><input type="checkbox"> Événements réels</label>
        </fieldset>
        <fieldset id="commands" disabled>
          <legend>Actions à autoriser</legend>
          <label><input type="checkbox"> Décaler une occurrence</label>
          <label><input type="checkbox"> Sauter une occurrence</label>
          <label><input type="checkbox"> Changer le mode d’une catégorie</label>
          <label><input type="checkbox"> Activer ou désactiver une alarme</label>
        </fieldset>
      </section>
      <section aria-labelledby="phones-title">
        <h2 id="phones-title">Téléphones associés</h2>
        <button id="refresh" type="button">Actualiser la liste</button>
        <p id="empty">Aucun téléphone associé.</p>
        <ul id="phones"></ul>
      </section>
      <section id="calendar-sources" aria-labelledby="calendar-sources-title" hidden>
        <h2 id="calendar-sources-title">Synchronisation du calendrier</h2>
        <p>Choisissez la source autorisée pour chaque entrée. Le téléphone doit aussi autoriser le partage. Changer de source efface la projection affichée.</p>
        <div id="calendar-entries"></div>
      </section>
    </div>
  </main>`;

function parseOrigin(value) {
  if (!/^https:\/\/[^/?#\\\s]+\/?$/i.test(value)) return null;
  try {
    const url = new URL(value);
    return url.username || url.password ? null : url;
  } catch {
    return null;
  }
}

function drawInvitationQr(canvas, origin, code, expiresAt) {
  const data = JSON.stringify({ v: 1, origin, code, expires_at: expiresAt });
  const qr = qrcodegen.QrCode.encodeText(data, qrcodegen.QrCode.Ecc.MEDIUM);
  const scale = 8;
  const quietZone = 4;
  canvas.width = canvas.height = (qr.size + 2 * quietZone) * scale;
  const context = canvas.getContext("2d");
  if (!context) return false;
  context.fillStyle = "#fff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = "#000";
  for (let y = 0; y < qr.size; y += 1) {
    for (let x = 0; x < qr.size; x += 1) {
      if (qr.getModule(x, y)) {
        context.fillRect((x + quietZone) * scale, (y + quietZone) * scale, scale, scale);
      }
    }
  }
  return true;
}

function base64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

async function newDisplayKey() {
  const pair = await crypto.subtle.generateKey({
    name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256",
  }, false, ["encrypt", "decrypt"]);
  return {
    privateKey: pair.privateKey,
    publicKey: base64url(await crypto.subtle.exportKey("spki", pair.publicKey)),
  };
}

async function decrypt(privateKey, value) {
  const encoded = value.replaceAll("-", "+").replaceAll("_", "/");
  const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0));
  return new TextDecoder().decode(await crypto.subtle.decrypt("RSA-OAEP", privateKey, bytes));
}

class HoreveilAssociation extends HTMLElement {
  #hass;
  #admin;
  #generation = 0;
  #busy = false;
  #testOrigin;
  #directOrigin;
  #directConfigured = false;
  #httpsOrigins;
  #configuredOrigin;
  #loaded = false;
  #invitation;
  #timer;
  #expiryTimer;

  constructor() {
    super();
    this.attachShadow({ mode: "open" }).innerHTML = template;
    this.#element("address").addEventListener("submit", (event) => {
      event.preventDefault();
      this.#create();
    });
    this.#element("origin").addEventListener("input", () => {
      this.#element("direct-confirm").checked = false;
      this.#address();
    });
    this.#element("local").onclick = () => {
      this.#element("origin").value = this.#testOrigin;
      this.#element("direct-confirm").checked = false;
      this.#address();
    };
    this.#element("direct-confirm").onchange = () => this.#address();
    this.#element("direct-toggle").onclick = () => this.#toggleDirect();
    this.#element("compared").onchange = () => this.#invitationState();
    this.#element("approve").onclick = () => this.#decide(true);
    this.#element("cancel").onclick = () => this.#decide(false);
    this.#element("regenerate").onclick = () => this.#regenerate();
    this.#element("refresh").onclick = () => this.#run((generation) => this.#load(generation));
    this.#element("content").addEventListener("change", (event) => {
      if (event.target.name !== "mode") return;
      const commands = this.#element("commands");
      commands.disabled = !this.#element("control-mode").checked;
      if (commands.disabled) for (const option of commands.querySelectorAll("input")) option.checked = false;
    });
  }

  set hass(value) {
    this.#hass = value;
    const user = value?.user;
    const admin = user?.is_admin && value.connected !== false ? user.id : undefined;
    if (admin === this.#admin) return;
    this.#reset();
    this.#admin = admin;
    if (this.isConnected && admin) this.#run((generation) => this.#load(generation));
  }

  connectedCallback() {
    if (this.#admin) this.#run((generation) => this.#load(generation));
    this.#timer = setInterval(() => this.#poll(), 2000);
  }

  disconnectedCallback() {
    clearInterval(this.#timer);
    this.#reset();
  }

  #element(id) {
    return this.shadowRoot.getElementById(id);
  }

  #notice(message) {
    this.#element("notice").textContent = message;
  }

  #reset() {
    this.#generation += 1;
    this.#busy = false;
    this.#testOrigin = undefined;
    this.#directOrigin = undefined;
    this.#directConfigured = false;
    this.#httpsOrigins = undefined;
    this.#element("https-origins").replaceChildren();
    this.#configuredOrigin = undefined;
    this.#loaded = false;
    this.#element("direct-confirm").checked = false;
    this.#dropInvitation();
    this.#element("content").hidden = true;
    this.#element("origin").value = "";
    this.#element("phones").replaceChildren();
    this.#element("calendar-entries").replaceChildren();
    this.#notice("Accès réservé à un administrateur connecté en HTTPS.");
  }

  #dropInvitation() {
    clearTimeout(this.#expiryTimer);
    this.#invitation = undefined;
    this.#element("code").textContent = "";
    this.#element("comparison").textContent = "";
    this.#element("compared").checked = false;
    this.#invitationState();
  }

  #call(command, fields = {}) {
    return this.#hass.callWS({ type: COMMAND + command, ...fields });
  }

  async #run(operation, fallback = "Opération indisponible.") {
    if (this.#busy || !this.#admin || !this.isConnected) return;
    const generation = this.#generation;
    this.#busy = true;
    this.#invitationState();
    try {
      await operation(generation);
    } catch (error) {
      if (generation !== this.#generation) return;
      if (error?.code === "unauthorized") this.#reset();
      else this.#notice(fallback);
    } finally {
      if (generation === this.#generation) {
        this.#busy = false;
        this.#invitationState();
      }
    }
  }

  #address() {
    const parsed = parseOrigin(this.#element("origin").value.trim());
    const usable = parsed && (this.#httpsOrigins ?? [this.#testOrigin, this.#directOrigin]).includes(parsed.origin);
    this.#element("host").textContent = parsed?.host ?? "—";
    this.#element("availability").textContent = usable
      ? "Adresse autorisée pour créer une invitation. La joignabilité n’est pas vérifiée."
      : this.#httpsOrigins
        ? (this.#httpsOrigins.length ? "Choisissez une adresse HTTPS proposée par Home Assistant."
          : "Home Assistant ne propose aucune adresse HTTPS pour le téléphone.")
      : this.#configuredOrigin
        ? `Confirmez l’adresse configurée pour le relais HTTPS (${this.#configuredOrigin}).`
      : "Choisissez une adresse de recette ou confirmez le relais HTTPS avant de créer une invitation.";
    this.#element("local").hidden = !this.#testOrigin;
    this.#element("direct").hidden = !this.#directConfigured;
    this.#element("direct-state").textContent = this.#directOrigin
      ? `Accès direct actif sur ${this.#directOrigin}` : "Accès direct désactivé";
    this.#element("direct-toggle").textContent = this.#directOrigin
      ? "Désactiver l’accès direct" : "Activer l’accès direct";
    this.#element("direct-confirm-label").hidden = Boolean(this.#directOrigin);
    this.#element("direct-toggle").disabled = this.#busy || (!this.#directOrigin && (
      !parsed || parsed.origin === this.#testOrigin || !this.#element("direct-confirm").checked
      || (this.#configuredOrigin && parsed.origin !== this.#configuredOrigin)
    ));
    this.#element("create").disabled = !usable || this.#busy || Boolean(this.#invitation);
    return usable ? parsed.origin : null;
  }

  async #load(generation) {
    const info = await this.#call("info");
    if (generation !== this.#generation) return;
    if (!this.#loaded) this.#element("origin").value = info.configured_origin ?? info.suggested_origin ?? "";
    this.#loaded = true;
    this.#testOrigin = info.test_origin;
    this.#directOrigin = info.direct_origin;
    this.#directConfigured = info.direct_configured === true;
    this.#httpsOrigins = info.https_origins;
    this.#element("https-origins").replaceChildren(...(this.#httpsOrigins ?? []).map((origin) => {
      const option = document.createElement("option");
      option.value = origin;
      return option;
    }));
    this.#configuredOrigin = info.configured_origin;
    this.#element("content").hidden = false;
    this.#element("empty").hidden = info.phones.length > 0;
    this.#element("phones").replaceChildren(...info.phones.map((id) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "Révoquer";
      button.setAttribute("aria-label", `Révoquer le téléphone ${id}`);
      button.onclick = () => this.#revoke(id);
      item.append(`Téléphone ${id} `, button);
      return item;
    }));
    this.#element("calendar-sources").hidden = !info.calendars?.length;
    this.#element("calendar-entries").replaceChildren(...(info.calendars ?? []).map((entry) => {
      const row = document.createElement("div");
      const label = document.createElement("label");
      const select = document.createElement("select");
      const choices = [["", "Webhook local"], ...info.phones.map((id) => [id, `Téléphone ${id}`])];
      if (entry.pairing_id && !info.phones.includes(entry.pairing_id)) {
        choices.push([entry.pairing_id, "Source révoquée · écritures bloquées"]);
      }
      for (const [value, title] of choices) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = title;
        select.append(option);
      }
      select.value = entry.pairing_id ?? "";
      label.append(`${entry.title} · source active `, select);
      const save = document.createElement("button");
      save.type = "button";
      save.textContent = "Autoriser cette source";
      save.disabled = true;
      select.onchange = () => { save.disabled = select.value === (entry.pairing_id ?? ""); };
      save.onclick = () => {
        if (!window.confirm("Changer la source et effacer la projection actuelle ?")) return;
        this.#run(async (generation) => {
          await this.#call("calendar_source", {
            entry_id: entry.entry_id, pairing_id: select.value || null, confirmed: true,
          });
          await this.#load(generation);
        });
      };
      const last = document.createElement("p");
      last.textContent = entry.last_success
        ? `Dernière réception réussie · ${new Date(entry.last_success).toLocaleString()}`
        : "Aucune réception pour cette source.";
      row.append(label, save, last);
      return row;
    }));
    this.#notice("");
    return info.phones;
  }

  #toggleDirect() {
    const enabled = !this.#directOrigin;
    const origin = parseOrigin(this.#element("origin").value.trim())?.origin;
    if (enabled && (!origin || !this.#element("direct-confirm").checked)) return;
    this.#run(async (generation) => {
      const fields = { enabled, confirmed: true };
      if (enabled) fields.origin = origin;
      const result = await this.#call("direct_access", fields);
      if (generation !== this.#generation) return;
      if (!enabled && this.#invitation?.origin === this.#directOrigin) this.#dropInvitation();
      this.#directOrigin = result.direct_origin;
      this.#element("direct-confirm").checked = false;
      this.#notice(enabled ? "Accès direct activé pour cette session de Home Assistant." : "Accès direct désactivé.");
    }, "Impossible de modifier l’accès direct. Vérifiez le relais HTTPS et l’origine.");
  }

  #create() {
    const origin = this.#address();
    if (!origin || this.#invitation) return;
    this.#run((generation) => this.#issue(origin, generation),
      "Impossible de créer l’invitation. Une invitation peut déjà être ouverte.");
  }

  async #issue(origin, generation) {
    const key = await newDisplayKey();
    if (generation !== this.#generation) return;
    const result = await this.#call("invite", { origin, display_key: key.publicKey });
    if (generation !== this.#generation) return;
    const code = await decrypt(key.privateKey, result.code_encrypted);
    if (generation !== this.#generation) return;
    const expiration = Date.parse(result.expires_at);
    if (!Number.isFinite(expiration) || expiration <= Date.now()) {
      this.#notice("Invitation expirée.");
      return;
    }
    const invitation = {
      invitation_id: result.invitation_id, origin: result.origin, expires_at: result.expires_at,
      key, state: "issued",
    };
    this.#invitation = invitation;
    this.#element("invitation-origin").textContent = invitation.origin;
    this.#element("code").textContent = code;
    this.#element("expires").dateTime = invitation.expires_at;
    this.#element("expires").textContent = new Date(expiration).toLocaleString();
    if (this.#httpsOrigins?.includes(invitation.origin) || invitation.origin === this.#directOrigin) {
      const drawn = drawInvitationQr(this.#element("qr-canvas"), invitation.origin, code, invitation.expires_at);
      this.#element("qr").hidden = !drawn;
    }
    this.#expiryTimer = setTimeout(
      () => this.#expire(invitation), Math.max(0, Math.min(300_000, expiration - Date.now())),
    );
    this.#notice("");
  }

  #regenerate() {
    const invitation = this.#invitation;
    if (invitation?.state !== "issued") return;
    this.#run(async (generation) => {
      await this.#call("cancel", { invitation_id: invitation.invitation_id });
      if (generation !== this.#generation) return;
      this.#dropInvitation();
      await this.#issue(invitation.origin, generation);
    }, "Impossible de régénérer l’invitation.");
  }

  #expire(invitation) {
    if (this.#invitation !== invitation) return;
    this.#dropInvitation();
    this.#notice("Invitation expirée.");
  }

  #invitationState() {
    const state = this.#invitation?.state;
    const reviewing = state === "awaiting_approval";
    this.#element("invitation").hidden = !state;
    this.#element("manual").hidden = state !== "issued";
    const direct = this.#httpsOrigins?.includes(this.#invitation?.origin) || this.#invitation?.origin === this.#directOrigin;
    const qr = this.#element("qr");
    if (state !== "issued" || !direct) {
      const canvas = this.#element("qr-canvas");
      canvas.width = canvas.height = 0;
      qr.hidden = true;
    }
    const qrUnavailable = this.#element("qr-unavailable");
    qrUnavailable.hidden = !qr.hidden;
    qrUnavailable.textContent = direct
      ? "QR indisponible. Utilisez la saisie manuelle."
      : "La recette sur la boucle locale utilise le code manuel avec un simulateur sur cet hôte.";
    this.#element("regenerate").disabled = this.#busy;
    this.#element("review").hidden = !reviewing;
    this.#element("state").textContent = {
      issued: "En attente de demande", awaiting_approval: "À vérifier",
      approved: "Approuvée, en attente de confirmation du client",
    }[state] ?? "";
    this.#element("cancel").textContent = reviewing ? "Refuser la demande" : "Annuler l’invitation";
    this.#element("cancel").disabled = this.#busy;
    this.#element("approve").disabled = this.#busy || !reviewing || !this.#element("compared").checked;
    this.#address();
  }

  #poll() {
    const invitation = this.#invitation;
    if (!invitation) return;
    if (Date.now() >= Date.parse(invitation.expires_at)) {
      this.#expire(invitation);
      return;
    }
    this.#run(async (generation) => {
      let result;
      try {
        result = await this.#call("status", {
          invitation_id: invitation.invitation_id, display_key: invitation.key.publicKey,
        });
      } catch (error) {
        if (error?.code !== "invitation_unavailable") throw error;
        if (generation !== this.#generation || this.#invitation !== invitation) return;
        this.#dropInvitation();
        const phones = await this.#load(generation);
        if (generation === this.#generation) {
          this.#notice(phones.includes(invitation.pairing_id) ? "Téléphone associé." : "Invitation indisponible.");
        }
        return;
      }
      if (generation !== this.#generation || this.#invitation !== invitation || result.state === invitation.state) return;
      const comparison = result.state === "awaiting_approval"
        ? await decrypt(invitation.key.privateKey, result.comparison_code_encrypted) : "";
      if (generation !== this.#generation || this.#invitation !== invitation) return;
      invitation.state = result.state;
      invitation.pairing_id = result.pairing_id;
      this.#element("code").textContent = "";
      this.#element("comparison").textContent = comparison;
      this.#element("compared").checked = false;
    });
  }

  #decide(approve) {
    const invite = this.#invitation;
    if (!invite || (approve && !this.#element("compared").checked)) return;
    this.#run(async (generation) => {
      const fields = { invitation_id: invite.invitation_id };
      if (approve) Object.assign(fields, { pairing_id: invite.pairing_id, confirmed: true });
      await this.#call(approve ? "approve" : "cancel", fields);
      if (generation !== this.#generation || this.#invitation !== invite) return;
      if (approve) {
        invite.state = "approved";
        this.#element("comparison").textContent = "";
        this.#element("compared").checked = false;
      } else {
        this.#dropInvitation();
        this.#notice("Invitation annulée.");
      }
    });
  }

  #revoke(id) {
    if (!window.confirm(`Révoquer l’accès du téléphone ${id} ?`)) return;
    this.#run(async (generation) => {
      await this.#call("revoke", { pairing_id: id, confirmed: true });
      if (generation !== this.#generation) return;
      await this.#load(generation);
      if (generation === this.#generation) this.#notice("Accès révoqué.");
    });
  }
}

customElements.define("horeveil-association", HoreveilAssociation);
