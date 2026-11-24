"""Opt-in smoke test against a disposable, unmodified Home Assistant Core runtime."""

import asyncio
from argparse import ArgumentParser
from base64 import b64decode, urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import digest
from io import BytesIO
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
from secrets import token_bytes
from shutil import copytree
import socket
import ssl
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

from aiohttp import ClientConnectionError, ClientSession, TCPConnector, ThreadedResolver
from aiohttp.abc import AbstractResolver
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from homeassistant import bootstrap, loader
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.components.onboarding.const import STEPS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry
from homeassistant.helpers.storage import Store

_DOMAIN = "horeveil"
_BASE = "/api/horeveil/v1/pair"
_PANEL = "horeveil-association"
_SUGGESTED = "https://recette.example.invalid"
_ROOT = Path(__file__).resolve().parents[1]
_PUBLIC_NAME = "pairing.example.invalid"


def _certificate_files(profile, nginx, alternate_name=None):
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "CA Horéveil de recette")])

    def build(subject, key):
        return (
            x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        )

    ca = build(issuer, ca_key).add_extension(
        x509.BasicConstraints(ca=True, path_length=0), critical=True,
    ).add_extension(
        x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True,
    ).sign(ca_key, hashes.SHA256())
    (profile / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))

    def write(name, purpose, dns=None):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, name)])
        names = [x509.IPAddress(IPv4Address("127.0.0.1"))]
        if dns:
            names.append(x509.DNSName(dns))
        certificate = build(subject, key).add_extension(
            x509.ExtendedKeyUsage([purpose]), critical=False,
        ).add_extension(
            x509.SubjectAlternativeName(names), critical=False,
        ).sign(ca_key, hashes.SHA256())
        (profile / f"{name}.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path = profile / f"{name}.key"
        key_path.touch(mode=0o600)
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        return certificate.fingerprint(hashes.SHA256()).hex()

    write("serveur", x509.ExtendedKeyUsageOID.SERVER_AUTH, "horeveil-gateway.example.invalid")
    admin = write("client", x509.ExtendedKeyUsageOID.CLIENT_AUTH)
    if not nginx:
        return admin, None
    write("public", x509.ExtendedKeyUsageOID.SERVER_AUTH, _PUBLIC_NAME)
    if alternate_name:
        write("alternate", x509.ExtendedKeyUsageOID.SERVER_AUTH, alternate_name)
    return admin, write("proxy", x509.ExtendedKeyUsageOID.CLIENT_AUTH)


def _b64(data):
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode_code(ciphertext, key):
    return key.decrypt(
        urlsafe_b64decode(ciphertext + "=" * (-len(ciphertext) % 4)),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    ).decode("ascii")


def _comparison(reservation, claim_id, secret):
    message = "|".join((
        "horeveil-pair-v1", reservation["server_id"], reservation["pairing_id"],
        claim_id, reservation["challenge"],
    ))
    value = int.from_bytes(digest(sha256(secret).digest(), message.encode("ascii"), "sha256")[:5], "big")
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "".join(alphabet[(value >> shift) & 31] for shift in range(35, -1, -5))


async def _authenticate(ws, token):
    assert (await ws.receive_json())["type"] == "auth_required"
    await ws.send_json({"type": "auth", "access_token": token})
    return (await ws.receive_json())["type"]


async def _ws(ws, message_type, **fields):
    # The native socket requires monotonically increasing message IDs.
    _ws.message_id += 1
    message_id = _ws.message_id
    await ws.send_json({"id": message_id, "type": message_type, **fields})
    while (result := await ws.receive_json(timeout=10)).get("id") != message_id:
        pass
    return result


_ws.message_id = 0


async def _pair(ws, action, **fields):
    return await _ws(ws, "horeveil/pairing_test/" + action, **fields)


async def _token(hass, name, origin, *, administrator):
    user = await hass.auth.async_create_user(name, group_ids=[] if administrator else [GROUP_ID_USER])
    assert user.is_admin is administrator
    refresh = await hass.auth.async_create_refresh_token(user, client_id=origin + "/")
    return {
        "access_token": hass.auth.async_create_access_token(refresh),
        "refresh_token": refresh.token,
        "token_type": "Bearer",
        "expires_in": 1800,
    }


async def _test_api(hass, profile, origin, admin, guest):
    tls = ssl.create_default_context(cafile=profile / "ca.pem")
    async with ClientSession(connector=TCPConnector(ssl=tls, resolver=ThreadedResolver())) as anonymous:
        try:
            await anonymous.get(origin + _BASE + "/status")
        except ClientConnectionError:
            pass
        else:
            raise AssertionError("TLS accepted a client without certificate")
    tls.load_cert_chain(profile / "client.pem", profile / "client.key")
    async with ClientSession(connector=TCPConnector(ssl=tls, resolver=ThreadedResolver())) as client:
        async with client.ws_connect(origin + "/api/websocket") as ws:
            assert await _authenticate(ws, "invalid") == "auth_invalid"
        async with client.ws_connect(origin + "/api/websocket") as ws:
            assert await _authenticate(ws, guest["access_token"]) == "auth_ok"
            assert _PANEL not in (await _ws(ws, "get_panels"))["result"]
            for action in ("info", "invite", "status", "approve", "cancel", "revoke"):
                fields = {
                    "invite": {"origin": origin},
                    "status": {"invitation_id": str(uuid4())},
                    "approve": {"invitation_id": str(uuid4()), "pairing_id": str(uuid4()), "confirmed": True},
                    "cancel": {"invitation_id": str(uuid4())},
                    "revoke": {"pairing_id": str(uuid4()), "confirmed": True},
                }
                assert not (await _pair(ws, action, **fields.get(action, {})))["success"]
        async with client.get(origin + _BASE + "/status", headers={"X-Forwarded-Proto": "https"}) as response:
            assert response.status == 404
        async with client.ws_connect(origin + "/api/websocket") as ws:
            assert await _authenticate(ws, admin["access_token"]) == "auth_ok"
            panels = (await _ws(ws, "get_panels"))["result"]
            assert panels[_PANEL]["config_panel_domain"] == _DOMAIN
            assert panels[_PANEL]["require_admin"]
            module_url = panels[_PANEL]["config"]["_panel_custom"]["module_url"]
            async with client.get(origin + module_url) as response:
                assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
            assert (await _pair(ws, "info"))["result"]["suggested_origin"] == _SUGGESTED
            assert not (await _pair(ws, "invite", origin=_SUGGESTED))["success"]
            skipped = (await _pair(ws, "invite", origin=origin))["result"]
            assert (await _pair(ws, "cancel", invitation_id=skipped["invitation_id"]))["success"]
            expiring = (await _pair(ws, "invite", origin=origin))["result"]
            expiry = datetime.fromisoformat(expiring["expires_at"])
            assert timedelta(minutes=4, seconds=55) < expiry - datetime.now(UTC) <= timedelta(minutes=5)
            registry = hass.data["horeveil.local_pairing_test"].registry
            with patch.object(registry, "_clock", lambda: monotonic() + 300):
                assert not (await _pair(ws, "status", invitation_id=expiring["invitation_id"]))["success"]
            key = serialization.load_pem_private_key((profile / "client.key").read_bytes(), password=None)
            display = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            display_key = _b64(display.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            invite = (await _pair(ws, "invite", origin=origin, display_key=display_key))["result"]
            assert "code" not in invite
            try:
                _decode_code(invite["code_encrypted"], key)
            except ValueError:
                pass
            else:
                raise AssertionError("Browser code decryptable with TLS certificate key")
            code = _decode_code(invite["code_encrypted"], display)
            secret = token_bytes(32)
            payload = {
                "claim_id": str(uuid4()), "installation_id": str(uuid4()), "phone_label": "Recette",
                "credential_digest": _b64(sha256(secret).digest()),
            }
            async with client.post(
                origin + _BASE + "/claim", headers={"X-Horeveil-Invitation": code}, json=payload,
            ) as response:
                assert response.status == 200
                reservation = await response.json()
            pairing_id = reservation["pairing_id"]
            auth = {"X-Horeveil-Authorization": f"v1 {pairing_id}.{_b64(secret)}"}
            confirm = {"confirmed": True}
            async with client.post(origin + _BASE + "/activate", headers=auth, json=confirm) as response:
                assert response.status == 403
            ids = {"invitation_id": invite["invitation_id"], "pairing_id": pairing_id}
            status = (await _pair(ws, "status", invitation_id=invite["invitation_id"], display_key=display_key))["result"]
            assert status["state"] == "awaiting_approval"
            assert _decode_code(status["comparison_code_encrypted"], display) == _comparison(
                reservation, payload["claim_id"], secret,
            )
            assert (await _pair(ws, "approve", **ids, **confirm))["success"]
            async with client.post(origin + _BASE + "/activate", headers=auth, json=confirm) as response:
                assert (await response.json())["state"] == "active"
            assert (await _pair(ws, "info"))["result"]["phones"] == [pairing_id]
            association = (await Store(hass, 1, "horeveil.pairings").async_load())["associations"][0]
            for field in ("ha_permissions", "phone_permissions"):
                assert association[field] == {"consultation": [], "control": []}
            assert (await _pair(ws, "revoke", pairing_id=pairing_id, **confirm))["success"]
            assert (await _pair(ws, "info"))["result"]["phones"] == []
            async with client.get(origin + _BASE + "/status", headers=auth) as response:
                assert response.status == 401


class _LoopbackResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        assert host == _PUBLIC_NAME
        return [{"hostname": host, "host": "127.0.0.1", "port": port,
                 "family": socket.AF_INET, "proto": socket.IPPROTO_TCP, "flags": 0}]

    async def close(self):
        pass


async def _test_proxy(hass, profile, origin, public, backend_port, nginx, admin, guest, browser_test=None):
    port = int(public.rsplit(":", 1)[1])
    template = (_ROOT / "docs" / "nginx-horeveil-pairing.conf").read_text("utf-8")
    vhost = template.replace("listen 443 ssl;", f"listen 127.0.0.1:{port} ssl;").replace(
        "/chemin/prive/", profile.as_posix() + "/",
    ).replace("127.0.0.1:18443", f"127.0.0.1:{backend_port}").replace(
        "https://pairing.example.invalid;", public + ";",
    )
    (profile / "nginx.conf").write_text(
        "daemon off; master_process off; events {}\nhttp {\n" + vhost + "\n}", encoding="utf-8",
    )
    (profile / "logs").mkdir()
    (profile / "temp").mkdir()
    process = await asyncio.create_subprocess_exec(
        str(nginx.resolve()), "-p", profile.as_posix() + "/", "-c", "nginx.conf",
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        tls = ssl.create_default_context(cafile=profile / "ca.pem")
        async with ClientSession(connector=TCPConnector(ssl=tls, resolver=_LoopbackResolver())) as phone:
            test_url = public + _BASE
            for _ in range(20):
                try:
                    async with phone.get(test_url + "/status", allow_redirects=False) as response:
                        assert response.status == 404
                    break
                except ClientConnectionError:
                    if process.returncode is not None:
                        raise RuntimeError((await process.stderr.read()).decode(errors="replace"))
                    await asyncio.sleep(0.1)
            else:
                raise AssertionError("Nginx did not start")
            admin_tls = ssl.create_default_context(cafile=profile / "ca.pem")
            admin_tls.load_cert_chain(profile / "client.pem", profile / "client.key")
            async with ClientSession(connector=TCPConnector(ssl=admin_tls, resolver=ThreadedResolver())) as local:
                async with local.ws_connect(origin + "/api/websocket") as ws:
                    assert await _authenticate(ws, guest["access_token"]) == "auth_ok"
                    assert not (await _pair(ws, "direct_access", enabled=True, confirmed=True, origin=public))["success"]
                async with local.ws_connect(origin + "/api/websocket") as ws:
                    assert await _authenticate(ws, admin["access_token"]) == "auth_ok"
                    assert not (await _pair(ws, "direct_access", enabled=True, confirmed=False, origin=public))["success"]
                    assert not (await _pair(ws, "invite", origin=public))["success"]
                    assert (await _pair(ws, "direct_access", enabled=True, confirmed=True, origin=public))["success"]
                    async with phone.get(test_url + "/status", allow_redirects=False) as response:
                        assert response.status == 401
                    spoofed = {
                        "X-Forwarded-For": "192.0.2.22", "X-Horeveil-Proxy-Scheme": "http",
                        "X-Horeveil-Proxy-Origin": "https://incorrect.example.invalid",
                    }
                    async with phone.get(test_url + "/status", headers=spoofed) as response:
                        assert response.status == 401
                    async with phone.get(test_url + "/status", headers={"Host": "wrong.example.invalid"}) as response:
                        assert response.status == 421
                    for path in (
                        "/api/webhook/test", "/api/websocket", "/api/horeveil/v1/calendar/snapshot",
                        "/api/horeveil/pairing-qrcode.js",
                        _BASE + "/status?unexpected=1",
                    ):
                        async with phone.get(public + path, allow_redirects=False) as response:
                            assert response.status == 404 and "Location" not in response.headers
                    async with phone.get(test_url.replace("https:", "http:") + "/status", allow_redirects=False) as response:
                        assert response.status == 400 and "Location" not in response.headers
                    metadata = {
                        "X-Horeveil-Proxy-Scheme": "https", "X-Horeveil-Proxy-Origin": public,
                        "X-Horeveil-Proxy-Client-IP": "192.0.2.5",
                    }
                    backend_status = f"https://127.0.0.1:{backend_port}" + _BASE + "/status"
                    async with local.get(backend_status, headers=metadata) as response:
                        assert response.status == 404
                    proxy_tls = ssl.create_default_context(cafile=profile / "ca.pem")
                    proxy_tls.load_cert_chain(profile / "proxy.pem", profile / "proxy.key")
                    async with ClientSession(connector=TCPConnector(ssl=proxy_tls, resolver=ThreadedResolver())) as proxy:
                        insecure = {**metadata, "X-Horeveil-Proxy-Scheme": "http"}
                        async with proxy.get(backend_status, headers=insecure) as response:
                            assert response.status == 404
                    key = serialization.load_pem_private_key((profile / "client.key").read_bytes(), password=None)

                    async def invite(target):
                        result = (await _pair(ws, "invite", origin=target))["result"]
                        return result["invitation_id"], _decode_code(result["code_encrypted"], key)

                    secret = token_bytes(32)
                    body = {
                        "claim_id": str(uuid4()), "installation_id": str(uuid4()),
                        "phone_label": "Recette Nginx", "credential_digest": _b64(sha256(secret).digest()),
                    }

                    async def claim(code, expected):
                        async with phone.post(
                            test_url + "/claim", json=body, headers={"X-Horeveil-Invitation": code},
                        ) as response:
                            assert response.status == expected
                            return await response.json()

                    await claim("0" * 26, 401)
                    local_id, local_code = await invite(origin)
                    await claim(local_code, 401)
                    assert (await _pair(ws, "cancel", invitation_id=local_id))["success"]
                    _, expired = await invite(public)
                    registry = hass.data["horeveil.local_pairing_test"].registry
                    with patch.object(registry, "_clock", lambda: monotonic() + 300):
                        await claim(expired, 401)
                    invitation_id, code = await invite(public)
                    reserved = await claim(code, 200)
                    assert await claim(code, 200) == reserved
                    pairing_id = reserved["pairing_id"]
                    credential = {"X-Horeveil-Authorization": f"v1 {pairing_id}.{_b64(secret)}"}
                    confirmed = {"confirmed": True}
                    async with phone.post(test_url + "/activate", headers=credential, json=confirmed) as response:
                        assert response.status == 403
                    assert (await _pair(ws, "approve", invitation_id=invitation_id, pairing_id=pairing_id,
                                        **confirmed))["success"]
                    for _ in range(2):
                        async with phone.post(test_url + "/activate", headers=credential, json=confirmed) as response:
                            assert (await response.json())["state"] == "active"
                    async with local.get(origin + _BASE + "/status", headers=credential) as response:
                        assert response.status == 401
                    assert (await _pair(ws, "revoke", pairing_id=pairing_id, **confirmed))["success"]
                    async with phone.get(test_url + "/status", headers=credential) as response:
                        assert response.status == 401
                    assert (await _pair(ws, "direct_access", enabled=False, confirmed=True))["success"]
                    async with phone.get(test_url + "/status") as response:
                        assert response.status == 404
        print("Nginx réel : TLS, opt-in, en-têtes, association et révocation OK.", flush=True)
        if browser_test is not None:
            await browser_test()
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()


async def _browser_qr(panel, origin):
    import zxingcpp
    from PIL import Image

    canvas = panel.locator("#qr-canvas")
    png = b64decode((await canvas.evaluate("(canvas) => canvas.toDataURL()")).partition(",")[2])
    qr = zxingcpp.read_barcode(Image.open(BytesIO(png)), formats=zxingcpp.BarcodeFormat.QRCode)
    assert qr is not None
    payload = json.loads(qr.text)
    manual_code = await panel.locator("#code").text_content()
    expiration = await panel.locator("#expires").get_attribute("datetime")
    assert await panel.locator("#invitation-origin").text_content() == origin
    assert type(payload["v"]) is int
    assert payload == {"v": 1, "origin": origin, "code": manual_code, "expires_at": expiration}
    assert 280 < (datetime.fromisoformat(expiration) - datetime.now(UTC)).total_seconds() <= 300
    classpath = os.environ.get("HOREVEIL_ANDROID_QR_CLASSPATH")
    if classpath:
        java = os.environ.get("HOREVEIL_ANDROID_QR_JAVA", "java")
        process = await asyncio.create_subprocess_exec(
            java, "-classpath", classpath, "fr.moosketair.horeveil.platform.homeassistant.AndroidPairingQrProbeKt",
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await process.communicate(png)
        assert process.returncode == 0, "ZXing Java or the Android parser rejected the QR"
    return payload["code"]


async def _test_browser_qr(hass, panel, profile, origin):
    from playwright.async_api import expect

    await panel.locator("#origin").fill(origin)
    await expect(panel.locator("#host")).to_have_text(origin.removeprefix("https://"))
    await expect(panel.locator("#direct-toggle")).to_be_disabled()
    await panel.locator("#direct-confirm").check()
    await panel.locator("#direct-toggle").click()
    await expect(panel.locator("#direct-state")).to_contain_text(origin)
    await expect(panel.locator("#create")).to_be_enabled()
    await panel.locator("#create").click()
    await expect(panel.locator("#qr")).to_be_visible()
    original = await _browser_qr(panel, origin)
    await panel.locator("#regenerate").click()
    await expect(panel.locator("#regenerate")).to_be_enabled()
    replacement = await _browser_qr(panel, origin)
    assert original != replacement
    secret = token_bytes(32)
    claim = {
        "claim_id": str(uuid4()), "installation_id": str(uuid4()), "phone_label": "Recette QR",
        "credential_digest": _b64(sha256(secret).digest()),
    }
    tls = ssl.create_default_context(cafile=profile / "ca.pem")
    async with ClientSession(connector=TCPConnector(ssl=tls, resolver=_LoopbackResolver())) as phone:
        async def send(code):
            return await phone.post(
                origin + _BASE + "/claim", headers={"X-Horeveil-Invitation": code}, json=claim,
            )

        async with await send(original) as response:
            assert response.status == 401
        await panel.locator("#cancel").click()
        await expect(panel.locator("#notice")).to_have_text("Invitation annulée.")
        await expect(panel.locator("#qr")).to_be_hidden()
        assert await panel.locator("#qr-canvas").evaluate("(canvas) => canvas.width") == 0
        async with await send(replacement) as response:
            assert response.status == 401
        await panel.locator("#create").click()
        await expect(panel.locator("#qr")).to_be_visible()
        expired = await _browser_qr(panel, origin)
        registry = hass.data["horeveil.local_pairing_test"].registry
        with patch.object(registry, "_clock", lambda: monotonic() + 300):
            async with await send(expired) as response:
                assert response.status == 401
            await expect(panel.locator("#qr")).to_be_hidden()
        assert await panel.locator("#qr-canvas").evaluate("(canvas) => canvas.width") == 0
        await panel.locator("#create").click()
        await expect(panel.locator("#qr")).to_be_visible()
        code = await _browser_qr(panel, origin)
        async with await send(code) as response:
            assert response.status == 200
        await expect(panel.locator("#review")).to_be_visible()
        await expect(panel.locator("#approve")).to_be_disabled()
        await expect(panel.locator("#qr")).to_be_hidden()
        assert await panel.locator("#qr-canvas").evaluate("(canvas) => canvas.width") == 0
        await panel.locator("#cancel").click()
        await expect(panel.locator("#notice")).to_have_text("Invitation annulée.")
    await panel.locator("#direct-toggle").click()
    await expect(panel.locator("#direct-state")).to_have_text("Accès direct désactivé")


async def _test_browser(hass, profile, origin, admin, channel, component, direct_origin=None, *, native=False):
    from playwright.async_api import async_playwright, expect

    flow = await hass.config_entries.flow.async_init(_DOMAIN, context={"source": "user"})
    entry = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    assert entry["type"] == "create_entry"
    await hass.async_block_till_done()
    entities = entity_registry.async_entries_for_config_entry(
        entity_registry.async_get(hass), entry["result"].entry_id,
    )
    assert sorted(entity.domain for entity in entities) == ["calendar", "sensor", "sensor", "sensor", "sensor"]

    with patch.dict(os.environ, {"NODE_EXTRA_CA_CERTS": str(profile / "ca.pem")}):
        async with async_playwright() as playwright:
            # Playwright terminates browser-side TLS with a temporary certificate.
            # The independent HTTP/WS checks above still validate the Core chain.
            browser = await playwright.chromium.launch(
                channel=channel, args=["--ignore-certificate-errors"],
            )
            context = await browser.new_context(
                client_certificates=[] if native else [{
                    "origin": origin,
                    "certPath": profile / "client.pem",
                    "keyPath": profile / "client.key",
                }],
            )
            try:
                async def keep_local(route):
                    if route.request.url.startswith(origin + "/"):
                        await route.continue_()
                    else:
                        await route.abort()

                await context.route("**/*", keep_local)
                tokens = {
                    **admin,
                    "clientId": origin + "/",
                    "hassUrl": origin,
                    "expires": int(datetime.now(UTC).timestamp() * 1000) + 1_800_000,
                }
                page = await context.new_page()
                await page.goto(origin + "/robots.txt")
                await page.evaluate("(tokens) => localStorage.setItem('hassTokens', JSON.stringify(tokens))", tokens)
                if component:
                    await page.goto(origin + "/" + _PANEL)
                    await page.wait_for_function("""(name) => {
                      const hass = document.querySelector('home-assistant')?.hass;
                      return hass?.user?.is_admin && hass?.panels?.[name] && typeof hass.callWS === 'function';
                    }""", arg=_PANEL)
                    await page.evaluate("""async (name) => {
                      const hass = document.querySelector('home-assistant').hass;
                      const module = hass.panels[name].config._panel_custom.module_url;
                      await import(module);
                      if (document.querySelector(name)) return;
                      const host = document.createElement('div');
                      host.style.cssText = 'position:fixed;inset:0;overflow:auto;background:white;z-index:2147483647';
                      const panel = document.createElement(name);
                      document.body.append(host);
                      host.append(panel);
                      panel.hass = hass;
                    }""", _PANEL)
                else:
                    await page.goto(origin + "/config/integrations/integration/horeveil")
                    link = page.locator(f'ha-config-entry-row ha-icon-button[href^="/{_PANEL}?"]').first
                    await link.click(timeout=15_000)
                panel = page.locator(_PANEL)
                initial_origin = direct_origin if native else _SUGGESTED
                await expect(panel.locator("#origin")).to_have_value(initial_origin)
                await expect(panel.locator("#host")).to_have_text(initial_origin.removeprefix("https://"))
                await expect(panel.locator("#create")).to_be_disabled()
                if direct_origin:
                    await _test_browser_qr(hass, panel, profile, direct_origin)
                if native:
                    return
                await panel.locator("#origin").fill(origin)
                await expect(panel.locator("#create")).to_be_enabled()
                await expect(panel.locator("input[value=read]")).to_be_checked()
                assert await panel.locator("fieldset input:checked").count() == 0
                await expect(panel.locator("#qr")).to_be_hidden()
                assert await panel.locator("#qr-canvas").evaluate("(canvas) => canvas.width") == 0

                async def create():
                    await panel.locator("#create").click()
                    await expect(panel.locator("#manual")).to_be_visible()

                await create()
                await expect(panel.locator("#qr-unavailable")).to_be_visible()
                await panel.locator("#cancel").click()
                await expect(panel.locator("#notice")).to_have_text("Invitation annulée.")
                await create()
                registry = hass.data["horeveil.local_pairing_test"].registry
                with patch.object(registry, "_clock", lambda: monotonic() + 300):
                    await expect(panel.locator("#notice")).to_have_text("Invitation indisponible.")
                await expect(panel.locator("#manual")).to_be_hidden()
                await create()
                code = await panel.locator("#code").text_content()
                secret = token_bytes(32)
                payload = {
                    "claim_id": str(uuid4()), "installation_id": str(uuid4()), "phone_label": "Recette",
                    "credential_digest": _b64(sha256(secret).digest()),
                }
                tls = ssl.create_default_context(cafile=profile / "ca.pem")
                tls.load_cert_chain(profile / "client.pem", profile / "client.key")
                async with ClientSession(connector=TCPConnector(ssl=tls, resolver=ThreadedResolver())) as phone:
                    async with phone.post(
                        origin + _BASE + "/claim", json=payload, headers={"X-Horeveil-Invitation": code},
                    ) as response:
                        assert response.status == 200
                        reserved = await response.json()
                    await expect(panel.locator("#review")).to_be_visible()
                    assert await panel.locator("#comparison").text_content() == _comparison(
                        reserved, payload["claim_id"], secret,
                    )
                    await expect(panel.locator("#approve")).to_be_disabled()
                    await panel.locator("#compared").check()
                    await panel.locator("#approve").click()
                    await expect(panel.locator("#state")).to_contain_text("Approuvée")
                    pairing_id = reserved["pairing_id"]
                    headers = {"X-Horeveil-Authorization": f"v1 {pairing_id}.{_b64(secret)}"}
                    async with phone.post(
                        origin + _BASE + "/activate", json={"confirmed": True}, headers=headers,
                    ) as response:
                        assert (await response.json())["state"] == "active"
                    await expect(panel.locator("#phones")).to_contain_text(pairing_id)
                    page.once("dialog", lambda dialog: dialog.accept())
                    await panel.locator("#phones button").click()
                    await expect(panel.locator("#notice")).to_have_text("Accès révoqué.")
                    async with phone.get(origin + _BASE + "/status", headers=headers) as response:
                        assert response.status == 401
            finally:
                await browser.close()


def _free_port():
    with socket.socket() as port_picker:
        port_picker.bind(("127.0.0.1", 0))
        return port_picker.getsockname()[1]


async def main(browser, channel, component, nginx):
    with TemporaryDirectory(prefix="horeveil-ha-") as directory:
        profile = Path(directory)
        copytree(_ROOT / "custom_components" / _DOMAIN, profile / "custom_components" / _DOMAIN)
        fingerprint, proxy_fingerprint = _certificate_files(profile, nginx)
        port = _free_port()
        origin = f"https://127.0.0.1:{port}"
        hass = HomeAssistant(directory)
        loader.async_setup(hass)
        await Store(hass, 4, "onboarding", private=True).async_save({"done": list(STEPS)})
        config = {
            "homeassistant": {"external_url": _SUGGESTED, "internal_url": origin},
            "http": {
                "server_host": ["127.0.0.1"], "server_port": port,
                "ssl_certificate": str(profile / "serveur.pem"), "ssl_key": str(profile / "serveur.key"),
                "ssl_peer_certificate": str(profile / "ca.pem"),
            },
            "frontend": {}, "config": {}, "lovelace": {}, "repairs": {}, "recorder": {},
            _DOMAIN: {"local_pairing_test_client_sha256": fingerprint},
        }
        if nginx:
            backend_port = _free_port()
            direct_origin = f"https://{_PUBLIC_NAME}:{_free_port()}"
            config[_DOMAIN]["direct_pairing"] = {
                "port": backend_port, "certificate": str(profile / "serveur.pem"),
                "key": str(profile / "serveur.key"), "client_ca": str(profile / "ca.pem"),
                "admin_certificate_sha256": fingerprint, "proxy_certificate_sha256": proxy_fingerprint,
            }
        try:
            assert await bootstrap.async_from_config_dict(config, hass) is hass
            await hass.async_start()
            admin = await _token(hass, "Administrateur de recette", origin, administrator=True)
            guest = await _token(hass, "Utilisateur de recette", origin, administrator=False)
            await _test_api(hass, profile, origin, admin, guest)
            print("Core natif : panneau, authentification, invitations, origine, permissions et révocation OK.", flush=True)
            async def run_browser():
                await _test_browser(hass, profile, origin, admin, channel, component, direct_origin if nginx else None)
                path = "composant monté avec la session HA réelle" if component else "ouverture depuis la fiche Horéveil"
                print(f"Frontend HA et navigateur : {path}, association et révocation OK.")
                if nginx:
                    print("QR décodé dans Chrome, régénération, annulation, expiration et réservation via Nginx OK.")
                    if os.environ.get("HOREVEIL_ANDROID_QR_CLASSPATH"):
                        print("Le parseur du commit Android a accepté le QR décodé par ZXing Java.")

            if nginx:
                await _test_proxy(
                    hass, profile, origin, direct_origin, backend_port, nginx, admin, guest,
                    browser_test=run_browser if browser else None,
                )
            elif browser:
                await run_browser()
        finally:
            await hass.async_stop(force=True)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true", help="Exercise the installed HA frontend with Playwright.")
    parser.add_argument("--channel", help="Optional installed browser channel, e.g. chrome.")
    parser.add_argument("--nginx", type=Path, help="Exercise the documented vhost with this nginx executable.")
    parser.add_argument(
        "--component", action="store_true",
        help="Mount the real component in the authenticated frontend without testing its router.",
    )
    arguments = parser.parse_args()
    if arguments.component and not arguments.browser:
        parser.error("--component requires --browser")
    asyncio.run(main(arguments.browser, arguments.channel, arguments.component, arguments.nginx))
