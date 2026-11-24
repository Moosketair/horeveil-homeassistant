"""Test native registration and socket policy using HA doubles and real mTLS."""

import asyncio
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from ipaddress import IPv4Address
from pathlib import Path
from secrets import token_bytes
import ssl
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from aiohttp import ClientConnectionError, ClientSession, TCPConnector, web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from test_pairing_protocol import comparison_code
from test_receiver import current_request, is_cloud_request, pairing_ha_module, setup_domain

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
            if name == "pinned":
                cls.pinned_key, cls.fingerprint = key, fingerprint
        cls.anonymous_tls = ssl.create_default_context(cafile=cafile)

    async def asyncSetUp(self):
        self.directory = Path(self.enterContext(
            TemporaryDirectory(dir=Path(__file__).resolve().parents[1]),
        ))
        self.enterContext(patch.dict(
            "sys.modules", {"custom_components.horeveil.pairing_ha": pairing_ha_module},
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
            config=SimpleNamespace(path=self.directory.joinpath), data={},
            ws_commands={}, views=views, listeners=listeners,
            bus=SimpleNamespace(async_listen_once=listeners.__setitem__),
        )

    def config(self):
        return {"horeveil": {"local_pairing_test_client_sha256": self.fingerprint}}

    async def start(self):
        self.hass = self.new_ha()
        await setup_domain(self.hass, self.config())
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

        app.router.add_get("/api/websocket", dispatch_ws)
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

    async def test_registration_refuses_default_and_unsafe_server(self):
        hass = self.new_ha()
        await setup_domain(hass, {})
        self.assertEqual(hass.views, {})
        self.assertEqual(hass.ws_commands, {})
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
