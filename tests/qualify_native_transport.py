"""Exercise native HA pairing views, confined to a disposable profile."""

import asyncio
from argparse import ArgumentParser
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
import json
from secrets import token_bytes
from shutil import copytree
import ssl
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from aiohttp import ClientConnectionError, ClientSession, TCPConnector, ThreadedResolver
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from homeassistant import bootstrap, loader
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from real_pairing_native import check_request
from real_pairing_smoke import _BASE, _DOMAIN, _ROOT, _authenticate, _b64, _certificate_files, _decode_code, _free_port, _pair, _token
from real_upgrade_030 import snapshot


async def register_probe(hass, origin):
    """Reuse Core views and the existing administrator commands without configuring a relay."""
    from custom_components.horeveil import pairing_ha as ha, pairing_http

    registry = await ha.PairingRegistry.async_open(hass)

    class Runtime(ha._PairingRuntime):
        @property
        def native(self):
            return self

    def rate_source(request):
        # Core processes forwarding before dispatch. IP only partitions the rate budget.
        return ip_address(request.remote) if request.remote else None

    runtime = Runtime(registry, None, hass.http.server_port, local_test=False, proxy=None)
    runtime.configured_origin = runtime.origin = origin
    runtime.http = pairing_http.PairingHttp(registry, origin=origin, local_source=rate_source)
    hass.data[ha._RUNTIME_KEY] = runtime
    for view in (ha._ClaimView, ha._StatusView, ha._ActivateView):
        hass.http.register_view(view(runtime))
    for command in (ha._invite, ha._status, ha._approve, ha._cancel, ha._info, ha._revoke):
        websocket_api.async_register_command(hass, command)
    return registry


