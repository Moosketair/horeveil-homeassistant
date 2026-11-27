import { qrcodegen } from "./pairing-qrcode.js";

const COMMAND = "horeveil/pairing_test/";

const template = `
  <style>
    :host { display: block; min-height: 100%; color: var(--primary-text-color);
      background: var(--primary-background-color); font-family: var(--ha-font-family-body, inherit); }
    header { display: flex; align-items: center; gap: 12px; min-height: var(--header-height, 56px);
      padding: 0 16px; color: var(--app-header-text-color, var(--primary-text-color));
      background: var(--app-header-background-color, var(--card-background-color));
      border-bottom: 1px solid var(--divider-color); }
    h1, h2, h3, p, figure, ul { margin: 0; }
    h1 { font-size: var(--ha-font-size-xl, 1.25rem); font-weight: 500; }
    h2 { font-size: var(--ha-font-size-l, 1.125rem); font-weight: 600; }
    h3 { font-size: var(--ha-font-size-m, 1rem); font-weight: 600; overflow-wrap: anywhere; }
    p, label, li { line-height: 1.5; }
    main { max-width: 1100px; margin: auto; padding: 24px 20px; }
    #content { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; align-items: start; }
    section { display: grid; gap: 16px; min-width: 0; padding: 20px;
      background: var(--ha-card-background, var(--card-background-color));
      border: var(--ha-card-border-width, 1px) solid var(--ha-card-border-color, var(--divider-color));
      border-radius: var(--ha-card-border-radius, 12px); }
    .heading, .actions, .check { display: flex; align-items: center; gap: 8px; }
    .heading ha-icon { color: var(--primary-color); --mdc-icon-size: 22px; }
    .actions { flex-wrap: wrap; }
    .stack { display: grid; gap: 12px; }
    .field { display: grid; gap: 6px; font-weight: 500; }
    input:not([type=checkbox]), select { box-sizing: border-box; width: 100%; min-height: 44px;
      padding: 8px 12px; font: inherit; color: inherit; background: var(--secondary-background-color);
      border: 1px solid var(--divider-color); border-radius: var(--ha-border-radius-md, 8px); }
    :host { color-scheme: var(--color-scheme, light dark); }
    input[type=checkbox] { width: 18px; height: 18px; margin: 0; flex: none; accent-color: var(--primary-color); }
    :is(button, a, input, select):focus-visible { outline: 2px solid var(--primary-color); outline-offset: 3px; }
    button, .button { display: inline-flex; align-items: center; justify-content: center; min-height: 42px;
      box-sizing: border-box; padding: 7px 14px; font: inherit; font-weight: 600; text-decoration: none;
      color: var(--primary-text-color); background: transparent; border: 1px solid var(--divider-color);
      border-radius: var(--ha-border-radius-lg, 12px); cursor: pointer; text-align: center; }
    button:hover, .button:hover { background: var(--secondary-background-color); }
    .primary, .primary:hover { background: var(--primary-color);
      color: var(--primary-color-text, var(--text-primary-color)); border-color: transparent; }
    .danger { color: var(--error-color); }
    button:disabled { color: var(--disabled-text-color); background: var(--secondary-background-color); cursor: default; }
    .muted { color: var(--secondary-text-color); }
    #notice:not(:empty) { margin-bottom: 16px; }
    #notice:not(:empty), #invitation { background: var(--secondary-background-color); padding: 12px 14px;
      border-inline-start: 3px solid var(--primary-color); border-radius: var(--ha-border-radius-md, 8px); }
    #notice[data-kind=error] { border-inline-start-color: var(--error-color); }
    #notice[data-kind=success] { border-inline-start-color: var(--success-color); }
    #network { margin-bottom: 16px; }
    #phones { list-style: none; padding: 0; }
    #phones > li, #calendar-entries > div { display: grid; gap: 8px; padding-block: 14px;
      border-top: 1px solid var(--divider-color); }
    #phones > li:last-child, #calendar-entries > div:last-child { padding-bottom: 0; }
    #calendar-sources { grid-column: 1 / -1; }
    #calendar-entries { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr)); gap: 16px; }
    code, output, p { overflow-wrap: anywhere; }
    #code, #comparison { font-size: 1.05rem; letter-spacing: .08em; }
    #qr-canvas { display: block; width: min(100%, 248px); height: auto; image-rendering: pixelated; margin-bottom: 8px; }
    dialog { box-sizing: border-box; width: min(456px, calc(100% - 32px)); max-height: calc(100% - 32px); padding: 24px;
      color: var(--primary-text-color); background: var(--dialog-surface-background, var(--card-background-color));
      border: 1px solid var(--divider-color); border-radius: var(--ha-card-border-radius, 12px); }
    dialog::backdrop { background: var(--dialog-backdrop-filter-color, rgb(0 0 0 / 45%)); }
    dialog .actions { justify-content: flex-end; }
    :host([narrow]) #content { grid-template-columns: minmax(0, 1fr); }
    @media (max-width: 700px) {
      main { padding: 16px 12px; }
      section { padding: 16px; }
      #content { grid-template-columns: minmax(0, 1fr); }
    }
    [hidden] { display: none !important; }
  </style>
  <header>
    <ha-menu-button></ha-menu-button>
    <h1>Horéveil</h1>
  </header>
  <main>
    <section id="network" aria-labelledby="network-title" hidden>
      <h2 id="network-title">Accès HTTPS requis</h2>
      <p>HTTPS protège la session d’administration et le code d’association. Ouvrez Home Assistant depuis une adresse HTTPS avec un certificat reconnu par votre navigateur.</p>
      <p id="https-help" class="muted">Si vous ne disposez pas encore d’une adresse sécurisée, configurez-en une dans Home Assistant. Retrouvez les adresses de votre instance dans Paramètres → Système → Réseau.</p>
      <div id="https-action" hidden><a id="https-link" class="button primary" rel="noreferrer">Ouvrir Horéveil en HTTPS</a></div>
    </section>
    <p id="notice" role="status">Chargement du panneau…</p>
    <button id="retry" type="button" hidden>Réessayer</button>
    <div id="content" hidden>
      <section id="phones-card" aria-labelledby="phones-title">
        <div class="heading"><ha-icon icon="mdi:cellphone" aria-hidden="true"></ha-icon><h2 id="phones-title">Téléphones associés</h2></div>
        <p id="empty">Aucun téléphone associé.</p>
        <ul id="phones" hidden></ul>
        <div><button id="refresh" type="button">Actualiser la liste</button></div>
      </section>
      <section aria-labelledby="add-title">
        <div class="heading"><ha-icon icon="mdi:qrcode-scan" aria-hidden="true"></ha-icon><h2 id="add-title">Associer un téléphone</h2></div>
        <form id="address" class="stack">
          <label class="field" for="origin">Adresse HTTPS de Home Assistant
            <input id="origin" type="url" required autocomplete="off" spellcheck="false" list="https-origins" aria-describedby="availability">
          </label>
          <datalist id="https-origins"></datalist>
          <p id="availability" class="muted"></p>
          <div id="direct" class="stack" hidden>
            <p id="direct-state"></p>
            <label id="direct-confirm-label" class="check"><input id="direct-confirm" type="checkbox">
              Je confirme l’origine et le relais HTTPS configuré.
            </label>
            <div><button id="direct-toggle" type="button" disabled>Activer l’accès direct</button></div>
          </div>
          <div class="actions">
            <button id="create" class="primary" type="submit" disabled>Créer une invitation</button>
            <button id="local" type="button">Utiliser l’adresse de recette</button>
          </div>
        </form>
        <div id="invitation" class="stack" hidden>
          <h3 id="state" role="status"></h3>
          <p>Validité de cinq minutes · Expire à <time id="expires"></time></p>
          <div id="manual" class="stack">
            <figure id="qr" hidden>
              <canvas id="qr-canvas" width="0" height="0" role="img" aria-label="QR temporaire d’association Horéveil"></canvas>
              <figcaption>Dans Horéveil Android, ouvrez Réglages → Intégrations → Home Assistant et scannez le QR code.</figcaption>
            </figure>
            <p id="qr-unavailable" class="muted"></p>
            <p>Adresse <output id="invitation-origin"></output><br>Code de saisie manuelle <code id="code"></code></p>
            <div><button id="regenerate" type="button">Régénérer l’invitation</button></div>
          </div>
          <div id="review" class="stack" hidden>
            <p>Comparez ce code avec celui affiché sur le téléphone. <strong><code id="comparison"></code></strong></p>
            <label class="check"><input id="compared" type="checkbox"> Les deux codes sont identiques.</label>
            <div><button id="approve" class="primary" type="button" disabled>Approuver dans Home Assistant</button></div>
          </div>
          <p id="android-confirm" hidden>Confirmez maintenant l’association dans Horéveil Android.</p>
          <div><button id="cancel" type="button">Annuler l’invitation</button></div>
        </div>
      </section>
      <section id="calendar-sources" aria-labelledby="calendar-sources-title" hidden>
        <div class="heading"><ha-icon icon="mdi:calendar-sync" aria-hidden="true"></ha-icon><h2 id="calendar-sources-title">Synchronisation du calendrier</h2></div>
        <p>Une seule source peut écrire dans chaque calendrier. Pour un téléphone associé, le partage du calendrier et le choix des réveils se règlent dans Horéveil Android.</p>
        <div id="calendar-entries"></div>
      </section>
    </div>
  </main>
  <dialog id="dialog" aria-labelledby="dialog-title" aria-describedby="dialog-description">
    <form id="dialog-form" class="stack">
      <h2 id="dialog-title"></h2>
      <p id="dialog-description"></p>
      <label id="label-field" class="field" for="phone-label" hidden>Libellé du téléphone
        <input id="phone-label" maxlength="64" autocomplete="off">
      </label>
      <div class="actions">
        <button id="dismiss" type="button">Annuler</button>
        <button id="confirm" type="submit"></button>
      </div>
    </form>
  </dialog>`;

