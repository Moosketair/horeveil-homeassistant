"""Opt-in browser regression for acknowledged, rejected and uncertain panel mutations."""

from argparse import ArgumentParser
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "horeveil"
ORIGIN = "https://panel.example"
FIXTURE = """
<!doctype html>
<meta charset="utf-8">
<horeveil-association></horeveil-association>
<script type="module">
  import "/pairing-panel.js";
  const panel = document.querySelector("horeveil-association");
  let source = null;
  let label = "Téléphone de recette";
  let phones = ["phone"];
  let failRead = false;
  let uncertain = null;
  window.recover = () => { uncertain = null; };
  window.mode = "acknowledged";
  window.writes = 0;
  const hass = { user: { id: "admin", is_admin: true }, async callWS({ type, pairing_id, confirmed, ...data }) {
    if (type.endsWith("/info")) {
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


def main(channel):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel)
        try:
            for mode in ("success", "acknowledged", "rejected", "lost", "disconnected", "storage", "ownership"):
                page = browser.new_page()
                page.route(ORIGIN + "/", lambda route: route.fulfill(body=FIXTURE, content_type="text/html"))
                for name, file in (("pairing-panel.js", "pairing_panel.js"), ("pairing-qrcode.js", "pairing_qrcode.js")):
                    page.route(ORIGIN + "/" + name, lambda route, _request, file=file: route.fulfill(
                        body=(COMPONENT / file).read_text(encoding="utf-8"), content_type="text/javascript",
                    ))
                page.goto(ORIGIN)
                page.evaluate("(mode) => { window.mode = mode; }", mode)
                expect(page.get_by_text("Source active · Webhook local")).to_be_visible()
                if mode == "ownership":
                    refresh = page.get_by_role("button", name="Actualiser la liste")
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
                refresh = page.get_by_role("button", name="Actualiser la liste")
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
        finally:
            browser.close()
    print("Seven browser scenarios passed, including ownership conflicts and unavailable entries.")


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--channel", help="Installed Chromium channel, e.g. chrome.")
    main(parser.parse_args().channel)
