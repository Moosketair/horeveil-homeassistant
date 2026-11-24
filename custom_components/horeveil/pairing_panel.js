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
    fieldset { margin:12px 0; border:1px solid var(--divider-color); }
    .muted { color:var(--secondary-text-color); }
    [hidden] { display:none !important; }
  </style>
  <main>
    <h1>Association Horéveil</h1>
    <p id="notice" role="status">Chargement de la recette locale…</p>
    <div id="content" hidden>
      <section aria-labelledby="add-title">
        <h2 id="add-title">Ajouter un téléphone</h2>
        <form id="address">
          <label for="origin">Adresse HTTPS de Home Assistant</label>
          <input id="origin" type="url" required autocomplete="off" spellcheck="false">
          <p>Domaine ou hôte <output id="host" for="origin">—</output></p>
          <p class="muted">L’adresse Internet configurée dans HA est une suggestion. Son accessibilité depuis le téléphone n’est pas vérifiée.</p>
          <button id="local" type="button">Utiliser l’adresse de recette</button>
          <button id="create" type="submit" disabled>Confirmer et créer l’invitation</button>
        </form>
        <p id="availability"></p>
        <p class="muted">QR indisponible dans ce mode. Les routes sont limitées à cet hôte, où un simulateur peut utiliser le code manuel.</p>
        <div id="invitation" hidden>
          <p>État <strong id="state"></strong></p>
          <p>Expiration <time id="expires"></time></p>
          <p id="manual">Adresse <output id="invitation-origin"></output><br>Code manuel <code id="code"></code></p>
          <div id="review" hidden>
            <p>Code de sécurité <code id="comparison"></code></p>
            <label><input id="compared" type="checkbox"> J’ai comparé le code avec celui du simulateur.</label>
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
  #invitation;
  #timer;

  constructor() {
    super();
    this.attachShadow({ mode: "open" }).innerHTML = template;
    this.#element("address").addEventListener("submit", (event) => {
      event.preventDefault();
      this.#create();
    });
    this.#element("origin").addEventListener("input", () => this.#address());
    this.#element("local").onclick = () => {
      this.#element("origin").value = this.#testOrigin;
      this.#address();
    };
    this.#element("compared").onchange = () => this.#invitationState();
    this.#element("approve").onclick = () => this.#decide(true);
    this.#element("cancel").onclick = () => this.#decide(false);
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
    this.#dropInvitation();
    this.#element("content").hidden = true;
    this.#element("origin").value = "";
    this.#element("phones").replaceChildren();
    this.#notice("Accès réservé à un administrateur en mode de recette locale.");
  }

  #dropInvitation() {
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
    const usable = parsed && parsed.origin === this.#testOrigin;
    this.#element("host").textContent = parsed?.host ?? "—";
    this.#element("availability").textContent = usable
      ? "Adresse autorisée pour la recette sur cet hôte."
      : `Seule l’adresse de recette ${this.#testOrigin ?? ""} peut servir à créer une invitation.`;
    this.#element("create").disabled = !usable || this.#busy || Boolean(this.#invitation);
    return usable ? parsed.origin : null;
  }

  async #load(generation) {
    const info = await this.#call("info");
    if (generation !== this.#generation) return;
    if (!this.#testOrigin) this.#element("origin").value = info.suggested_origin;
    this.#testOrigin = info.test_origin;
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
    this.#notice("");
    return info.phones;
  }

  #create() {
    const origin = this.#address();
    if (!origin || this.#invitation) return;
    this.#run(async (generation) => {
      const key = await newDisplayKey();
      if (generation !== this.#generation) return;
      const result = await this.#call("invite", { origin, display_key: key.publicKey });
      if (generation !== this.#generation) return;
      const code = await decrypt(key.privateKey, result.code_encrypted);
      if (generation !== this.#generation) return;
      this.#invitation = { ...result, key, state: "issued" };
      this.#element("invitation-origin").textContent = result.origin;
      this.#element("code").textContent = code;
      this.#element("expires").dateTime = result.expires_at;
      this.#element("expires").textContent = new Date(result.expires_at).toLocaleString();
      this.#notice("");
    }, "Impossible de créer l’invitation. Une invitation peut déjà être ouverte.");
  }

  #invitationState() {
    const state = this.#invitation?.state;
    const reviewing = state === "awaiting_approval";
    this.#element("invitation").hidden = !state;
    this.#element("manual").hidden = state !== "issued";
    this.#element("review").hidden = !reviewing;
    this.#element("state").textContent = {
      issued: "En attente de demande", awaiting_approval: "À vérifier",
      approved: "Approuvée, en attente de confirmation du simulateur",
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
      this.#dropInvitation();
      this.#notice("Invitation expirée.");
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
