"""Qualify calendar quarantine and recovery on a disposable Home Assistant Core."""

from argparse import ArgumentParser
import asyncio
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from secrets import token_bytes
from shutil import copytree
import ssl
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

from aiohttp import ClientSession, TCPConnector, ThreadedResolver
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from homeassistant import bootstrap, loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage

from qualify_native_transport import signed_phone
from real_pairing_native import check_request
from real_pairing_smoke import _DOMAIN, _ROOT, _authenticate, _b64, _certificate_files, _decode_code, _free_port, _pair, _token
from real_upgrade_030 import snapshot


@asynccontextmanager
async def core(profile, origin):
    hass = HomeAssistant(str(profile))
    config = {
        "homeassistant": {"internal_url": origin, "time_zone": "UTC"}, "frontend": {},
        "http": {"server_host": ["127.0.0.1"], "server_port": int(origin.rsplit(":", 1)[1]),
                 "ssl_certificate": str(profile / "serveur.pem"), "ssl_key": str(profile / "serveur.key")},
    }
    loader.async_setup(hass)
    try:
        assert await bootstrap.async_from_config_dict(config, hass) is hass
        await hass.async_start()
        await hass.async_block_till_done()
        yield hass
    finally:
        await hass.async_stop(force=True)


@asynccontextmanager
async def admin_socket(client, origin, admin):
    async with client.ws_connect(origin + "/api/websocket") as ws:
        assert await _authenticate(ws, admin["access_token"]) == "auth_ok"
        yield ws


async def create_entry(hass):
    flow = await hass.config_entries.flow.async_init(_DOMAIN, context={"source": "user"})
    created = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    assert created["type"] == "create_entry"
    await hass.async_block_till_done()
    return created["result"]


async def enroll(client, ws, origin):
    base = origin + "/api/horeveil/direct/v1/"
    display = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encoding = serialization.Encoding.DER
    format_spki = serialization.PublicFormat.SubjectPublicKeyInfo
    display_key = _b64(display.public_key().public_bytes(encoding, format_spki))
    invite = (await _pair(ws, "invite", origin=origin, display_key=display_key))["result"]
    secret = token_bytes(32)
    claim_id, installation = str(uuid4()), str(uuid4())
    reply = await check_request(client, "POST", base + "claim", 200,
                                headers={"X-Horeveil-Invitation": _decode_code(invite["code_encrypted"], display)},
                                json={"installation_id": installation, "claim_id": claim_id, "phone_label": "Recette",
                                      "credential_digest": _b64(sha256(secret).digest())})
    pairing_id, server_id = reply["pairing_id"], reply["server_id"]
    key = ec.generate_private_key(ec.SECP256R1())
    prepare = signed_phone(base, pairing_id, server_id, secret, key)
    await check_request(client, "POST", base + "enroll", 200, **prepare("enroll", {
        "pairing_id": pairing_id, "server_id": server_id, "claim_id": claim_id, "installation_id": installation,
        "challenge": reply["challenge"], "public_key_spki": _b64(key.public_key().public_bytes(encoding, format_spki)),
    }))
    assert (await _pair(ws, "approve", invitation_id=invite["invitation_id"],
                        pairing_id=pairing_id, confirmed=True))["success"]
    await check_request(client, "POST", base + "activate", 200, **prepare("activate", {"confirmed": True}))
    return pairing_id, prepare, base + "calendar", snapshot() | {"installation_id": installation}


def quarantine_clock():
    if os.name != "nt":
        return nullcontext()
    # Core uses ISO colons in a filename. Keep the real decode error, rename and
    # None return on Windows by giving only this Store module a filename-safe time.
    return patch.object(storage, "dt_util", SimpleNamespace(
        utcnow=lambda: SimpleNamespace(isoformat=lambda: datetime.now(UTC).isoformat().replace(":", "-")),
    ))


async def verify_store_contract(profile):
    from custom_components.horeveil.storage import HoreveilStore, StoreReadError, async_load_store

    hass = HomeAssistant(str(profile))
    store = HoreveilStore(hass, 1, "horeveil.storage-recovery-probe", atomic_writes=True)
    path = Path(store.path)
    assert await async_load_store(store) is None
    await store.async_save({"direct_source": None})
    original = path.read_bytes()
    incompatible = json.loads(original)
    incompatible["minor_version"] += 1
    path.write_bytes(json.dumps(incompatible).encode())
    before = path.read_bytes()
    try:
        await async_load_store(store)
    except StoreReadError as error:
        assert error.reason == "invalid"
    else:
        raise AssertionError("An unsupported minor version was accepted")
    assert path.read_bytes() == before
    path.write_bytes(original)
    store.set_load_empty()
    try:
        await async_load_store(store)
    except StoreReadError as error:
        assert error.reason == "indeterminate"
    else:
        raise AssertionError("An existing Store was treated as absent in load_empty mode")
    assert path.read_bytes() == original
    print("Core Store: legitimate absence, unsupported minor version and load_empty verified.", flush=True)