async def exercise(client, origin, admin, guest, registry, through_proxy):
    status = origin + _BASE + "/status"
    forged = {"X-Horeveil-Proxy-Origin": origin, "X-Horeveil-Relay-Secret": "invalid", "X-Forwarded-For": "192.0.2.1"}
    expected = 401 if through_proxy else 400
    await check_request(client, "GET", status, expected, headers=forged)
    await check_request(client, "GET", status, 401)

    async with client.ws_connect(origin + "/api/websocket") as ws:
        assert await _authenticate(ws, "invalid") == "auth_invalid"
    async with client.ws_connect(origin + "/api/websocket") as ws:
        assert await _authenticate(ws, guest["access_token"]) == "auth_ok"
        dummy = str(uuid4())
        for command, fields in (
            ("info", {}), ("invite", {"origin": origin}), ("status", {"invitation_id": dummy}),
            ("cancel", {"invitation_id": dummy}), ("approve", {"invitation_id": dummy, "pairing_id": dummy, "confirmed": True}),
            ("revoke", {"pairing_id": dummy, "confirmed": True}),
        ):
            assert not (await _pair(ws, command, **fields))["success"]

    async with client.ws_connect(origin + "/api/websocket") as ws:
        assert await _authenticate(ws, admin["access_token"]) == "auth_ok"
        display = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key = _b64(display.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
        assert not (await _pair(ws, "invite", origin="https://other.example.invalid", display_key=key))["success"]

        async def invite():
            result = (await _pair(ws, "invite", origin=origin, display_key=key))["result"]
            return result["invitation_id"], _decode_code(result["code_encrypted"], display)

        secret = token_bytes(32)
        body = {"installation_id": str(uuid4()), "claim_id": str(uuid4()), "phone_label": "Qualification",
                "credential_digest": _b64(sha256(secret).digest())}

        async def claim(code, expected):
            return await check_request(client, "POST", origin + _BASE + "/claim", expected,
                                       json=body, headers={"X-Horeveil-Invitation": code})

        _, expired = await invite()
        with patch.object(registry, "_clock", lambda: monotonic() + 301):
            await claim(expired, 401)
        invite_id, code = await invite()
        first, duplicate = await asyncio.gather(claim(code, 200), claim(code, 200))
        assert first == duplicate
        pairing_id = first["pairing_id"]
        auth = {"X-Horeveil-Authorization": f"v1 {pairing_id}.{_b64(secret)}"}
        activate = origin + _BASE + "/activate"
        await check_request(client, "POST", activate, 403, json={"confirmed": True}, headers=auth)
        review = {"invitation_id": invite_id, "pairing_id": pairing_id, "confirmed": True}
        assert (await _pair(ws, "approve", **review))["success"]
        assert (await check_request(client, "GET", status, 200, headers=auth))["state"] == "approved"
        for _ in range(2):
            assert (await check_request(client, "POST", activate, 200, json={"confirmed": True}, headers=auth))["state"] == "active"
        assert (await _pair(ws, "revoke", pairing_id=pairing_id, confirmed=True))["success"]
        await check_request(client, "GET", status, 401, headers=auth)


async def exercise_signed(client, origin, admin, guest, entry, backend):
    from http_message_signatures import HTTPMessageSigner, HTTPSignatureKeyResolver, algorithms

    base = origin + "/api/horeveil/direct/v1/"
    alias = ec.generate_private_key(ec.SECP256R1())

    class Key(HTTPSignatureKeyResolver):
        def resolve_private_key(self, key_id):
            return alias

    signer = HTTPMessageSigner(signature_algorithm=algorithms.ECDSA_P256_SHA256, key_resolver=Key())
    await check_request(client, "POST", base + "status", 401, headers={"X-Horeveil-Relay-Secret": "invalid"})
    await check_request(client, "POST", origin + _BASE + "/claim", 404)
    async with client.ws_connect(origin + "/api/websocket") as ws:
        assert await _authenticate(ws, guest["access_token"]) == "auth_ok"
        assert not (await _pair(ws, "info"))["success"]
        assert not (await _pair(ws, "calendar_source", entry_id=entry.entry_id, pairing_id=None, confirmed=True))["success"]
    async with client.ws_connect(origin + "/api/websocket") as ws:
        assert await _authenticate(ws, admin["access_token"]) == "auth_ok"
        assert origin in (await _pair(ws, "info"))["result"]["https_origins"]
        display = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        display_key = _b64(display.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
        invite = (await _pair(ws, "invite", origin=origin, display_key=display_key))["result"]
        code = _decode_code(invite["code_encrypted"], display)
        secret = token_bytes(32)
        claim = {"installation_id": str(uuid4()), "claim_id": str(uuid4()), "phone_label": "Qualification",
                 "credential_digest": _b64(sha256(secret).digest())}
        reply = await check_request(client, "POST", base + "claim", 200,
                                    headers={"X-Horeveil-Invitation": code}, json=claim)
        assert reply["profile"] == "signed-v1"
        pairing_id, server_id = reply["pairing_id"], reply["server_id"]
        approval = {"invitation_id": invite["invitation_id"], "pairing_id": pairing_id, "confirmed": True}
        assert not (await _pair(ws, "approve", **approval))["success"]

        def prepare(operation, body):
            raw = json.dumps(body, separators=(",", ":")).encode()
            message = SimpleNamespace(method="POST", url=base + operation, headers={
                "Content-Type": "application/json", "Horeveil-Server-Id": server_id,
                "X-Horeveil-Authorization": f"v1 {pairing_id}.{_b64(secret)}",
            })
            now = datetime.now(UTC)
            signer.sign(message, key_id=pairing_id, created=now, expires=now + timedelta(seconds=120),
                        nonce=str(uuid4()), label="sig1", tag=f"horeveil-{operation}-v1", body=raw,
                        covered_component_ids=("@method", "@target-uri", "content-type", "content-digest", "horeveil-server-id"))
            return {"data": raw, "headers": message.headers}

        enrollment = {key: claim[key] for key in ("claim_id", "installation_id")}
        enrollment.update(pairing_id=pairing_id, server_id=server_id, challenge=reply["challenge"], public_key_spki=_b64(
            alias.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)))
        await check_request(client, "POST", base + "enroll", 200, **prepare("enroll", enrollment))
        assert (await _pair(ws, "approve", **approval))["success"]
        activate = prepare("activate", {"confirmed": True})
        assert (await check_request(client, "POST", base + "activate", 200, **activate))["state"] == "active"
        await check_request(client, "POST", base + "activate", 401, **activate)
        calendar = base + "calendar"
        body = snapshot() | {"installation_id": claim["installation_id"]}
        await check_request(client, "POST", calendar, 401, json=body)
        await check_request(client, "POST", calendar, 403, **prepare("calendar", body))
        selection = {"entry_id": entry.entry_id, "pairing_id": pairing_id, "confirmed": True}
        assert (await _pair(ws, "calendar_source", **selection))["success"]
        signed = prepare("calendar", body)
        assert (await check_request(client, "POST", calendar, 200, **signed))["result"] == "accepted"
        await check_request(client, "POST", calendar, 401, **signed)
        assert (await check_request(client, "POST", calendar, 200, **prepare("calendar", body)))["result"] == "duplicate"
        assert (await _pair(ws, "info"))["result"]["calendars"][0]["last_success"]
        assert entry.runtime_data.current.installation_id == claim["installation_id"]
        path = "/api/webhook/" + entry.data["webhook_id"]
        assert (await check_request(client, "POST", backend + path, 403, json=snapshot(),
                                    headers={"X-Horeveil-Token": entry.data["relay_token"]}))["result"] == "source_not_authorized"
        empty = body | {"revision": body["revision"] + 1, "alarms": {}, "occurrences": []}
        assert (await check_request(client, "POST", calendar, 200, **prepare("calendar", empty)))["result"] == "accepted"
        assert not tuple(entry.runtime_data.current.iter_calendar_occurrences())
        assert (await _pair(ws, "revoke", pairing_id=pairing_id, confirmed=True))["success"]
        await check_request(client, "POST", base + "status", 401, **prepare("status", {}))
        await check_request(client, "POST", calendar, 401, **prepare("calendar", empty))
        assert entry.runtime_data.current is None
        assert (await _pair(ws, "calendar_source", **(selection | {"pairing_id": None})))["success"]


async def exercise_android(client, origin, admin, entry, profile, adb, serial):
    """Run the Android transport with a disposable CA and the real Core views."""
    from homeassistant.core import callback
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    package = "fr.moosketair.horeveil"
    command = [str(adb), "-s", serial]
    input_file = "files/horeveil-calendar-recette.json"
    port = origin.rsplit(":", 1)[1]

    async def launch(*args, stdin=None):
        return await asyncio.create_subprocess_exec(
            *command, *args, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )

    async def adb_ok(*args, body=None):
        proc = await launch(*args, stdin=subprocess.PIPE if body is not None else None)
        output, _ = await proc.communicate(body)
        if proc.returncode:
            raise RuntimeError(f"ADB fixture setup failed ({proc.returncode})")
        return output

    observations = []
    receiver = entry.runtime_data

    @callback
    def observe():
        if snapshot := receiver.current:
            observations.append((snapshot.revision, len(tuple(snapshot.iter_calendar_occurrences()))))

    remove = async_dispatcher_connect(receiver._hass, receiver.update_signal, observe)
    proc = None
    try:
        await adb_ok("reverse", f"tcp:{port}", f"tcp:{port}")
        async with client.ws_connect(origin + "/api/websocket") as ws:
            assert await _authenticate(ws, admin["access_token"]) == "auth_ok"
            previous = (await _pair(ws, "info"))["result"]["phones"]
            display = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            display_key = _b64(display.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            ))
            invite = (await _pair(ws, "invite", origin=origin, display_key=display_key))["result"]
            payload = json.dumps({
                "origin": origin, "code": _decode_code(invite["code_encrypted"], display),
                "ca": (profile / "ca.pem").read_text(),
            }).encode()
            await adb_ok("shell", "-T", f"run-as {package} sh -c 'mkdir -p files && cat > {input_file}'",
                         body=payload)
            proc = await launch(
                "shell", "am", "instrument", "-w", "-e", "class",
                f"{package}.ui.DirectCalendarAndroidTest#signedCalendarReachesDisposableHomeAssistant",
                f"{package}.test/androidx.test.runner.AndroidJUnitRunner",
            )

            async def wait_for(operation, predicate):
                async with asyncio.timeout(40):
                    while proc.returncode is None:
                        result = await operation()
                        if predicate(result):
                            return result
                        await asyncio.sleep(0.4)
                raise RuntimeError("Android fixture stopped before completing the association")

            review = await wait_for(
                lambda: _pair(ws, "status", invitation_id=invite["invitation_id"], display_key=display_key),
                lambda response: response.get("result", {}).get("state") == "awaiting_approval",
            )
            pairing_id = review["result"]["pairing_id"]
            assert pairing_id not in previous
            assert (await _pair(ws, "approve", invitation_id=invite["invitation_id"],
                                pairing_id=pairing_id, confirmed=True))["success"]
            await wait_for(lambda: _pair(ws, "info"), lambda response: pairing_id in response["result"]["phones"])
            selection = {"entry_id": entry.entry_id, "confirmed": True}
            assert (await _pair(ws, "calendar_source", pairing_id=pairing_id, **selection))["success"]
            async with asyncio.timeout(50):
                output, _ = await proc.communicate()
            if b"OK (1 test)" not in output:
                raise RuntimeError(output.decode(errors="replace"))
            assert observations[0][0] == 1 and observations[0][1] > 0, observations
            assert observations[-1] == (2, 0), observations
            assert (await _pair(ws, "revoke", pairing_id=pairing_id, confirmed=True))["success"]
            assert receiver.current is None
            assert (await _pair(ws, "calendar_source", pairing_id=None, **selection))["success"]
        print("Émulateur Android : appairage Keystore, envoi signé et retrait acquitté.", flush=True)
    finally:
        remove()
        if proc is not None and proc.returncode is None:
            proc.terminate()
            await proc.wait()
        await adb_ok("shell", "run-as", package, "rm", "-f", input_file)
        await adb_ok("reverse", "--remove", f"tcp:{port}")


