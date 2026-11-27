"""Opt-in browser regression for acknowledged, rejected and uncertain panel mutations."""

from argparse import ArgumentParser
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "horeveil"
ORIGIN = "https://panel.example"
REFRESH = "Actualiser la liste des téléphones"
FIXTURE = """
<!doctype html>
<meta charset="utf-8">
<style>
  body { margin: 0; font-family: system-ui, sans-serif; }
  horeveil-association { min-height: 100vh; }
  body.dark {
    --primary-color: #fff;
    --primary-color-text: #fff;
    --primary-background-color: #111418;
    --secondary-background-color: #252b32;
    --card-background-color: #1b2026;
    --primary-text-color: #fff;
    --secondary-text-color: #bfc8d2;
    --disabled-text-color: #a7b1bd;
    --divider-color: #3f4954;
    --error-color: #ffa3a3;
  }
  body.light {
    --primary-color: #206789;
    --primary-background-color: #edf2f6;
    --secondary-background-color: #e5eaef;
    --card-background-color: #fff;
    --primary-text-color: #202b35;
    --secondary-text-color: #455766;
    --disabled-text-color: #526170;
    --divider-color: #bac5cf;
    --ha-color-fill-primary-normal-resting: #206789;
    --ha-color-fill-primary-normal-hover: #174f6b;
    --ha-color-on-primary-normal: #fff;
  }
</style>
<horeveil-association></horeveil-association>
<script type="module">
  import "/pairing-panel.js";
  const icons = {
    "mdi:cellphone": "M8 2h8a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zM11 19h2",
    "mdi:qrcode-scan": "M3 9V3h6m6 0h6v6m0 6v6h-6m-6 0H3v-6m5-7h3v3H8zm5 0h3v3h-3zm-5 5h3v3H8zm5 0h3v3h-3z",
    "mdi:refresh": "M20 9a8 8 0 1 0 0 7m0-12v5h-5",
  };
  customElements.define("ha-icon", class extends HTMLElement {
    connectedCallback() {
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("width", "22");
      svg.setAttribute("height", "22");
      svg.style.display = "block";
      const path = document.createElementNS(svg.namespaceURI, "path");
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "currentColor");
      path.setAttribute("stroke-width", "2");
      path.setAttribute("d", icons[this.getAttribute("icon")] ?? "");
      svg.append(path);
      this.replaceChildren(svg);
    }
  });
  const panel = document.querySelector("horeveil-association");
  let source = null;
  let label = "Téléphone de recette";
  let phones = ["phone"];
  let failRead = false;
  let uncertain = null;
  window.recover = () => { uncertain = null; };
  window.mode = "acknowledged";
  window.writes = 0;
  const hass = { user: { id: "admin", is_admin: true }, config: { time_zone: "Europe/Paris" },
    async callWS({ type, pairing_id, confirmed, ...data }) {
    if (type.endsWith("/info")) {
      if (window.metadataFixture) {
        window.reads = (window.reads ?? 0) + 1;
        if (window.pauseRead) await new Promise(resolve => { window.resumeRead = resolve; });
        if (window.breakRead) throw Error("Connection lost");
        return {
          suggested_origin: "https://maison.example",
          https_origins: ["https://maison.example"],
          phones: ["complete", "partial", "missing"], calendar_phones: ["complete", "partial", "missing"],
          phone_labels: { complete: "Chambre", partial: "Bureau", missing: "Téléphone de secours" },
          phone_metadata: {
            complete: { manufacturer: "Samsung", model: "Samsung SM-S936B", app_version: "2.7.4", android_version: "17" },
            partial: { model: window.markupModel ? "<b>Pixel 8</b>" : "Pixel 8", app_version: "2.7.4" },
          },
          calendars: [{
            entry_id: "calendar", title: "Calendrier Horéveil", pairing_id: "complete",
            last_success: "2026-11-27T19:58:12Z",
          }, {
            entry_id: "test", title: "Calendrier sans réception", pairing_id: "partial", last_success: "invalide",
          }],
        };
      }
      if (failRead) {
        failRead = false;
        throw Error("Connection lost");
      }
      if (window.mode === "ownership") {
        return {
          phones: ["phone", "reserved"], calendar_phones: ["phone", "reserved"],
          phone_labels: { phone: label, reserved: "Téléphone réservé" },
          calendars: [{
            title: "Calendrier", entry_id: "calendar", pairing_id: "phone",
            source_conflict: !window.ownershipUnavailable,
            ownership_unavailable: Boolean(window.ownershipUnavailable),
            unavailable_phones: ["phone", "reserved"],
          }],
        };
      }
      return {
        phones: [...phones], calendar_phones: [...phones],
        phone_labels: uncertain === "rename" ? null : { phone: label },
        calendars: [{
          title: "Calendrier", entry_id: "calendar",
          pairing_id: uncertain === "calendar_source" ? null : source,
          source_uncertain: uncertain === "calendar_source",
        }],
      };
    }
    const operation = type.split("/").at(-1);
    if (window.mode === "rejected") {
      throw { code: operation === "rename" ? "rename_unavailable" : "calendar_source_unavailable" };
    }
    if (operation === "calendar_source") {
      if (!confirmed) throw Error("Missing confirmation");
      source = pairing_id;
    } else if (type.endsWith("/rename")) {
      label = data.label;
    } else if (type.endsWith("/revoke")) {
      if (!confirmed) throw Error("Missing confirmation");
      phones = [];
    }
    window.writes++;
    if (window.mode === "storage") {
      uncertain = operation;
      throw { code: operation === "revoke" ? "registry_unavailable" : "storage_outcome_unknown" };
    }
    if (window.mode === "disconnected") {
      panel.hass = { ...hass, connected: false };
      window.reconnect = () => { panel.hass = hass; };
      throw Error("Disconnected before receiving the response");
    }
    if (window.mode === "lost") throw Error("Response lost");
    failRead = window.mode === "acknowledged";
    return {};
  }};
  panel.hass = hass;
</script>
"""