async def exercise(profile, *, baseline):
    _certificate_files(profile, None)
    origin = f"https://127.0.0.1:{_free_port()}"
    tls = ssl.create_default_context(cafile=profile / "ca.pem")
    async with ClientSession(connector=TCPConnector(ssl=tls, resolver=ThreadedResolver())) as client:
        async with core(profile, origin) as hass:
            a, b, independent = [await create_entry(hass) for _ in range(3)]
            admin = await _token(hass, "Administrateur de recette", origin, administrator=True)
            async with admin_socket(client, origin, admin) as ws:
                pairing_id, prepare, calendar, body = await enroll(client, ws, origin)

                async def select(ws, entry, source=pairing_id):
                    return await _pair(ws, "calendar_source", entry_id=entry.entry_id, pairing_id=source, confirmed=True)

                async def send(expected, revision):
                    return await check_request(client, "POST", calendar, expected,
                                               **prepare("calendar", body | {"revision": revision}))

                assert (await select(ws, a))["success"]
                assert (await send(200, 7))["result"] == "accepted"
                primary = Path(a.runtime_data._store.path)
                original = primary.read_bytes()
                assert await hass.config_entries.async_unload(a.entry_id)
                primary.write_bytes(b'{"data":')
                with quarantine_clock():
                    competing = await select(ws, b)
                quarantine, = primary.parent.glob(primary.name + ".corrupt.*")
                assert not primary.exists() and quarantine.read_bytes() == b'{"data":'
                if baseline:
                    assert competing["success"]
                    primary.write_bytes(original)
                    assert await hass.config_entries.async_setup(a.entry_id)
                    assert (await send(403, 8))["result"] == "source_not_authorized"
                    print("Baseline ada94b1: Core quarantined A; B accepted the phone; restored A caused HTTP 403.", flush=True)
                    return
                assert competing["error"]["code"] == "calendar_source_unavailable"
                assert not await hass.config_entries.async_setup(a.entry_id)
                assert (await select(ws, b))["error"]["code"] == "calendar_source_unavailable"
                assert (await send(403, 8))["result"] == "source_not_authorized"
                assert (await _pair(ws, "info"))["result"]["calendars"][0]["ownership_unavailable"]
                endpoint = origin + "/api/webhook/" + independent.data["webhook_id"]
                result = await check_request(client, "POST", endpoint, 200, json=snapshot(),
                                             headers={"X-Horeveil-Token": independent.data["relay_token"]})
                assert result["result"] == "accepted"
                assert not primary.exists()
                print("Core: actual quarantine, repeated denial and independent webhook verified.", flush=True)

        async with core(profile, origin) as restarted:
            async with admin_socket(client, origin, admin) as ws:
                assert (await select(ws, b))["error"]["code"] == "calendar_source_unavailable"
                assert (await send(403, 8))["result"] == "source_not_authorized"
                assert not primary.exists() and quarantine.read_bytes() == b'{"data":'
                assert not await restarted.config_entries.async_reload(a.entry_id)
            print("Core restart: quarantine still blocks ownership and writes.", flush=True)

        primary.write_bytes(original)
        async with core(profile, origin) as restored:
            async with admin_socket(client, origin, admin) as ws:
                a = restored.config_entries.async_get_entry(a.entry_id)
                b = restored.config_entries.async_get_entry(b.entry_id)
                assert str(a.runtime_data.direct_source) == pairing_id
                assert (await select(ws, b))["error"]["code"] == "calendar_source_unavailable"
                assert primary.read_bytes() == original
                assert (await send(200, 8))["result"] == "accepted"
                # Simulate a restored conflicting B Store without selecting a winner.
                second = Path(b.runtime_data._store.path)
                await b.runtime_data._store.async_save(json.loads(original)["data"])
                assert await restored.config_entries.async_unload(b.entry_id)
                assert await restored.config_entries.async_setup(b.entry_id)
                before_a, before_b = primary.read_bytes(), second.read_bytes()
                info = (await _pair(ws, "info"))["result"]["calendars"]
                assert sum(item["source_conflict"] for item in info) == 2
                assert (await send(403, 9))["result"] == "source_not_authorized"
                assert (primary.read_bytes(), second.read_bytes()) == (before_a, before_b)
                assert (await select(ws, b, None))["success"]
                assert (await send(200, 9))["result"] == "accepted"
                assert quarantine.read_bytes() == b'{"data":'
            print("Core restoration: A resumed, a restored duplicate was blocked, and explicit resolution worked.", flush=True)
    await verify_store_contract(profile / "store-contract")


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true", help="Reproduce the known defect on ada94b1.")
    options = parser.parse_args()
    with TemporaryDirectory(prefix="horeveil-storage-recovery-") as directory:
        profile = Path(directory)
        component = Path("custom_components") / _DOMAIN
        if options.baseline:
            archive = subprocess.check_output(["git", "archive", "--format=zip", "ada94b1", str(component.as_posix())], cwd=_ROOT)
            with ZipFile(BytesIO(archive)) as tree:
                tree.extractall(profile)
        else:
            copytree(_ROOT / component, profile / component)
        asyncio.run(exercise(profile, baseline=options.baseline))