function parseOrigin(value) {
  if (!/^https:\/\/[^/?#\\\s]+\/?$/i.test(value)) return null;
  try {
    const url = new URL(value);
    return url.username || url.password ? null : url;
  } catch {
    return null;
  }
}

function actionButton(title, action, className = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = title;
  button.onclick = action;
  return button;
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
  #stale = false;
  #panelOrigin;
  #mutation;
  #interruptedMutation;
  #polling;
  #dialogAction;
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
    this.#element("refresh").onclick = () => this.#refresh();
    this.#element("retry").onclick = () => this.#refresh();
    const dialog = this.#element("dialog");
    this.#element("dismiss").onclick = () => dialog.close();
    this.#element("dialog-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const action = this.#dialogAction;
      const label = this.#element("phone-label").value;
      this.#dialogAction = undefined;
      dialog.close();
      action?.(label);
    });
    dialog.addEventListener("close", () => {
      this.#dialogAction = undefined;
      this.#element("phone-label").value = "";
    });
  }

  set narrow(value) {
    this.toggleAttribute("narrow", Boolean(value));
  }

  set panel(value) {
    this.#panelOrigin = value?.config?.https_origin;
    this.#network(Boolean(this.#admin));
  }

  set hass(value) {
    this.#hass = value;
    const user = value?.user;
    const admin = user?.is_admin && value.connected !== false ? user.id : undefined;
    this.#network(Boolean(admin));
    if (admin !== this.#admin) {
      if (!admin && user?.is_admin && this.#mutation?.admin === user.id) {
        this.#interruptedMutation = this.#mutation;
      } else if (this.#interruptedMutation?.admin !== admin) {
        this.#interruptedMutation = undefined;
      }
      this.#reset();
      this.#admin = admin;
      if (this.isConnected && admin && location.protocol === "https:") this.#refresh();
      else if (this.#interruptedMutation) this.#notice(this.#interruptedMutation.message, "error");
    }
  }

  connectedCallback() {
    this.#network(Boolean(this.#admin));
    if (this.#admin && location.protocol === "https:") this.#refresh();
    if (location.protocol !== "https:") this.#notice("");
    this.#timer = setInterval(() => this.#poll(), 2000);
  }

  disconnectedCallback() {
    clearInterval(this.#timer);
    this.#interruptedMutation = undefined;
    this.#reset();
  }

  #element(id) {
    return this.shadowRoot.getElementById(id);
  }

  #notice(message, kind = "") {
    const notice = this.#element("notice");
    notice.dataset.kind = kind;
    notice.textContent = message;
  }

  #network(admin) {
    const insecure = location.protocol !== "https:";
    const origin = admin && insecure && this.#panelOrigin;
    const link = this.#element("https-link");
    if (origin) link.href = new URL("/horeveil-association", origin).href;
    else link.removeAttribute("href");
    this.#element("https-action").hidden = !origin;
    this.#element("https-help").hidden = Boolean(origin);
    this.#element("network").hidden = !insecure;
  }

  #refresh() {
    const interrupted = this.#interruptedMutation;
    if (!this.#loaded && !interrupted) this.#notice("Chargement du panneau…");
    this.#run(async (generation) => {
      await this.#load(generation);
      if (generation === this.#generation && interrupted) {
        this.#notice(`${interrupted.message} Le panneau a été actualisé.`);
        this.#interruptedMutation = undefined;
      }
    }, interrupted ? `${interrupted.message} L’actualisation du panneau a échoué. Réessayez.`
      : this.#stale ? "L’actualisation du panneau a échoué. Réessayez."
        : "Impossible de charger le panneau. Vérifiez votre session puis réessayez.");
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
    this.#stale = false;
    this.#mutation = undefined;
    this.#element("phones").inert = false;
    this.#element("calendar-entries").inert = false;
    this.#polling = undefined;
    this.#dialogAction = undefined;
    this.#element("dialog").close();
    this.#element("retry").hidden = true;
    this.#element("direct-confirm").checked = false;
    this.#dropInvitation();
    this.#element("content").hidden = true;
    this.#element("origin").value = "";
    this.#element("phones").replaceChildren();
    this.#element("calendar-entries").replaceChildren();
    this.#notice(location.protocol === "https:" ? "Connectez-vous en tant qu’administrateur." : "");
  }

  #dropInvitation() {
    clearTimeout(this.#expiryTimer);
    this.#invitation = undefined;
    this.#element("code").textContent = "";
    this.#element("comparison").textContent = "";
    this.#element("invitation-origin").textContent = "";
    this.#element("compared").checked = false;
    this.#invitationState();
  }

  #call(command, fields = {}) {
    return this.#hass.callWS({ type: COMMAND + command, ...fields });
  }

  async #run(operation, fallback = "Opération indisponible.") {
    if (this.#busy || !this.#admin || !this.isConnected || location.protocol !== "https:") return;
    const generation = this.#generation;
    this.#busy = true;
    this.#element("retry").hidden = true;
    this.#element("phones").inert = true;
    this.#element("calendar-entries").inert = true;
    this.#invitationState();
    try {
      await operation(generation);
    } catch (error) {
      if (generation !== this.#generation) return;
      if (error?.code === "unauthorized") {
        this.#reset();
        this.#notice("Home Assistant a refusé la session d’administration HTTPS.", "error");
      } else {
        this.#notice(fallback, "error");
      }
      this.#element("retry").hidden = this.#loaded && !this.#stale;
    } finally {
      if (generation === this.#generation) {
        this.#busy = false;
        this.#element("phones").inert = false;
        this.#element("calendar-entries").inert = false;
        if (this.#stale) this.#element("retry").hidden = false;
        this.#invitationState();
      }
    }
  }

  #markStale(message) {
    this.#stale = true;
    this.#element("phones-card").hidden = true;
    this.#element("calendar-sources").hidden = true;
    this.#element("retry").hidden = false;
    this.#notice(message, "error");
  }

  #mutate(command, fields, { success, rejected, failure, unknown }) {
    this.#run(async (generation) => {
      const mutation = { admin: this.#admin, message: unknown };
      this.#mutation = mutation;
      try {
        try {
          await this.#call(command, fields);
        } catch (error) {
          if (generation !== this.#generation) return;
          if (error?.code === "unauthorized") throw error;
          if (!rejected.includes(error?.code)) this.#markStale(`${unknown} Actualisez le panneau.`);
          else if (error.code === "phone_unavailable") this.#markStale(`${failure} Actualisez le panneau.`);
          else this.#notice(failure, "error");
          return;
        }
        if (generation !== this.#generation) return;
        mutation.message = `${success}, mais l’actualisation de l’affichage a échoué.`;
        try {
          await this.#load(generation);
        } catch {
          if (generation === this.#generation) this.#markStale(`${mutation.message} Actualisez le panneau.`);
          return;
        }
        if (generation === this.#generation) this.#notice(`${success}.`, "success");
      } finally {
        if (this.#mutation === mutation) this.#mutation = undefined;
      }
    });
  }

  #address() {
    const parsed = parseOrigin(this.#element("origin").value.trim());
    const usable = parsed && (this.#httpsOrigins ?? [this.#testOrigin, this.#directOrigin]).includes(parsed.origin);
    this.#element("availability").textContent = usable
      ? "Cette adresse doit aussi être accessible depuis votre téléphone."
      : this.#httpsOrigins
        ? (this.#httpsOrigins.length ? "Choisissez une adresse HTTPS proposée par Home Assistant."
          : "Home Assistant ne propose aucune adresse HTTPS pour le téléphone.")
      : this.#configuredOrigin
        ? `Confirmez l’adresse configurée pour le relais HTTPS (${this.#configuredOrigin}).`
      : "Choisissez une adresse de recette ou confirmez le relais HTTPS avant de créer une invitation.";
    this.#element("local").hidden = !this.#testOrigin;
    this.#element("local").disabled = this.#busy || Boolean(this.#invitation);
    this.#element("origin").readOnly = Boolean(this.#invitation);
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
    this.#element("refresh").disabled = this.#busy;
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
    const calendars = info.calendars ?? [];
    const eligible = new Set(info.calendar_phones ?? []);
    const name = (id) => info.phone_labels?.[id] ?? `Téléphone ${id.slice(0, 8)}`;
    const phones = this.#element("phones");
    this.#element("empty").hidden = info.phones.length > 0;
    phones.hidden = !info.phones.length;
    phones.replaceChildren(...info.phones.map((id) => {
      const item = document.createElement("li");
      const title = document.createElement("h3");
      title.textContent = name(id);
      const details = document.createElement("p");
      details.className = "muted";
      details.textContent = "Associé · Modèle indisponible";
      const sync = document.createElement("p");
      const receipts = calendars.filter((entry) => entry.pairing_id === id && entry.last_success);
      const last = Math.max(...receipts.map((entry) => Date.parse(entry.last_success)));
      sync.textContent = Number.isFinite(last)
        ? `Dernière réception du calendrier le ${new Date(last).toLocaleString()}`
        : "Aucune réception du calendrier disponible pour ce téléphone.";
      const actions = document.createElement("div");
      actions.className = "actions";
      const rename = actionButton("Renommer", () => this.#rename(id, info.phone_labels?.[id]));
      const revoke = actionButton("Révoquer", () => this.#revoke(id, name(id)), "danger");
      rename.setAttribute("aria-label", `Renommer ${name(id)}`);
      revoke.setAttribute("aria-label", `Révoquer ${name(id)}`);
      actions.append(rename, revoke);
      item.append(title, details, sync, actions);
      return item;
    }));
    this.#element("calendar-sources").hidden = !calendars.length;
    this.#element("calendar-entries").replaceChildren(...calendars.map((entry) => {
      const row = document.createElement("div");
      const title = document.createElement("h3");
      title.textContent = entry.title;
      const current = document.createElement("p");
      const label = document.createElement("label");
      label.className = "field";
      const select = document.createElement("select");
      const selected = entry.pairing_id ?? "";
      const choices = new Map([["", "Webhook local"]]);
      for (const id of eligible) {
        if (id !== selected && calendars.some((other) => other !== entry && other.pairing_id === id)) continue;
        choices.set(id, `${name(id)} · HTTPS direct`);
      }
      if (!choices.has(selected)) choices.set(selected, "Téléphone indisponible · écritures bloquées");
      current.textContent = `Source active · ${choices.get(selected)}`;
      for (const [value, title] of choices) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = title;
        select.append(option);
      }
      select.value = selected;
      select.setAttribute("aria-label", `Nouvelle source pour ${entry.title}`);
      label.append("Nouvelle source", select);
      const save = actionButton("Changer la source", () => {
        const next = select.value;
        this.#showDialog({
          title: "Changer la source du calendrier",
          description: `Le calendrier « ${entry.title} » utilisera ${choices.get(next)}. La projection actuelle sera effacée jusqu’à la prochaine réception.`,
          button: "Changer la source",
          action: () => this.#mutate("calendar_source", {
            entry_id: entry.entry_id, pairing_id: next || null, confirmed: true,
          }, {
            success: "La source du calendrier a été modifiée",
            rejected: ["calendar_source_unavailable"],
            failure: "Home Assistant a refusé le changement de source du calendrier.",
            unknown: "Le résultat du changement de source du calendrier est indéterminé.",
          }),
        });
      });
      save.disabled = true;
      select.onchange = () => { save.disabled = select.value === selected; };
      const last = document.createElement("p");
      last.className = "muted";
      last.textContent = entry.last_success
        ? `Dernière réception réussie le ${new Date(entry.last_success).toLocaleString()}`
        : "Aucune réception pour cette source.";
      const actions = document.createElement("div");
      actions.append(save);
      row.append(title, current, last, label, actions);
      return row;
    }));
    this.#stale = false;
    this.#element("phones-card").hidden = false;
    this.#notice("");
    return info.phones;
  }

  #showDialog({ title, description, button, action, label, destructive = false }) {
    if (this.#busy) return;
    const dialog = this.#element("dialog");
    const renaming = label !== undefined;
    this.#element("label-field").hidden = !renaming;
    const input = this.#element("phone-label");
    input.value = label ?? "";
    this.#element("dialog-title").textContent = title;
    this.#element("dialog-description").textContent = description;
    const confirm = this.#element("confirm");
    confirm.textContent = button;
    confirm.className = destructive ? "danger" : "primary";
    this.#dialogAction = action;
    dialog.showModal();
    (renaming ? input : this.#element("dismiss")).focus();
  }

  #rename(id, label = "") {
    this.#showDialog({
      title: "Renommer le téléphone",
      description: "Le libellé est enregistré dans Home Assistant. Laissez le champ vide pour rétablir le nom généré.",
      button: "Enregistrer",
      label,
      action: (value) => this.#mutate("rename", { pairing_id: id, label: value }, {
        success: "Le libellé du téléphone a été enregistré",
        rejected: ["invalid_label", "phone_unavailable", "rename_unavailable"],
        failure: "Home Assistant a refusé le renommage. Vérifiez le téléphone et le libellé (64 caractères maximum).",
        unknown: "Le résultat du renommage est indéterminé.",
      }),
    });
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
    this.#element("expires").textContent = new Date(expiration).toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    if (this.#httpsOrigins?.includes(invitation.origin) || invitation.origin === this.#directOrigin) {
      const drawn = drawInvitationQr(this.#element("qr-canvas"), invitation.origin, code, invitation.expires_at);
      this.#element("qr").hidden = !drawn;
    }
    this.#expiryTimer = setTimeout(
      () => this.#expire(invitation), Math.max(0, Math.min(300_000, expiration - Date.now())),
    );
    this.#notice("Invitation créée. Elle est valable cinq minutes.", "success");
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
    if (invitation.state === "approved") {
      this.#poll();
      return;
    }
    this.#dropInvitation();
    this.#notice("Invitation expirée. Créez une nouvelle invitation pour recommencer.", "error");
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
    this.#element("android-confirm").hidden = state !== "approved";
    const stateText = {
      issued: "En attente du téléphone", awaiting_approval: "Code de sécurité à vérifier",
      approved: "En attente de confirmation sur Android",
    }[state] ?? "";
    if (this.#element("state").textContent !== stateText) this.#element("state").textContent = stateText;
    this.#element("cancel").textContent = reviewing ? "Refuser la demande" : "Annuler l’invitation";
    this.#element("cancel").disabled = this.#busy;
    this.#element("approve").disabled = this.#busy || !reviewing || !this.#element("compared").checked;
    this.#address();
  }

  async #poll() {
    const invitation = this.#invitation;
    if (!invitation || this.#busy || this.#polling) return;
    const expired = Date.now() >= Date.parse(invitation.expires_at);
    if (expired && invitation.state !== "approved") {
      this.#expire(invitation);
      return;
    }
    const generation = this.#generation;
    const state = invitation.state;
    const current = () => generation === this.#generation && this.#invitation === invitation
      && invitation.state === state && !this.#busy;
    this.#polling = invitation;
    try {
      const result = await this.#call("status", {
        invitation_id: invitation.invitation_id, display_key: invitation.key.publicKey,
      });
      if (!current() || result.state === state) return;
      const comparison = result.state === "awaiting_approval"
        ? await decrypt(invitation.key.privateKey, result.comparison_code_encrypted) : "";
      if (!current()) return;
      invitation.state = result.state;
      invitation.pairing_id = result.pairing_id;
      this.#element("code").textContent = "";
      this.#element("comparison").textContent = comparison;
      this.#element("compared").checked = false;
      if (!this.#stale) this.#notice("");
      this.#invitationState();
    } catch (error) {
      if (!current()) return;
      if (error?.code === "invitation_unavailable") {
        this.#run(async (generation) => {
          const phones = await this.#load(generation);
          if (generation !== this.#generation || this.#invitation !== invitation) return;
          this.#dropInvitation();
          if (phones.includes(invitation.pairing_id)) this.#notice("Téléphone associé.", "success");
          else this.#notice("Invitation terminée ou expirée. Créez une nouvelle invitation pour recommencer.", "error");
        }, "Impossible de vérifier le résultat de l’association. Nouvelle tentative en cours.");
      } else if (error?.code === "unauthorized") {
        this.#reset();
        this.#notice("Home Assistant a refusé la session d’administration HTTPS.", "error");
        this.#element("retry").hidden = false;
      } else {
        this.#notice("Impossible de vérifier l’état de l’invitation. Nouvelle tentative en cours.", "error");
      }
    } finally {
      if (this.#polling === invitation) this.#polling = undefined;
    }
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
        this.#notice("Invitation annulée. Vous pouvez en créer une nouvelle.");
      }
    }, "Impossible de traiter l’invitation. Vérifiez son état ou créez-en une nouvelle après expiration.");
  }

  #revoke(id, name) {
    this.#showDialog({
      title: `Révoquer ${name} ?`,
      description: "Le téléphone perdra son accès et les données de calendrier reçues de ce téléphone seront retirées. Pour le reconnecter, vous devrez refaire l’association.",
      button: "Révoquer le téléphone",
      destructive: true,
      action: () => this.#mutate("revoke", { pairing_id: id, confirmed: true }, {
        success: "L’accès du téléphone a été révoqué",
        rejected: ["confirmation_required", "phone_unavailable"],
        failure: "Home Assistant a refusé la révocation du téléphone.",
        unknown: "Le résultat de la révocation est indéterminé.",
      }),
    });
  }
}

customElements.define("horeveil-association", HoreveilAssociation);