def rename(page, old, new):
    page.get_by_role("button", name=f"Renommer {old}").click()
    page.get_by_label("Libellé du téléphone").fill(new)
    page.get_by_role("dialog").get_by_role("button", name="Enregistrer").click()


def route_panel(page):
    page.route(ORIGIN + "/", lambda route: route.fulfill(body=FIXTURE, content_type="text/html"))
    for name, file in (("pairing-panel.js", "pairing_panel.js"), ("pairing-qrcode.js", "pairing_qrcode.js")):
        page.route(ORIGIN + "/" + name, lambda route, _request, file=file: route.fulfill(
            body=(COMPONENT / file).read_text(encoding="utf-8"), content_type="text/javascript",
        ))


def contrast(button):
    def luminance(css):
        rgb = [int(value) / 255 for value in css.removeprefix("rgb(").removesuffix(")").split(", ")]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in rgb]
        return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    foreground, background = button.evaluate("""button => {
        const style = getComputedStyle(button);
        return [style.color, style.backgroundColor];
    }""")
    dark, light = sorted(map(luminance, (foreground, background)))
    return (light + 0.05) / (dark + 0.05)


def check_metadata_panel(browser, screenshots):
    for theme, width in (("dark", 1060), ("light", 560)):
        page = browser.new_page(viewport={"width": width, "height": 950}, timezone_id="America/New_York")
        route_panel(page)
        page.add_init_script(f"window.metadataFixture = true; window.addEventListener('DOMContentLoaded', "
                             f"() => document.body.className = '{theme}');")
        page.goto(ORIGIN)
        expect(page.get_by_text("Associé · Samsung SM-S936B · Horéveil 2.7.4 · Android 17", exact=True)).to_be_visible()
        expect(page.get_by_text("Associé · Pixel 8 · Horéveil 2.7.4", exact=True)).to_be_visible()
        expect(page.get_by_text("Associé · Modèle indisponible", exact=True)).to_be_visible()
        expect(page.get_by_text("Dernière réception du calendrier le 27/11/2026 à 20:58:12", exact=True)).to_be_visible()
        expect(page.get_by_text("Dernière réception réussie le 27/11/2026 à 20:58:12", exact=True)).to_be_visible()
        expect(page.get_by_text("Aucune réception pour cette source.", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Changer la source").first).to_be_disabled()
        refresh = page.get_by_role("button", name=REFRESH)
        expect(refresh).to_have_attribute("title", REFRESH)
        expect(page.locator("#phones-title + #refresh ha-icon")).to_have_attribute("icon", "mdi:refresh")
        create = page.get_by_role("button", name="Créer une invitation")
        expect(create).to_be_enabled()
        assert contrast(create) >= 4.5
        create.hover()
        assert contrast(create) >= 4.5
        page.locator("#origin").focus()
        page.keyboard.press("Tab")
        expect(create).to_be_focused()
        assert create.evaluate("button => getComputedStyle(button).outlineStyle") == "solid"
        assert contrast(create) >= 4.5
        if screenshots:
            create.evaluate("button => button.blur()")
            page.mouse.move(0, 0)
            page.screenshot(path=screenshots / f"horeveil-telephones-{theme}.png", full_page=True)

        page.evaluate("window.pauseRead = true; window.markupModel = true")
        refresh.focus()
        page.keyboard.press("Enter")
        expect(refresh).to_be_disabled()
        expect(create).to_be_disabled()
        assert contrast(create) >= 4.5
        page.evaluate("window.resumeRead()")
        expect(refresh).to_be_enabled()
        expect(page.get_by_text("Associé · <b>Pixel 8</b> · Horéveil 2.7.4", exact=True)).to_be_visible()
        expect(page.locator("#phones b")).to_have_count(0)
        assert page.evaluate("window.reads") == 2
        page.evaluate("window.pauseRead = false; window.breakRead = true")
        refresh.click()
        expect(page.locator("#notice")).to_contain_text("Impossible de charger le panneau")
        expect(refresh).to_be_enabled()
        page.close()


def main(channel, screenshots):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel)
        try:
            for mode in ("success", "acknowledged", "rejected", "lost", "disconnected", "storage", "ownership"):
                page = browser.new_page()
                route_panel(page)
                page.goto(ORIGIN)
                page.evaluate("(mode) => { window.mode = mode; }", mode)
                expect(page.get_by_text("Source active · Webhook local")).to_be_visible()
                if mode == "ownership":
                    refresh = page.get_by_role("button", name=REFRESH)
                    refresh.click()
                    expect(page.get_by_text("Ce téléphone est affecté à plusieurs calendriers.", exact=False)).to_be_visible()
                    expect(page.get_by_text("Source configurée · Téléphone de recette · HTTPS direct")).to_be_visible()
                    source = page.get_by_role("combobox", name="Nouvelle source pour Calendrier")
                    expect(source.locator('option[value="reserved"]')).to_have_count(0)
                    expect(source.locator('option[value=""]')).to_have_text("Webhook local")
                    page.evaluate("window.ownershipUnavailable = true")
                    refresh.click()
                    expect(page.get_by_text("Une entrée indisponible empêche de vérifier les affectations.", exact=False)).to_be_visible()
                    assert page.evaluate("window.writes") == 0
                    page.close()
                    continue
                page.get_by_role("combobox", name="Nouvelle source pour Calendrier").select_option("phone")
                page.get_by_role("button", name="Changer la source").click()
                dialog = page.get_by_role("dialog")
                expect(dialog.get_by_text("La projection actuelle sera effacée", exact=False)).to_be_visible()
                dialog.get_by_role("button", name="Changer la source").click()
                notice = page.locator("#notice")
                retry = page.get_by_role("button", name="Réessayer")
                source = page.get_by_text("Source active · Téléphone de recette · HTTPS direct")
                previous_source = page.get_by_text("Source active · Webhook local")
                previous_name = page.get_by_role("heading", name="Téléphone de recette")
                refresh = page.get_by_role("button", name=REFRESH)
                if mode == "rejected":
                    expect(notice).to_contain_text("a refusé le changement")
                    expect(previous_source).to_be_visible()
                    rename(page, "Téléphone de recette", "Chambre")
                    expect(notice).to_contain_text("a refusé le renommage")
                    expect(previous_name).to_be_visible()
                    assert page.evaluate("window.writes") == 0
                elif mode == "success":
                    expect(notice).to_have_text("La source du calendrier a été modifiée.")
                    expect(source).to_be_visible()
                    rename(page, "Téléphone de recette", "Chambre")
                    expect(notice).to_have_text("Le libellé du téléphone a été enregistré.")
                    expect(page.get_by_role("heading", name="Chambre")).to_be_visible()
                    assert page.evaluate("window.writes") == 2
                elif mode == "disconnected":
                    expect(notice).to_contain_text("est indéterminé")
                    expect(previous_source).to_be_hidden()
                    page.evaluate("window.reconnect()")
                    expect(notice).to_contain_text("Le panneau a été actualisé.")
                    expect(source).to_be_visible()
                    assert page.evaluate("window.writes") == 1
                elif mode == "storage":
                    expect(notice).to_contain_text("changement de source du calendrier est indéterminé")
                    expect(previous_source).to_be_hidden()
                    retry.click()
                    expect(page.get_by_text("Source indéterminée.", exact=False)).to_be_visible()
                    expect(previous_source).to_be_hidden()
                    expect(page.get_by_role("button", name="Changer la source")).to_be_hidden()
                    page.evaluate("window.recover()")
                    refresh.click()
                    expect(source).to_be_visible()
                    assert page.evaluate("window.writes") == 1
                    rename(page, "Téléphone de recette", "Chambre")
                    expect(notice).to_contain_text("résultat du renommage est indéterminé")
                    expect(previous_name).to_be_hidden()
                    retry.click()
                    expect(page.get_by_role("heading", name="Téléphone phone (libellé indisponible)")).to_be_visible()
                    expect(previous_name).to_be_hidden()
                    expect(page.get_by_role("button", name="Renommer", exact=False)).to_be_disabled()
                    page.evaluate("window.recover()")
                    refresh.click()
                    expect(page.get_by_role("heading", name="Chambre")).to_be_visible()
                    expect(page.get_by_text("Source active · Chambre · HTTPS direct")).to_be_visible()
                    assert page.evaluate("window.writes") == 2
                    page.get_by_role("button", name="Révoquer Chambre").click()
                    dialog.get_by_role("button", name="Révoquer le téléphone").click()
                    expect(notice).to_contain_text("résultat de la révocation est indéterminé")
                    expect(page.get_by_role("heading", name="Chambre")).to_be_hidden()
                    retry.click()
                    expect(page.get_by_text("Aucun téléphone associé.")).to_be_visible()
                    assert page.evaluate("window.writes") == 3
                else:
                    expected = ("a été modifiée, mais l’actualisation" if mode == "acknowledged"
                                else "est indéterminé")
                    expect(notice).to_contain_text(expected)
                    expect(previous_source).to_be_hidden()
                    retry.click()
                    expect(source).to_be_visible()
                    assert page.evaluate("window.writes") == 1
                if mode == "acknowledged":
                    rename(page, "Téléphone de recette", "Chambre")
                    expect(notice).to_contain_text("a été enregistré, mais l’actualisation")
                    retry.click()
                    expect(page.get_by_role("heading", name="Chambre")).to_be_visible()
                    page.get_by_role("button", name="Révoquer Chambre").click()
                    dialog.get_by_role("button", name="Révoquer le téléphone").click()
                    expect(notice).to_contain_text("a été révoqué, mais l’actualisation")
                    retry.click()
                    expect(page.get_by_text("Aucun téléphone associé.")).to_be_visible()
                    assert page.evaluate("window.writes") == 3
                page.close()
            check_metadata_panel(browser, screenshots)
        finally:
            browser.close()
    print("Nine browser scenarios passed, including phone metadata in light and white-accent dark themes.")


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--channel", help="Installed Chromium channel, e.g. chrome.")
    parser.add_argument("--screenshots", type=Path, help="Directory for the optional panel screenshots.")
    args = parser.parse_args()
    main(args.channel, args.screenshots)
