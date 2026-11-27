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
  window.mode = "acknowledged";
  window.writes = 0;
  const hass = { user: { id: "admin", is_admin: true }, async callWS({ type, pairing_id, confirmed, ...data }) {
    if (type.endsWith("/info")) {
      if (failRead) {
        failRead = false;
        throw Error("Connection lost");
      }
      return {
        phones: [...phones], calendar_phones: [...phones], phone_labels: { phone: label },
        calendars: [{ title: "Calendrier", entry_id: "calendar", pairing_id: source }],
      };
    }
    if (window.mode === "rejected") throw { code: "calendar_source_unavailable" };
    if (type.endsWith("/calendar_source")) {
      if (!confirmed) throw Error("Missing confirmation");
      source = pairing_id;
    } else if (type.endsWith("/rename")) {
      label = data.label;
    } else if (type.endsWith("/revoke")) {
      if (!confirmed) throw Error("Missing confirmation");
      phones = [];
    }
    window.writes++;
    if (window.mode === "disconnected") {
      panel.hass = { ...hass, connected: false };
      window.reconnect = () => { panel.hass = hass; };
      throw Error("Disconnected before receiving the response");
    }
    if (window.mode === "lost") throw Error("Response lost");
    failRead = true;
    return {};
  }};
  panel.hass = hass;
</script>
"""


def main(channel):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel)
        try:
            for mode in ("acknowledged", "rejected", "lost", "disconnected"):
                page = browser.new_page()
                page.route(ORIGIN + "/", lambda route: route.fulfill(body=FIXTURE, content_type="text/html"))
                for name, file in (("pairing-panel.js", "pairing_panel.js"), ("pairing-qrcode.js", "pairing_qrcode.js")):
                    page.route(ORIGIN + "/" + name, lambda route, _request, file=file: route.fulfill(
                        body=(COMPONENT / file).read_text(encoding="utf-8"), content_type="text/javascript",
                    ))
                page.goto(ORIGIN)
                page.evaluate("(mode) => { window.mode = mode; }", mode)
                expect(page.get_by_text("Source active · Webhook local")).to_be_visible()
                page.get_by_role("combobox", name="Nouvelle source pour Calendrier").select_option("phone")
                page.get_by_role("button", name="Changer la source").click()
                dialog = page.get_by_role("dialog")
                expect(dialog.get_by_text("La projection actuelle sera effacée", exact=False)).to_be_visible()
                dialog.get_by_role("button", name="Changer la source").click()
                notice = page.locator("#notice")
                retry = page.get_by_role("button", name="Réessayer")
                if mode == "rejected":
                    expect(notice).to_contain_text("a refusé le changement")
                    expect(page.get_by_text("Source active · Webhook local")).to_be_visible()
                    assert page.evaluate("window.writes") == 0
                elif mode == "disconnected":
                    expect(notice).to_contain_text("est indéterminé")
                    expect(page.get_by_text("Source active · Webhook local")).to_be_hidden()
                    page.evaluate("window.reconnect()")
                    expect(notice).to_contain_text("Le panneau a été actualisé.")
                    expect(page.get_by_text("Source active · Téléphone de recette · HTTPS direct")).to_be_visible()
                    assert page.evaluate("window.writes") == 1
                else:
                    expected = ("a été modifiée, mais l’actualisation" if mode == "acknowledged"
                                else "est indéterminé")
                    expect(notice).to_contain_text(expected)
                    expect(page.get_by_text("Source active · Webhook local")).to_be_hidden()
                    retry.click()
                    expect(page.get_by_text("Source active · Téléphone de recette · HTTPS direct")).to_be_visible()
                    assert page.evaluate("window.writes") == 1
                if mode == "acknowledged":
                    page.get_by_role("button", name="Renommer Téléphone de recette").click()
                    page.get_by_label("Libellé du téléphone").fill("Chambre")
                    dialog.get_by_role("button", name="Enregistrer").click()
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
    print("Panel mutation recovery: rejected, acknowledged, lost response, rename and revoke.")


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--channel", help="Installed Chromium channel, e.g. chrome.")
    main(parser.parse_args().channel)