async def relay_tcp(reader, writer, nginx_port):
    """Present a synthetic WAN socket address to Nginx using a test-only listener."""
    upstream_reader, upstream = await asyncio.open_connection("127.0.0.1", nginx_port)
    upstream.write(b"PROXY TCP4 8.8.8.8 127.0.0.1 43210 443\r\n")

    async def pipe(source, target):
        while data := await source.read(65536):
            target.write(data)
            await target.drain()

    tasks = [asyncio.create_task(pipe(reader, upstream)), asyncio.create_task(pipe(upstream_reader, writer))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        upstream.close()
        writer.close()


async def run(profile, nginx, stock, through_proxy, signed, adb=None, android_serial=None):
    _certificate_files(profile, nginx=False)
    core_port, proxy_port = _free_port(), _free_port()
    backend = f"{'http' if through_proxy else 'https'}://127.0.0.1:{core_port}"
    config = {"http": {"server_host": ["127.0.0.1"], "server_port": core_port}}
    if through_proxy:
        config["http"].update(use_x_forwarded_for=True, trusted_proxies=["127.0.0.2"])
    else:
        config["http"].update(ssl_certificate=str(profile / "serveur.pem"), ssl_key=str(profile / "serveur.key"))
    hass = HomeAssistant(str(profile))
    loader.async_setup(hass)
    process, relay = None, None
    try:
        if through_proxy:
            relay = await asyncio.start_server(lambda r, w: relay_tcp(r, w, proxy_port), "127.0.0.1", 0)
            origin = f"https://127.0.0.1:{relay.sockets[0].getsockname()[1]}"
            (profile / "proxy.conf").write_bytes(stock.read_bytes())
            (profile / "logs").mkdir()
            (profile / "temp").mkdir()
            (profile / "nginx.conf").write_text(f"""daemon off; master_process off; events {{}}
http {{ server {{ listen 127.0.0.1:{proxy_port} ssl proxy_protocol;
    set_real_ip_from 127.0.0.1; real_ip_header proxy_protocol;
    ssl_certificate serveur.pem; ssl_certificate_key serveur.key;
    set $forward_host 127.0.0.1; set $forward_scheme http; set $server 127.0.0.1; set $port {core_port};
    set $x_forwarded_scheme $scheme; set $x_forwarded_proto $scheme;
    location / {{ proxy_bind 127.0.0.2; proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection $http_connection;
        include proxy.conf; }}
}} }}""", encoding="ascii")
            process = await asyncio.create_subprocess_exec(str(nginx.resolve()), "-p", profile.as_posix() + "/", "-c", "nginx.conf",
                                                           stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
        else:
            origin = backend
        if signed:
            config.update(homeassistant={"internal_url": origin}, frontend={})
        assert await bootstrap.async_from_config_dict(config, hass) is hass
        await hass.async_start()
        flow = await hass.config_entries.flow.async_init(_DOMAIN, context={"source": "user"})
        assert (await hass.config_entries.flow.async_configure(flow["flow_id"], {}))["type"] == "create_entry"
        await hass.async_block_till_done()
        entry, = hass.config_entries.async_entries(_DOMAIN)
        registry = None if signed else await register_probe(hass, origin)
        admin = await _token(hass, "Administrateur de qualification", origin, administrator=True)
        guest = await _token(hass, "Utilisateur de qualification", origin, administrator=False)
        tls = ssl.create_default_context(cafile=profile / "ca.pem")
        async with ClientSession(connector=TCPConnector(ssl=tls, resolver=ThreadedResolver())) as client:
            if through_proxy:
                for _ in range(20):
                    try:
                        if signed:
                            await check_request(client, "POST", origin + "/api/horeveil/direct/v1/status", 401)
                        else:
                            await check_request(client, "GET", origin + _BASE + "/status", 401)
                        break
                    except ClientConnectionError:
                        await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("Nginx de qualification indisponible")
                url = backend + ("/api/horeveil/direct/v1" if signed else _BASE) + "/status"
                method = "POST" if signed else "GET"
                await check_request(client, method, url, 404)
                await check_request(client, method, url, 400,
                                    headers={"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https"})
            if signed:
                await exercise_signed(client, origin, admin, guest, entry, backend)
                if adb is not None:
                    await exercise_android(client, origin, admin, entry, profile, adb, android_serial)
            else:
                await exercise(client, origin, admin, guest, registry, through_proxy)
            # A real synthetic webhook, with credentials from this disposable profile only.
            path = "/api/webhook/" + entry.data["webhook_id"]
            auth = {"X-Horeveil-Token": entry.data["relay_token"]}
            if through_proxy:
                await check_request(client, "POST", origin + path, 200, json=snapshot(),
                                    headers=auth | {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"})
                assert entry.runtime_data.current is None
            result = await check_request(client, "POST", backend + path, 200, json=snapshot(), headers=auth)
            assert result["result"] == "accepted"
        print(("Nginx avec include standard NPM" if through_proxy else "HTTPS direct") +
              " — administration, appairage et webhook historique validés.", flush=True)
    finally:
        if relay is not None:
            relay.close()
            await relay.wait_closed()
        if process is not None and process.returncode is None:
            process.terminate()
            await process.wait()
        await hass.async_stop(force=True)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--nginx", type=Path)
    parser.add_argument("--stock-proxy", type=Path)
    parser.add_argument("--signed", action="store_true", help="Exercise the installed signed profile instead of the qualification probe")
    parser.add_argument("--adb", type=Path, help="Use the existing app and test APKs on a disposable emulator")
    parser.add_argument("--android-serial")
    args = parser.parse_args()
    if bool(args.nginx) != bool(args.stock_proxy):
        parser.error("--nginx and --stock-proxy are required together")
    if args.adb or args.android_serial:
        if not args.adb or not args.android_serial or not args.android_serial.startswith("emulator-") or not args.signed or args.nginx:
            parser.error("Android requires --signed, --adb, --android-serial emulator-PORT and direct HTTPS")
    with TemporaryDirectory(prefix="horeveil-native-qualification-") as name:
        profile = Path(name)
        copytree(_ROOT / "custom_components" / _DOMAIN, profile / "custom_components" / _DOMAIN)
        asyncio.run(run(profile, args.nginx, args.stock_proxy, bool(args.nginx), args.signed,
                        args.adb, args.android_serial))
