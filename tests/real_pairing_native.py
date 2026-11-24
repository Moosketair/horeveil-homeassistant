"""Run disposable Core and real Nginx with the NPM Advanced location rules."""

import asyncio
from argparse import ArgumentParser
from hashlib import sha256
from pathlib import Path
from secrets import token_bytes, token_hex
from shutil import copytree
import ssl
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

from aiohttp import ClientConnectionError, ClientSession, TCPConnector, ThreadedResolver
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from homeassistant import bootstrap, loader
from homeassistant.components.onboarding.const import STEPS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from real_pairing_smoke import (
    _BASE, _DOMAIN, _PANEL, _PUBLIC_NAME, _ROOT, _SUGGESTED, _LoopbackResolver,
    _authenticate, _b64, _certificate_files, _decode_code, _free_port, _pair,
    _test_browser, _token, _ws,
)


async def check_request(client, method, url, expected, **kwargs):
    async with client.request(method, url, allow_redirects=False, **kwargs) as response:
        assert response.status == expected, (method, response.status, expected)
        assert "Location" not in response.headers
        return await response.json() if response.content_type == "application/json" else None


async def test_api(local, phone, origin, public, cleartext, alternate, admin, guest, *, registry=None, boundary=None):
    status = _BASE + "/status"
    await check_request(phone, "GET", public + status, 404)
    async with local.ws_connect(alternate + "/api/websocket") as ws:
        assert await _authenticate(ws, "invalid") == "auth_invalid"
    async with local.ws_connect(alternate + "/api/websocket") as ws:
        assert await _authenticate(ws, guest["access_token"]) == "auth_ok"
        assert _PANEL not in (await _ws(ws, "get_panels"))["result"]
        assert not (await _pair(ws, "info"))["success"]
        assert not (await _pair(ws, "direct_access", enabled=True, confirmed=True, origin=public))["success"]
    async with local.ws_connect(alternate + "/api/websocket") as ws:
        assert await _authenticate(ws, admin["access_token"]) == "auth_ok"
        assert _PANEL in (await _ws(ws, "get_panels"))["result"]
        assert not (await _pair(ws, "direct_access", enabled=True, confirmed=True, origin=_SUGGESTED))["success"]
        assert not (await _pair(ws, "direct_access", enabled=True, confirmed=False, origin=public))["success"]
        assert (await _pair(ws, "direct_access", enabled=True, confirmed=True, origin=public))["success"]
        if boundary is not None:
            await boundary(origin, public)
        spoofed = {
            "X-Horeveil-Relay-Secret": token_hex(32), "X-Horeveil-Proxy-Origin": _SUGGESTED,
            "X-Forwarded-For": "172.18.0.1", "X-Forwarded-Proto": "http",
        }
        await check_request(phone, "GET", public + status, 401, headers=spoofed)
        await check_request(local, "GET", origin + status, 404)
        await check_request(local, "GET", origin + status, 404, headers=spoofed)
        await check_request(local, "GET", alternate + status, 404)
        await check_request(local, "GET", alternate + status, 404, headers={"Host": _PUBLIC_NAME})
        await check_request(local, "POST", alternate + "/api/webhook/unregistered", 404)
        await check_request(phone, "GET", cleartext + status, 404)
        await check_request(phone, "HEAD", public + status, 405)
        await check_request(phone, "OPTIONS", public + status, 405)
        await check_request(phone, "GET", public + status, 400, headers={"Content-Encoding": "gzip"})
        await check_request(phone, "POST", public + "/api/webhook/unregistered", 404)
        await check_request(phone, "GET", public + "/api/websocket", 404)
        await check_request(phone, "GET", public + status + "?ignored=1", 404)

        display = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        display_key = _b64(display.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
        phone_secret = token_bytes(32)
        body = {
            "claim_id": str(uuid4()), "installation_id": str(uuid4()), "phone_label": "Recette native",
            "credential_digest": _b64(sha256(phone_secret).digest()),
        }
        async def invite_phone():
            return (await _pair(ws, "invite", origin=public, display_key=display_key))["result"]

        assert not (await _pair(ws, "invite", origin=public))["success"]
        if registry is not None:
            expired = _decode_code((await invite_phone())["code_encrypted"], display)
            with patch.object(registry, "_clock", lambda: monotonic() + 301):
                await check_request(phone, "POST", public + _BASE + "/claim", 401, json=body,
                                    headers={"X-Horeveil-Invitation": expired})

        invite = await invite_phone()
        invitations = {"X-Horeveil-Invitation": _decode_code(invite["code_encrypted"], display)}
        async def claim():
            return await check_request(phone, "POST", public + _BASE + "/claim", 200, json=body, headers=invitations)

        first, duplicate = await asyncio.gather(claim(), claim())
        assert first == duplicate
        identifier = first["pairing_id"]
        auth = {"X-Horeveil-Authorization": f"v1 {identifier}.{_b64(phone_secret)}"}
        activate = public + _BASE + "/activate"
        await check_request(phone, "POST", activate, 403, json={"confirmed": True}, headers=auth)
        approval = {"invitation_id": invite["invitation_id"], "pairing_id": identifier, "confirmed": True}
        assert (await _pair(ws, "approve", **approval))["success"]
        assert (await check_request(phone, "GET", public + status, 200, headers=auth))["state"] == "approved"
        assert (await check_request(phone, "POST", activate, 200, json={"confirmed": True}, headers=auth))["state"] == "active"
        assert (await check_request(phone, "POST", activate, 200, json={"confirmed": True}, headers=auth))["state"] == "active"
        assert (await _pair(ws, "revoke", pairing_id=identifier, confirmed=True))["success"]
        await check_request(phone, "GET", public + status, 401, headers=auth)
        assert (await _pair(ws, "direct_access", enabled=False, confirmed=True))["success"]
        await check_request(phone, "GET", public + status, 404, headers=auth)


async def main(nginx, browser, component, channel):
    with TemporaryDirectory(prefix="horeveil-native-") as directory:
        profile = Path(directory)
        copytree(_ROOT / "custom_components" / _DOMAIN, profile / "custom_components" / _DOMAIN)
        _certificate_files(profile, nginx)
        port, public_port, clear_port = (_free_port() for _ in range(3))
        origin, public = f"https://127.0.0.1:{port}", f"https://{_PUBLIC_NAME}:{public_port}"
        alternate = f"https://127.0.0.1:{public_port}"
        cleartext = f"http://{_PUBLIC_NAME}:{clear_port}"
        relay_secret = token_hex(32)
        (profile / "relay.inc").write_text(f"proxy_set_header X-Horeveil-Relay-Secret {relay_secret};\n")
        config = {
            "homeassistant": {"external_url": _SUGGESTED, "internal_url": origin},
            "http": {
                "server_host": ["127.0.0.1"], "server_port": port,
                "ssl_certificate": str(profile / "serveur.pem"), "ssl_key": str(profile / "serveur.key"),
                "use_x_forwarded_for": True, "trusted_proxies": ["127.0.0.1"],
            },
            "frontend": {}, "config": {}, "lovelace": {}, "repairs": {}, "recorder": {},
            _DOMAIN: {"native_pairing": {
                "origin": public, "proxy_ip": "127.0.0.1", "upstream": "tls",
                "relay_secret_sha256": sha256(relay_secret.encode()).hexdigest(),
            }},
        }
        hass = HomeAssistant(directory)
        loader.async_setup(hass)
        await Store(hass, 4, "onboarding", private=True).async_save({"done": list(STEPS)})
        process = None
        try:
            assert await bootstrap.async_from_config_dict(config, hass) is hass
            await hass.async_start()
            admin = await _token(hass, "Administrateur natif", alternate, administrator=True)
            guest = await _token(hass, "Utilisateur natif", alternate, administrator=False)
            advanced = (_ROOT / "docs/npm-horeveil-pairing.conf").read_text("utf-8").replace(
                "https://pairing.example.invalid;", public + ";",
            ).replace("ha-interne.example.invalid", "horeveil-gateway.example.invalid").replace(
                "/data/nginx/custom/horeveil-upstream-ca.pem", f'"{profile.as_posix()}/ca.pem"',
            ).replace("/data/nginx/custom/horeveil-relay-secret.inc", "relay.inc").replace("/dev/null", "logs/private.log")
            config_text = f"""daemon off; master_process off; events {{}}
            http {{
              server_names_hash_bucket_size 64;
              server {{
                listen 127.0.0.1:{public_port} ssl default_server;
                server_name 127.0.0.1;
                ssl_certificate serveur.pem; ssl_certificate_key serveur.key;
                location ^~ /api/horeveil/v1/pair/ {{ return 404; }}
                location ^~ /api/webhook/ {{ return 404; }}
                location / {{
                  proxy_pass https://127.0.0.1:{port};
                  proxy_ssl_verify on;
                  proxy_ssl_name horeveil-gateway.example.invalid;
                  proxy_ssl_trusted_certificate "{profile.as_posix()}/ca.pem";
                  proxy_http_version 1.1;
                  proxy_set_header Host $http_host;
                  proxy_set_header X-Forwarded-For $remote_addr;
                  proxy_set_header X-Forwarded-Proto $scheme;
                  proxy_set_header Upgrade $http_upgrade;
                  proxy_set_header Connection $http_connection;
                }}
              }}
              server {{
                listen 127.0.0.1:{public_port} ssl;
                listen 127.0.0.1:{clear_port};
                server_name {_PUBLIC_NAME};
                ssl_certificate public.pem; ssl_certificate_key public.key;
                set $forward_scheme https; set $server 127.0.0.1; set $port {port};
                {advanced}
              }}
            }}"""
            (profile / "nginx.conf").write_text(config_text, encoding="utf-8")
            (profile / "logs").mkdir()
            (profile / "temp").mkdir()
            process = await asyncio.create_subprocess_exec(
                str(nginx.resolve()), "-p", profile.as_posix() + "/", "-c", "nginx.conf",
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            tls = ssl.create_default_context(cafile=profile / "ca.pem")
            async with (
                ClientSession(connector=TCPConnector(ssl=tls, resolver=ThreadedResolver())) as local,
                ClientSession(connector=TCPConnector(ssl=tls, resolver=_LoopbackResolver())) as phone,
            ):
                for _ in range(20):
                    try:
                        await check_request(phone, "GET", public + _BASE + "/status", 404)
                        break
                    except ClientConnectionError:
                        if process.returncode is not None:
                            raise RuntimeError((await process.stderr.read()).decode(errors="replace")) from None
                        await asyncio.sleep(0.1)
                else:
                    raise AssertionError("Nginx did not start")
                await test_api(local, phone, origin, public, cleartext, alternate, admin, guest)
                print("Core natif et Nginx : opt-in, droits, refus réseau et HTTP, réessais, approbation et révocation OK.")
                if browser:
                    await _test_browser(hass, profile, alternate, admin, channel, component, public, native=True)
                    print("Panneau Core et Chrome : calendrier, quatre capteurs, QR, annulation et expiration OK.")
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                await process.wait()
            await hass.async_stop(force=True)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--nginx", required=True, type=Path)
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--component", action="store_true")
    parser.add_argument("--channel")
    args = parser.parse_args()
    asyncio.run(main(args.nginx, args.browser, args.component, args.channel))
