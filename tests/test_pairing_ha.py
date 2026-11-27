"""Test native registration and socket policy using HA doubles and real mTLS."""

import asyncio
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from ipaddress import IPv4Address
import json
from pathlib import Path
from secrets import token_bytes
import ssl
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from aiohttp import ClientConnectionError, ClientSession, TCPConnector, web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from test_pairing_protocol import comparison_code
from test_pairing_signed import signed_headers
from test_receiver import (
    DiskStore, EXAMPLE, RELAY_HEADER, RELAY_TOKEN, WEBHOOK_ID, async_setup_entry, async_unload_entry,
    current_request, is_cloud_request, pairing_ha_module, pairing_module, pairing_signed_module,
    receiver_module, setup_domain,
)

_BASE = "/api/horeveil/v1/pair"
_ORIGIN = "https://127.0.0.1:8123"


def b64(value):
    return urlsafe_b64encode(value).decode().rstrip("=")


class NativePairingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name)
        now = datetime.now(UTC)
        issuer_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        issuer = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Temporary test CA")])

        def builder(name, key):
            return (
                x509.CertificateBuilder().subject_name(name).issuer_name(issuer)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(hours=1))
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                    critical=False,
                )
            )

        ca = builder(issuer, issuer_key).add_extension(
            x509.BasicConstraints(ca=True, path_length=0), critical=True,
        ).add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        ).sign(issuer_key, hashes.SHA256())
        cafile = root / "ca.pem"
        cafile.write_bytes(ca.public_bytes(serialization.Encoding.PEM))

        def cert_files(name, purpose):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, name)])
            cert = builder(subject, key).add_extension(
                x509.ExtendedKeyUsage([purpose]), critical=False,
            ).add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(IPv4Address("127.0.0.1"))]),
                critical=False,
            ).sign(issuer_key, hashes.SHA256())
            pem = root / f"{name}.pem"
            pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            keyfile = root / f"{name}.key"
            keyfile.write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            return key, cert.fingerprint(hashes.SHA256()).hex(), pem, keyfile

        cls.server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        _, _, pem, keyfile = cert_files("server", x509.ExtendedKeyUsageOID.SERVER_AUTH)
        cls.server_tls.load_cert_chain(pem, keyfile)
        cls.server_tls.load_verify_locations(cafile)
        cls.server_tls.verify_mode = ssl.CERT_REQUIRED
        for name in ("pinned", "other"):
            key, fingerprint, pem, keyfile = cert_files(name, x509.ExtendedKeyUsageOID.CLIENT_AUTH)
            context = ssl.create_default_context(cafile=cafile)
            context.load_cert_chain(pem, keyfile)
            setattr(cls, f"{name}_tls", context)
            setattr(cls, f"{name}_key", key)
            if name == "pinned":
                cls.fingerprint = fingerprint
        cls.anonymous_tls = ssl.create_default_context(cafile=cafile)

    async def asyncSetUp(self):
        self.directory = Path(self.enterContext(
            TemporaryDirectory(dir=Path(__file__).resolve().parents[1]),
        ))
        self.enterContext(patch.dict(
            "sys.modules", {"custom_components.horeveil.pairing_ha": pairing_ha_module,
                            "custom_components.horeveil.pairing_signed": pairing_signed_module},
        ))
        self.secret = token_bytes(32)
        self.payload = {
            "claim_id": str(uuid4()), "installation_id": str(uuid4()),
            "phone_label": "Donnée privée de test", "credential_digest": b64(sha256(self.secret).digest()),
        }
        self.admin = SimpleNamespace(is_admin=True, is_active=True, id="owner")
        self.user = self.admin
        self.cloud = False

    def new_ha(self):
        views = {}
        listeners = {}
        http = SimpleNamespace(
            server_host=["127.0.0.1"], server_port=8123, ssl_peer_certificate="test-ca.pem",
            trusted_proxies=[], context=self.server_tls, supervisor_unix_socket_path=None,
            register_view=lambda view: views.update({view.url: view}),
        )
        return SimpleNamespace(
            http=http, directory=self.directory,
            config=SimpleNamespace(path=self.directory.joinpath, components={"frontend"}, external_url=None),
            data={}, ws_commands={}, views=views, listeners=listeners, panels={},
            bus=SimpleNamespace(async_listen_once=listeners.__setitem__),
        )

    def config(self):
        return {"horeveil": {"local_pairing_test_client_sha256": self.fingerprint}}

    async def start(self, *, universal=False):
        self.hass = self.new_ha()
        await setup_domain(self.hass, {} if universal else self.config())
        app = web.Application()

        async def dispatch_ws(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                msg = frame.json()
                results = []
                connection = SimpleNamespace(
                    user=self.user,
                    send_result=lambda _, data: results.append({"result": data}),
                    send_error=lambda _, code, _message: results.append({"error": code}),
                )
                request_token = current_request.set(request)
                cloud_token = is_cloud_request.set(self.cloud)
                try:
                    await self.hass.ws_commands[msg["type"]](self.hass, connection, msg)
                finally:
                    current_request.reset(request_token)
                    is_cloud_request.reset(cloud_token)
                await ws.send_json(results[0])
            return ws

        async def dispatch_http(request):
            view = self.hass.views[request.path]
            token = is_cloud_request.set(self.cloud)
            try:
                return await getattr(view, request.method.lower())(request)
            finally:
                is_cloud_request.reset(token)

        async def dispatch_webhook(request):
            key = request.match_info["webhook_id"]
            handler, _ = self.hass.hooks[key]
            return await handler(self.hass, key, request)

        app.router.add_get("/api/websocket", dispatch_ws)
        app.router.add_post("/api/webhook/{webhook_id}", dispatch_webhook)
        for path, view in self.hass.views.items():
            method = "GET" if hasattr(view, "get") else "POST"
            app.router.add_route(method, path, dispatch_http)
        self.server = TestServer(app)
        await self.server.start_server(ssl=self.server_tls)
        self.addAsyncCleanup(self.server.close)
        self.client = await self.enterAsyncContext(ClientSession(connector=TCPConnector(ssl=self.pinned_tls)))

    async def ws(self, operation, **fields):
        async with self.client.ws_connect(self.server.make_url("/api/websocket")) as ws:
            await ws.send_json({"id": 1, "type": f"horeveil/pairing_test/{operation}", **fields})
            return await ws.receive_json()

    async def setup_calendars(self, entries):
        async def platforms(*_):
            return True

        hass = self.hass
        hass.is_stopping = False
        hass.hooks = {}
        hass.async_add_executor_job = asyncio.to_thread
        hass.async_create_task = asyncio.create_task
        hass.config_entries = SimpleNamespace(
            async_entries=lambda _: entries, async_forward_entry_setups=platforms, async_unload_platforms=platforms,
        )
        for entry in entries:
            await async_setup_entry(hass, entry)

    @staticmethod
    def calendar_entry(name):
        return SimpleNamespace(entry_id=name, title=name, data={WEBHOOK_ID: uuid4().hex, RELAY_TOKEN: "recette"})

    async def signed_phone(self):
        registry = self.hass.data["horeveil.local_pairing_test"].registry
        key = ec.generate_private_key(ec.SECP256R1())
        secret = token_bytes(32)
        installation = uuid4()
        public_key = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        invite = await registry.async_invite(administrator_id=self.admin.id, confirmed_origin=_ORIGIN, direct=True)
        claim = pairing_module.PairingClaim(uuid4(), installation, "Téléphone de recette", sha256(secret).digest())
        reservation = await registry.async_claim(invite.code, claim, origin=_ORIGIN, direct=True)
        pairing_id = reservation.pairing_id
        now = int(datetime.now(UTC).timestamp())

        def verify(*_):
            return uuid4(), now + 60, now

        await registry.async_direct_request(
            pairing_id, secret, "enroll", verify,
            enrollment=(public_key, claim.claim_id, installation, reservation.challenge),
        )
        review = await registry.async_review_claim(self.admin, invite.invitation_id)
        await registry.async_approve_claim(self.admin, pairing_id, review.comparison_code)
        await registry.async_direct_request(pairing_id, secret, "activate", verify)
        return SimpleNamespace(id=pairing_id, installation=str(installation), key=key, secret=secret,
                               server_id=str(reservation.server_id))

    async def select_source(self, entry, phone=None):
        return await self.ws("calendar_source", entry_id=entry.entry_id,
                             pairing_id=str(phone.id) if phone else None, confirmed=True)

    @staticmethod
    def calendar_body(installation, revision):
        now = datetime.now(UTC)
        return json.dumps({
            "schema_version": 1, "event": "calendar_snapshot", "installation_id": installation,
            "revision": revision, "generated_at": now.isoformat(), "window_start": now.isoformat(),
            "window_end_exclusive": (now + timedelta(days=30)).isoformat(), "timezone": "UTC",
            "expires_at": (now + timedelta(hours=1)).isoformat(), "occurrences": [],
        }).encode()

    async def publish_direct(self, phone, revision):
        body = self.calendar_body(phone.installation, revision)
        headers = signed_headers(phone.key, _ORIGIN, "calendar", str(phone.id), phone.server_id, phone.secret, body)
        async with self.client.post(self.server.make_url(pairing_signed_module.DIRECT_BASE + "/calendar"),
                                    data=body, headers=headers) as response:
            result = await response.json()
            return response.status, result.get("result", result.get("error"))

    async def publish_webhook(self, entry, installation, revision):
        path = "/api/webhook/" + entry.data[WEBHOOK_ID]
        async with self.client.post(self.server.make_url(path), data=self.calendar_body(installation, revision),
                                    headers={RELAY_HEADER: entry.data[RELAY_TOKEN], "Content-Type": "application/json"}) as response:
            return response.status, (await response.json())["result"]

    async def test_uncertain_assignment_excludes_other_entries_through_reload(self):
        await self.start(universal=True)
        a, b = entries = [self.calendar_entry(name) for name in ("a", "b")]
        await self.setup_calendars(entries)
        phone = await self.signed_phone()
        store = a.runtime_data._store
        with patch.object(store, "async_load", side_effect=OSError):
            self.assertEqual(await self.select_source(a, phone), {"error": "storage_outcome_unknown"})
        contender = await self.select_source(b, phone)
        await async_unload_entry(self.hass, a)
        self.assertEqual(await self.select_source(b, phone), {"error": "calendar_source_unavailable"})
        await async_setup_entry(self.hass, a)
        delivery = await self.publish_direct(phone, 1)
        self.assertEqual((contender, delivery), (
            {"error": "calendar_source_unavailable"}, (200, "accepted"),
        ))

    async def test_uncertain_source_transition_matrix(self):
        await self.start(universal=True)
        old_phone, new_phone, independent = [await self.signed_phone() for _ in range(3)]
        for old, new in ((None, new_phone), (old_phone, None), (old_phone, new_phone)):
            for persisted in (False, True):
                with self.subTest(old_is_direct=old is not None, new_is_direct=new is not None, persisted=persisted):
                    a, b, c = entries = [self.calendar_entry(uuid4().hex) for _ in range(3)]
                    await self.setup_calendars(entries)
                    installation = str(uuid4())
                    if old:
                        self.assertEqual(await self.select_source(a, old), {"result": {"state": "selected"}})
                        self.assertEqual(await self.publish_direct(old, 1), (200, "accepted"))
                    else:
                        self.assertEqual(await self.publish_webhook(a, installation, 1), (200, "accepted"))
                    store = a.runtime_data._store
                    before = store.path.read_bytes()
                    self.hass.is_stopping = True
                    try:
                        self.assertEqual(await self.select_source(a, new), {"error": "calendar_source_unavailable"})
                    finally:
                        self.hass.is_stopping = False
                    self.assertEqual(store.path.read_bytes(), before)
                    self.assertFalse(a.runtime_data.source_selection_uncertain)

                    failed_step = "async_load" if persisted else "async_save"
                    with patch.object(store, failed_step, side_effect=OSError):
                        self.assertEqual(await self.select_source(a, new), {"error": "storage_outcome_unknown"})
                    durable = store.path.read_bytes()
                    self.assertEqual(durable != before, persisted)
                    for candidate in (phone for phone in (old, new) if phone):
                        self.assertEqual(await self.select_source(b, candidate), {"error": "calendar_source_unavailable"})
                        self.assertEqual(await self.publish_direct(candidate, 2), (403, "source_not_authorized"))
                    self.assertEqual(await self.select_source(b, independent), {"result": {"state": "selected"}})
                    self.assertEqual(await self.publish_direct(independent, 1), (200, "accepted"))
                    self.assertEqual(await self.publish_webhook(c, installation, 1), (200, "accepted"))
                    with patch.object(pairing_ha_module, "_https_origins", return_value=[]):
                        info = (await self.ws("info"))["result"]
                    self.assertTrue(info["calendars"][0]["source_uncertain"])
                    self.assertEqual(store.path.read_bytes(), durable)

                    if old and new and not persisted:
                        self.hass = self.new_ha()
                        await setup_domain(self.hass, {})
                        await self.setup_calendars(entries)
                    else:
                        await async_unload_entry(self.hass, a)
                        await async_setup_entry(self.hass, a)
                    retained, released = (new, old) if persisted else (old, new)
                    self.assertFalse(a.runtime_data.source_selection_uncertain)
                    self.assertEqual(a.runtime_data.direct_source, retained.id if retained else None)
                    self.assertEqual(store.path.read_bytes(), durable)
                    if retained:
                        self.assertEqual(await self.select_source(b, retained), {"error": "calendar_source_unavailable"})
                        self.assertEqual(await self.publish_direct(retained, 2), (200, "accepted"))
                    else:
                        self.assertEqual(await self.publish_webhook(a, installation, 2), (200, "accepted"))
                    if released:
                        self.assertEqual(await self.select_source(b, released), {"result": {"state": "selected"}})
                        self.assertEqual(await self.publish_direct(released, 3), (200, "accepted"))
                    for entry in entries:
                        await async_unload_entry(self.hass, entry)

    async def test_preexisting_conflict_is_blocked_even_if_an_entry_is_unloaded(self):
        await self.start(universal=True)
        phone = await self.signed_phone()
        a, b, c = entries = [self.calendar_entry(name) for name in ("a", "b", "c")]
        for entry in (a, b):
            store = DiskStore(self.hass, 1, f"horeveil.{entry.entry_id}")
            await store.async_save({
                "direct_source": str(phone.id),
                "snapshot": self.calendar_body(phone.installation, 1).decode(),
            })
        await self.setup_calendars(entries)
        a_store, b_store = a.runtime_data._store, b.runtime_data._store
        before_a, before_b = a_store.path.read_bytes(), b_store.path.read_bytes()
        await async_unload_entry(self.hass, b)
        with patch.object(pairing_ha_module, "_https_origins", return_value=[]):
            info = (await self.ws("info"))["result"]
        self.assertTrue(info["calendars"][0]["source_conflict"])
        self.assertIn(str(phone.id), info["calendars"][1]["unavailable_phones"])
        self.assertEqual(await self.publish_direct(phone, 2), (403, "source_not_authorized"))
        self.assertEqual(await self.select_source(c, phone), {"error": "calendar_source_unavailable"})
        with patch.object(pairing_ha_module, "calendar_store", side_effect=OSError):
            self.assertEqual(await self.publish_direct(phone, 2), (403, "source_not_authorized"))
            self.assertEqual(await self.select_source(c, phone), {"error": "calendar_source_unavailable"})
            self.assertEqual(await self.publish_webhook(c, str(uuid4()), 1), (200, "accepted"))
        self.assertEqual((a_store.path.read_bytes(), b_store.path.read_bytes()), (before_a, before_b))
        await async_setup_entry(self.hass, b)
        self.assertEqual(await self.select_source(b), {"result": {"state": "selected"}})
        self.assertEqual(a_store.path.read_bytes(), before_a)
        self.assertIsNone((await b_store.async_load())["snapshot"])
        self.assertEqual(await self.publish_direct(phone, 2), (200, "accepted"))

    async def test_concurrent_assignment_then_revocation_during_uncertainty(self):
        await self.start(universal=True)
        phone = await self.signed_phone()
        a, b = entries = [self.calendar_entry(name) for name in ("a", "b")]
        await self.setup_calendars(entries)
        store = a.runtime_data._store
        entered, queued, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        save = store.async_save
        registry = self.hass.data["horeveil.local_pairing_test"].registry
        select = registry.async_select_calendar_source

        async def pause_after_write(data):
            await save(data)
            entered.set()
            await release.wait()
            raise OSError

        async def observe_selection(source, callback):
            if entered.is_set():
                queued.set()
            await select(source, callback)

        with (patch.object(store, "async_save", pause_after_write),
              patch.object(registry, "async_select_calendar_source", observe_selection)):
            first = asyncio.create_task(self.select_source(a, phone))
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(self.select_source(b, phone))
            try:
                await asyncio.wait_for(queued.wait(), 5)
                self.assertFalse(second.done())
            finally:
                release.set()
            self.assertEqual(await first, {"error": "storage_outcome_unknown"})
            self.assertEqual(await second, {"error": "calendar_source_unavailable"})
        self.assertEqual(await self.ws("revoke", pairing_id=str(phone.id), confirmed=True),
                         {"error": "registry_unavailable"})
        self.assertEqual(await self.publish_direct(phone, 1), (401, "invalid_credential"))
        await async_unload_entry(self.hass, a)
        await async_setup_entry(self.hass, a)
        self.assertEqual(await self.select_source(b, phone), {"error": "calendar_source_unavailable"})
        self.assertEqual(await self.select_source(a), {"result": {"state": "selected"}})
        self.assertEqual(await self.publish_webhook(a, str(uuid4()), 1), (200, "accepted"))

    def unseal(self, ciphertext):
        return self.pinned_key.decrypt(
            urlsafe_b64decode(ciphertext + "=" * (-len(ciphertext) % 4)),
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        ).decode()

    async def claim(self, code, **kwargs):
        return await self.client.post(
            self.server.make_url(f"{_BASE}/claim"), json=self.payload,
            headers={"X-Horeveil-Invitation": code, **kwargs.pop("headers", {})}, **kwargs,
        )

    def auth(self, pairing_id):
        return {"X-Horeveil-Authorization": f"v1 {pairing_id}.{b64(self.secret)}"}

    async def test_default_only_registers_signed_routes_and_unsafe_legacy_is_disabled(self):
        hass = self.new_ha()
        await setup_domain(hass, {})
        self.assertIn("/api/horeveil/direct/v1/enroll", hass.views)
        self.assertIn("/api/horeveil/direct/v1/calendar", hass.views)
        self.assertNotIn(f"{_BASE}/claim", hass.views)
        self.assertEqual(
            set(hass.ws_commands), {f"horeveil/pairing_test/{command}" for command in (
                "invite", "status", "approve", "cancel", "info", "rename", "revoke", "calendar_source",
            )},
        )
        self.assertTrue(hass.panels["horeveil-association"]["require_admin"])
        for field, value in (
            ("server_host", ["0.0.0.0"]), ("server_host", ["127.0.0.1", "::"]),
            ("ssl_peer_certificate", None), ("context", None),
            ("trusted_proxies", ["127.0.0.1"]), ("supervisor_unix_socket_path", "/run/ha.sock"),
        ):
            with self.subTest(field=field, value=value):
                hass = self.new_ha()
                setattr(hass.http, field, value)
                with self.assertLogs(pairing_ha_module.__name__, level="WARNING"):
                    await setup_domain(hass, self.config())
                self.assertEqual(hass.views, {})
                self.assertEqual(hass.ws_commands, {})
                self.assertEqual(hass.panels, {})
                self.assertEqual(hass.data, {})

    async def test_real_tls_rejects_unpinned_clients_and_proxy_metadata(self):
        await self.start()
        url = self.server.make_url(f"{_BASE}/status")
        for name in ("other_tls", "anonymous_tls"):
            async with ClientSession(connector=TCPConnector(ssl=getattr(self, name))) as client:
                if name == "anonymous_tls":
                    with self.assertRaises(ClientConnectionError):
                        await client.get(url)
                else:
                    response = await client.get(url)
                    self.assertEqual(response.status, 404)
        for header in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Proto", "X-Real-IP", "Via"):
            with self.subTest(header=header):
                response = await self.client.get(url, headers={header: "127.0.0.1"})
                self.assertEqual(response.status, 404)
        self.cloud = True
        self.assertEqual((await self.client.get(url)).status, 404)
        self.assertEqual((await self.ws("invite", origin=_ORIGIN))["error"], "unauthorized")
        self.cloud = False
        self.assertEqual((await self.client.get(url)).status, 401)

    async def test_rename_rejects_http_in_the_standard_setup(self):
        hass = self.new_ha()
        await setup_domain(hass, {})
        results = []
        connection = SimpleNamespace(
            user=self.admin,
            send_result=lambda _, data: results.append(data),
            send_error=lambda _, code, _message: results.append(code),
        )
        token = current_request.set(SimpleNamespace(secure=False))
        try:
            await hass.ws_commands["horeveil/pairing_test/rename"](hass, connection, {
                "id": 1, "pairing_id": str(uuid4()), "label": "Essai",
            })
        finally:
            current_request.reset(token)
        self.assertEqual(results, ["unauthorized"])
        self.assertFalse((self.directory / "horeveil.pairing_labels").exists())

    async def test_calendar_source_write_succeeds_before_readback_fails(self):
        await self.start(universal=True)
        hass = self.hass
        hass.is_stopping = False
        hass.async_add_executor_job = asyncio.to_thread
        entry = SimpleNamespace(entry_id="recette", title="Calendrier de recette")
        hass.config_entries = SimpleNamespace(async_entries=lambda _domain: [entry])
        store = DiskStore(hass, 1, "calendar")
        source = uuid4()
        await store.async_save({
            "snapshot": json.dumps(EXAMPLE), "direct_source": str(source),
            "received_at": EXAMPLE["generated_at"],
        })
        receiver = receiver_module.SnapshotReceiver(hass, store, "recette", entry.entry_id)
        await receiver.async_restore()
        receivers = hass.data[pairing_ha_module.CALENDAR_RECEIVERS] = {entry.entry_id: receiver}
        fields = {"entry_id": entry.entry_id, "pairing_id": None}
        self.user = SimpleNamespace(id="other", is_active=True, is_admin=False)
        self.assertEqual(await self.ws("calendar_source", **fields, confirmed=True), {"error": "unauthorized"})
        self.user = self.admin
        self.assertEqual(await self.ws("calendar_source", **fields, confirmed=False),
                         {"error": "calendar_source_unavailable"})
        with patch.object(store, "async_load", side_effect=OSError):
            response = await self.ws("calendar_source", **fields, confirmed=True)
        self.assertEqual(receiver.direct_source, source)
        self.assertIsNotNone(receiver.received_at)
        self.assertEqual(store.writes, 2)
        with patch.object(pairing_ha_module, "_https_origins", return_value=[]):
            stale = (await self.ws("info"))["result"]["calendars"][0]
            restarted_store = DiskStore(hass, 1, "calendar")
            restored = receiver_module.SnapshotReceiver(hass, restarted_store, "recette", entry.entry_id)
            await restored.async_restore()
            receivers[entry.entry_id] = restored
            recovered = (await self.ws("info"))["result"]["calendars"][0]
        self.assertIsNone(restored.direct_source)
        self.assertIsNone(restored.current)
        self.assertIsNone((await restarted_store.async_load())["snapshot"])
        self.assertEqual(restarted_store.writes, 0)
        self.assertEqual(response, {"error": "storage_outcome_unknown"})
        self.assertTrue(stale["source_uncertain"])
        self.assertIsNone(stale["pairing_id"])
        self.assertIsNone(stale["last_success"])
        self.assertIsNone(recovered["pairing_id"])
        self.assertFalse(recovered.get("source_uncertain", False))
        self.assertEqual(await self.ws("calendar_source", **fields, confirmed=True),
                         {"result": {"state": "selected"}})
        self.assertEqual(restarted_store.writes, 0)

    async def test_rename_write_succeeds_before_readback_fails(self):
        await self.start()
        runtime = self.hass.data["horeveil.local_pairing_test"]
        invite = await runtime.registry.async_invite(administrator_id=self.admin.id, confirmed_origin=_ORIGIN)
        association = await runtime.registry.async_activate_confirmed(
            invite.invitation_id, invite.code, installation_id=uuid4(),
            credential_digest=sha256(self.secret).digest(), administrator_id=self.admin.id,
        )
        pairing_id = str(association.pairing_id)
        fields = {"pairing_id": pairing_id, "label": "Chambre"}
        load = runtime.labels.async_load

        async def fail_after_write():
            if runtime.labels.writes:
                raise OSError
            return await load()

        with patch.object(runtime.labels, "async_load", side_effect=fail_after_write):
            response = await self.ws("rename", **fields)
            unavailable = (await self.ws("info"))["result"]
        self.assertEqual((await self.ws("info"))["result"]["phone_labels"], {pairing_id: "Chambre"})
        self.assertEqual(runtime.labels.writes, 1)
        restarted = self.new_ha()
        await setup_domain(restarted, self.config())
        self.hass = restarted
        self.assertEqual((await self.ws("info"))["result"]["phone_labels"], {pairing_id: "Chambre"})
        self.assertEqual(restarted.data["horeveil.local_pairing_test"].labels.writes, 0)
        self.assertEqual(response, {"error": "storage_outcome_unknown"})
        self.assertIsNone(unavailable["phone_labels"])

    async def test_panel_publishes_only_a_server_validated_https_origin(self):
        for external, internal, expected in (
            ("https://outside.example:443/", "https://inside.example", "https://outside.example"),
            ("https://outside.example:0", "https://inside.example:8443", "https://inside.example:8443"),
            ("https://outside.example:0", "http://inside.example", None),
            ("https://user:secret@outside.example", "https://inside.example/path?token=sample", None),
            (None, None, None),
        ):
            with self.subTest(expected=expected):
                hass = self.new_ha()
                hass.config.external_url = external
                hass.config.internal_url = internal
                await setup_domain(hass, {})
                panel = hass.panels["horeveil-association"]
                self.assertTrue(panel["require_admin"])
                self.assertEqual(panel["config"], {"https_origin": expected})

    async def test_rename_and_revoke_serialize_and_recover_label_cleanup(self):
        await self.start()
        runtime = self.hass.data["horeveil.local_pairing_test"]

        class ObservableLock(asyncio.Lock):
            def __init__(self):
                super().__init__()
                self.contended = asyncio.Event()

            async def acquire(self):
                if self.locked():
                    self.contended.set()
                return await super().acquire()

        lock = runtime.labels_lock = ObservableLock()

        async def phone():
            invite = await runtime.registry.async_invite(administrator_id=self.admin.id, confirmed_origin=_ORIGIN)
            result = await runtime.registry.async_activate_confirmed(
                invite.invitation_id, invite.code, installation_id=uuid4(),
                credential_digest=sha256(self.secret).digest(), administrator_id=self.admin.id,
            )
            return str(result.pairing_id)

        pairing_id = await phone()
        entered, release = asyncio.Event(), asyncio.Event()
        save = runtime.labels.async_save

        async def delayed_save(labels):
            if labels:
                entered.set()
                await release.wait()
            await save(labels)

        with patch.object(runtime.labels, "async_save", delayed_save):
            rename = asyncio.create_task(self.ws("rename", pairing_id=pairing_id, label="Recette"))
            async with asyncio.timeout(5):
                await entered.wait()
            revoke = asyncio.create_task(self.ws("revoke", pairing_id=pairing_id, confirmed=True))
            try:
                async with asyncio.timeout(5):
                    await lock.contended.wait()
                self.assertFalse(revoke.done())
            finally:
                release.set()
                renamed, revoked = await asyncio.gather(rename, revoke)
        self.assertEqual(renamed["result"]["state"], "renamed")
        self.assertEqual(revoked["result"]["state"], "revoked")
        self.assertEqual(await runtime.labels.async_load(), {})

        pairing_id = await phone()
        self.assertIn("result", await self.ws("rename", pairing_id=pairing_id, label="Premier libellé"))
        runtime.labels.fail = True
        entered.clear()
        release.clear()
        lock.contended.clear()
        revoke_phone = runtime.registry.async_revoke

        async def delayed_revoke(*args, **kwargs):
            entered.set()
            await release.wait()
            return await revoke_phone(*args, **kwargs)

        with patch.object(runtime.registry, "async_revoke", delayed_revoke):
            revoke = asyncio.create_task(self.ws("revoke", pairing_id=pairing_id, confirmed=True))
            async with asyncio.timeout(5):
                await entered.wait()
            rename = asyncio.create_task(self.ws("rename", pairing_id=pairing_id, label="Second libellé"))
            try:
                async with asyncio.timeout(5):
                    await lock.contended.wait()
            finally:
                release.set()
                with self.assertLogs(pairing_ha_module.__name__, level="WARNING"):
                    revoked, renamed = await asyncio.gather(revoke, rename)
        self.assertEqual(revoked["result"]["state"], "revoked")
        self.assertEqual(renamed["error"], "phone_unavailable")
        self.assertEqual(await runtime.labels.async_load(), {pairing_id: "Premier libellé"})
        restarted = self.new_ha()
        await setup_domain(restarted, self.config())
        runtime = restarted.data["horeveil.local_pairing_test"]
        self.assertEqual(await runtime.labels.async_load(), {})
        self.assertEqual(await runtime.registry.async_list_active(), ())

    async def test_admin_certificate_approval_and_durable_activation(self):
        await self.start()
        self.user = None
        self.assertEqual((await self.ws("invite", origin=_ORIGIN))["error"], "unauthorized")
        self.user = SimpleNamespace(is_admin=True, is_active=False, id="owner")
        self.assertEqual((await self.ws("invite", origin=_ORIGIN))["error"], "unauthorized")
        self.user = SimpleNamespace(is_admin=False, is_active=True, id="other")
        self.assertEqual((await self.ws("invite", origin=_ORIGIN))["error"], "unauthorized")
        self.user = self.admin
        self.assertEqual((await self.ws("invite", origin="https://ha.example"))["error"], "invalid_origin")
        invited = (await self.ws("invite", origin=_ORIGIN))["result"]
        self.assertNotIn("code", invited)
        invitation_id = invited["invitation_id"]
        self.assertEqual((await self.ws("status", invitation_id=invitation_id))["result"]["state"], "issued")
        code = self.unseal(invited["code_encrypted"])
        reservation = await (await self.claim(code)).json()
        pairing_id = reservation["pairing_id"]
        status = (await self.ws("status", invitation_id=invitation_id))["result"]
        self.assertNotIn("comparison_code", status)
        self.assertNotIn("phone_label", status)
        self.assertEqual(
            self.unseal(status["comparison_code_encrypted"]),
            comparison_code(reservation, self.payload["claim_id"], self.secret),
        )
        url = self.server.make_url(f"{_BASE}/activate")

        async def activate():
            return await self.client.post(url, headers=self.auth(pairing_id), json={"confirmed": True})

        self.assertEqual((await activate()).status, 403)
        self.user = SimpleNamespace(is_admin=True, is_active=True, id="another-admin")
        arguments = {"invitation_id": invitation_id, "pairing_id": pairing_id}
        self.assertEqual((await self.ws("approve", **arguments, confirmed=True))["error"], "invitation_unavailable")
        self.user = self.admin
        self.assertEqual((await self.ws("approve", **arguments, confirmed=False))["error"], "invitation_unavailable")
        self.assertEqual((await self.ws("approve", **arguments, confirmed=True))["result"]["state"], "approved")
        for reply in await asyncio.gather(activate(), activate()):
            self.assertEqual((await reply.json())["state"], "active")
        saved = self.directory / "horeveil.pairings"
        self.assertIn(pairing_id, saved.read_text("utf-8"))
        self.assertEqual((await self.claim(code)).status, 401)

    async def test_expiry_cancellation_and_shutdown(self):
        await self.start()
        test = self.hass.data["horeveil.local_pairing_test"]
        now = 100.0
        test.registry._clock = lambda: now
        invite = (await self.ws("invite", origin=_ORIGIN))["result"]
        code = self.unseal(invite["code_encrypted"])
        now += 300
        self.assertEqual((await self.claim(code)).status, 401)
        expired = await self.ws("status", invitation_id=invite["invitation_id"])
        self.assertEqual(expired["error"], "invitation_unavailable")
        invite = (await self.ws("invite", origin=_ORIGIN))["result"]
        self.assertEqual((await self.ws("cancel", invitation_id=invite["invitation_id"]))["result"]["state"], "cancelled")
        self.assertEqual((await self.claim(self.unseal(invite["code_encrypted"]))).status, 401)
        invite = (await self.ws("invite", origin=_ORIGIN))["result"]
        await self.hass.listeners["homeassistant_stop"](None)
        self.assertFalse(test.registry._invitations)
        self.assertEqual((await self.claim(self.unseal(invite["code_encrypted"]))).status, 404)
        self.assertEqual((await self.ws("invite", origin=_ORIGIN))["error"], "unauthorized")
        restarted = self.new_ha()
        await setup_domain(restarted, self.config())
        registry = restarted.data["horeveil.local_pairing_test"].registry
        self.assertEqual(registry.server_id, test.registry.server_id)
        self.assertFalse(registry._invitations)

    async def test_admin_panel_and_address_suggestion_remain_local(self):
        await self.start()
        panel = self.hass.panels["horeveil-association"]
        self.assertIs(panel["require_admin"], True)
        self.assertEqual(panel["config_panel_domain"], "horeveil")
        self.hass.config.external_url = "https://ha.example/"
        info = (await self.ws("info"))["result"]
        self.assertEqual(info, {"suggested_origin": "https://ha.example", "test_origin": _ORIGIN, "phones": []})
        self.assertEqual((await self.ws("invite", origin=info["suggested_origin"]))["error"], "invalid_origin")
        self.hass.config.external_url = "https://private:user@ha.example"
        self.assertEqual((await self.ws("info"))["result"]["suggested_origin"], _ORIGIN)
        self.cloud = True
        self.assertEqual((await self.ws("info"))["error"], "unauthorized")
        asset_url = self.server.make_url(panel["module_url"])
        self.assertEqual((await self.client.get(asset_url)).status, 404)
        self.cloud = False
        response = await self.client.get(asset_url)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn('customElements.define("horeveil-association"', await response.text())
        self.user = SimpleNamespace(is_admin=False, is_active=True, id="other")
        self.assertEqual((await self.ws("info"))["error"], "unauthorized")
        self.assertEqual(
            (await self.ws("revoke", pairing_id=str(uuid4()), confirmed=True))["error"], "unauthorized",
        )
        self.assertEqual(
            (await self.ws("rename", pairing_id=str(uuid4()), label="Essai"))["error"], "unauthorized",
        )

    async def test_qr_module_requires_the_pinned_local_connection(self):
        await self.start()
        url = self.server.make_url("/api/horeveil/pairing-qrcode.js")
        async with ClientSession(connector=TCPConnector(ssl=self.other_tls)) as client:
            self.assertEqual((await client.get(url)).status, 404)
        self.cloud = True
        self.assertEqual((await self.client.get(url)).status, 404)
        self.cloud = False
        async with self.client.get(url) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("export { qrcodegen };", await response.text())
        self.user = SimpleNamespace(is_admin=False, is_active=True, id="other")
        self.assertEqual((await self.ws("invite", origin=_ORIGIN))["error"], "unauthorized")

    async def test_browser_key_and_admin_can_revoke_one_phone(self):
        await self.start()
        key = self.other_key
        display_key = b64(key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
        failure = await self.ws("invite", origin=_ORIGIN, display_key="invalid")
        self.assertEqual(failure["error"], "invitation_unavailable")
        invited = (await self.ws("invite", origin=_ORIGIN + "/", display_key=display_key))["result"]

        def unseal(value):
            return key.decrypt(
                urlsafe_b64decode(value + "=" * (-len(value) % 4)),
                padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
            ).decode("ascii")

        with self.assertRaises(ValueError):
            self.unseal(invited["code_encrypted"])
        reserved = await (await self.claim(unseal(invited["code_encrypted"]))).json()
        invitation_id, pairing_id = invited["invitation_id"], reserved["pairing_id"]
        status = (await self.ws("status", invitation_id=invitation_id, display_key=display_key))["result"]
        self.assertEqual(
            unseal(status["comparison_code_encrypted"]),
            comparison_code(reserved, self.payload["claim_id"], self.secret),
        )
        approved = await self.ws("approve", invitation_id=invitation_id, pairing_id=pairing_id, confirmed=True)
        self.assertEqual(approved["result"]["state"], "approved")
        response = await self.client.post(
            self.server.make_url(f"{_BASE}/activate"), headers=self.auth(pairing_id), json={"confirmed": True},
        )
        self.assertEqual((await response.json())["state"], "active")
        self.assertEqual((await self.ws("info"))["result"]["phones"], [pairing_id])
        registry = self.hass.data["horeveil.local_pairing_test"].registry
        self.assertEqual(await registry.async_list_active(signed_only=True), ())
        registry_file = self.directory / "horeveil.pairings"
        identity = registry_file.read_bytes()
        self.assertEqual(
            (await self.ws("rename", pairing_id=pairing_id, label="a" * 65))["error"], "invalid_label",
        )
        self.assertEqual(
            (await self.ws("rename", pairing_id=pairing_id, label="test\nlabel"))["error"], "invalid_label",
        )
        labels = self.hass.data["horeveil.local_pairing_test"].labels
        labels.fail = True
        self.assertEqual(
            (await self.ws("rename", pairing_id=pairing_id, label="Essai"))["error"], "storage_outcome_unknown",
        )
        labels.fail = False
        await labels.async_save({})
        labels.fail = True
        self.assertEqual(
            (await self.ws("rename", pairing_id=pairing_id, label="Essai"))["error"], "rename_unavailable",
        )
        labels.fail = False
        self.assertEqual(
            (await self.ws("rename", pairing_id=pairing_id, label="  Téléphone de recette  "))["result"],
            {"state": "renamed"},
        )
        self.assertEqual(registry_file.read_bytes(), identity)
        restarted = self.new_ha()
        await setup_domain(restarted, self.config())
        self.hass = restarted
        self.assertEqual((await self.ws("info"))["result"]["phone_labels"], {pairing_id: "Téléphone de recette"})
        url = self.server.make_url(f"{_BASE}/status")
        self.assertEqual((await self.client.get(url, headers=self.auth(pairing_id))).status, 200)
        self.assertEqual(
            (await self.ws("rename", pairing_id=pairing_id, label=""))["result"], {"state": "renamed"},
        )
        self.assertEqual((await self.ws("info"))["result"]["phone_labels"], {})
        declined = await self.ws("revoke", pairing_id=pairing_id, confirmed=False)
        self.assertEqual(declined["error"], "confirmation_required")
        result = await self.ws("revoke", pairing_id=pairing_id, confirmed=True)
        self.assertEqual(result["result"]["state"], "revoked")
        self.assertEqual((await self.ws("info"))["result"]["phones"], [])
        self.assertEqual(
            (await self.ws("rename", pairing_id=pairing_id, label="Essai"))["error"], "phone_unavailable",
        )
        response = await self.client.get(self.server.make_url(f"{_BASE}/status"), headers=self.auth(pairing_id))
        self.assertEqual(response.status, 401)
