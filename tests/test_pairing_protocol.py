"""Exercise the unregistered pairing handlers over a test-only HTTP server."""

import asyncio
from base64 import urlsafe_b64encode
from hashlib import sha256
from hmac import digest
from ipaddress import ip_address
from pathlib import Path
from secrets import token_bytes
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid4

from aiohttp import ServerDisconnectedError, web
from aiohttp.test_utils import TestClient, TestServer

from test_receiver import DiskStore, pairing_http_module, pairing_module

PairingRegistry = pairing_module.PairingRegistry
PairingHttp = pairing_http_module.PairingHttp
InvalidPairingCredential = pairing_module.InvalidPairingCredential
_BASE = "/api/horeveil/v1/pair"


def b64(data):
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def comparison_code(reservation, claim_id, secret):
    message = "|".join((
        "horeveil-pair-v1", reservation["server_id"], reservation["pairing_id"],
        claim_id, reservation["challenge"],
    ))
    mac = digest(sha256(secret).digest(), message.encode("ascii"), "sha256")
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    value = int.from_bytes(mac[:5], "big")
    code = ""
    for _ in range(8):
        value, digit = divmod(value, 32)
        code = alphabet[digit] + code
    return code


class PairingProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(TemporaryDirectory(dir=Path(__file__).resolve().parents[1]))
        self.hass = SimpleNamespace(directory=Path(directory))
        self.now = 100.0
        self.local_source = ip_address("192.168.1.10")
        self.secure = True
        self.disconnect = False
        self.admin = SimpleNamespace(id="admin", is_active=True, is_admin=True)
        self.secret = token_bytes(32)
        self.payload = {
            "claim_id": str(uuid4()),
            "installation_id": str(uuid4()),
            "phone_label": "Téléphone de test",
            "credential_digest": b64(sha256(self.secret).digest()),
        }
        await self.restart()
        app = web.Application()
        app.router.add_post(f"{_BASE}/claim", self.dispatch)
        app.router.add_get(f"{_BASE}/status", self.dispatch, allow_head=False)
        app.router.add_post(f"{_BASE}/activate", self.dispatch)
        self.client = await self.enterAsyncContext(TestClient(TestServer(app)))

    async def restart(self):
        self.store = DiskStore(self.hass, 1, "pairings")
        guard = DiskStore(self.hass, 1, "guard")
        self.registry = PairingRegistry(self.store, guard, clock=lambda: self.now)
        await self.registry.async_load()
        self.http = PairingHttp(
            self.registry, local_source=lambda _: self.local_source, clock=lambda: self.now,
        )

    async def dispatch(self, request):
        # Exercise handler policy without configuring TLS on the loopback server.
        if self.secure:
            request = request.clone(scheme="https")
        endpoint = request.path.rsplit("/", 1)[1]
        response = await getattr(self.http, endpoint)(request)
        if self.disconnect:
            self.disconnect = False
            request.transport.close()
        return response

    async def invite(self, *, administrator_id="admin"):
        return await self.registry.async_invite(
            administrator_id=administrator_id, confirmed_origin="https://ha.example",
        )

    async def claim(self, invite, *, payload=None, code=None, status=200):
        response = await self.client.post(
            f"{_BASE}/claim", headers={"X-Horeveil-Invitation": code or invite.code},
            json=self.payload if payload is None else payload,
        )
        self.assertEqual(response.status, status)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return await response.json()

    def auth(self, reservation, secret=None):
        return {"X-Horeveil-Authorization": f"v1 {reservation['pairing_id']}.{b64(secret or self.secret)}"}

    async def status(self, reservation, *, secret=None, status=200):
        response = await self.client.get(f"{_BASE}/status", headers=self.auth(reservation, secret))
        self.assertEqual(response.status, status)
        return await response.json()

    async def activate(self, reservation, *, confirmed=True, status=200):
        response = await self.client.post(
            f"{_BASE}/activate", headers=self.auth(reservation), json={"confirmed": confirmed},
        )
        self.assertEqual(response.status, status)
        return await response.json()

    async def approve(self, invite, reservation):
        review = await self.registry.async_review_claim(self.admin, invite.invitation_id)
        self.assertEqual(review.pairing_id, UUID(reservation["pairing_id"]))
        self.assertEqual(review.phone_label, self.payload["phone_label"])
        self.assertEqual(review.origin, invite.origin)
        code = comparison_code(reservation, self.payload["claim_id"], self.secret)
        self.assertEqual(review.comparison_code, code)
        await self.registry.async_approve_claim(self.admin, review.pairing_id, code)

    async def test_exchange_and_persistence(self):
        invite = await self.invite()
        self.assertIsNone(await self.registry.async_review_claim(self.admin, invite.invitation_id))
        reserved = await self.claim(invite)
        self.assertEqual(reserved["state"], "awaiting_approval")
        self.assertEqual(reserved["server_id"], str(self.registry.server_id))
        self.assertEqual((await self.status(reserved))["state"], "awaiting_approval")
        await self.approve(invite, reserved)
        self.assertEqual((await self.status(reserved))["state"], "approved")
        writes = self.store.writes
        activated, retry = await asyncio.gather(self.activate(reserved), self.activate(reserved))
        self.assertEqual(activated, retry)
        self.assertEqual(activated["state"], "active")
        self.assertEqual(self.store.writes, writes + 1)
        data = await self.store.async_load()
        self.assertEqual(len(data["associations"]), 1)
        stored = data["associations"][0]
        self.assertEqual(stored["pairing_id"], reserved["pairing_id"])
        self.assertEqual(stored["credential_digest"], sha256(self.secret).hexdigest())
        self.assertNotIn(self.payload["phone_label"], str(data))
        self.assertNotIn(invite.code, str(data))
        await self.claim(invite, status=401)

    async def test_expiration_and_cancellation_invalidate_approved_claim(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                invite = await self.invite()
                reserved = await self.claim(invite)
                await self.approve(invite, reserved)
                if cancel:
                    await self.registry.async_cancel(invite.invitation_id, administrator_id="admin")
                else:
                    self.now += 300
                await self.activate(reserved, status=401)
                await self.status(reserved, status=401)
                await self.claim(invite, status=401)

    async def test_authenticated_admin_and_phone_confirmation_required(self):
        invite = await self.invite()
        reserved = await self.claim(invite)
        await self.activate(reserved, status=403)
        await self.activate(reserved, confirmed=False, status=400)
        pairing_id = UUID(reserved["pairing_id"])
        code = comparison_code(reserved, self.payload["claim_id"], self.secret)
        for user in (
            None,
            SimpleNamespace(id="admin", is_active=True, is_admin=False),
            SimpleNamespace(id="admin", is_active=False, is_admin=True),
            SimpleNamespace(id="other", is_active=True, is_admin=True),
        ):
            with self.subTest(user=user), self.assertRaises(PermissionError):
                await self.registry.async_approve_claim(user, pairing_id, code)
        wrong_code = ("0" if code[0] != "0" else "1") + code[1:]
        with self.assertRaises(InvalidPairingCredential):
            await self.registry.async_approve_claim(self.admin, pairing_id, wrong_code)
        await self.activate(reserved, status=403)
        await self.approve(invite, reserved)
        await self.activate(reserved, confirmed=1, status=400)
        self.assertEqual((await self.status(reserved))["state"], "approved")

    async def test_wrong_credentials_and_attempt_limits(self):
        invite = await self.invite()
        changed = ("0" if invite.code[0] != "0" else "1") + invite.code[1:]
        await self.claim(invite, code=changed, status=401)
        reserved = await self.claim(invite)
        await self.status(reserved, secret=sha256(self.secret).digest(), status=401)
        for _ in range(27):
            await self.claim(invite, code=changed, status=401)
        response = await self.client.get(f"{_BASE}/status", headers=self.auth(reserved))
        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers["Retry-After"], "60")
        self.now += 60
        await self.status(reserved)

    async def test_global_attempt_limit_across_sources(self):
        invite = await self.invite()
        for source in range(1, 5):
            self.local_source = ip_address(f"192.168.1.{source}")
            for _ in range(30):
                await self.claim(invite, code="invalid", status=401)
        self.local_source = ip_address("192.168.1.5")
        await self.claim(invite, status=429)

    async def test_competing_claims_and_installation_conflict(self):
        invite = await self.invite()
        other = {**self.payload, "claim_id": str(uuid4())}
        requests = [
            self.client.post(f"{_BASE}/claim", headers={"X-Horeveil-Invitation": invite.code}, json=body)
            for body in (self.payload, other)
        ]
        responses = await asyncio.gather(*requests)
        self.assertCountEqual([response.status for response in responses], [200, 409])
        winning_index = next(index for index, response in enumerate(responses) if response.status == 200)
        winning_payload = (self.payload, other)[winning_index]
        self.assertEqual(await self.claim(invite, payload=winning_payload), await responses[winning_index].json())
        await self.claim(invite, payload={**winning_payload, "credential_digest": b64(token_bytes(32))}, status=409)
        await self.claim(await self.invite(administrator_id="other"), status=409)

    async def test_lost_responses_and_restart(self):
        invite = await self.invite()
        self.disconnect = True
        with self.assertRaises(ServerDisconnectedError):
            await self.claim(invite)
        reservation = await self.claim(invite)
        self.assertEqual(reservation, await self.claim(invite))
        await self.approve(invite, reservation)
        self.disconnect = True
        with self.assertRaises(ServerDisconnectedError):
            await self.activate(reservation)
        await self.restart()
        self.assertEqual((await self.status(reservation))["state"], "active")
        self.assertEqual((await self.activate(reservation))["state"], "active")
        self.assertEqual(self.store.writes, 0)
        await self.registry.async_revoke(UUID(reservation["pairing_id"]))
        await self.status(reservation, status=401)

    async def test_restart_discards_unfinished_approval(self):
        invite = await self.invite()
        reservation = await self.claim(invite)
        await self.approve(invite, reservation)
        await self.restart()
        await self.status(reservation, status=401)
        await self.activate(reservation, status=401)
        await self.claim(invite, status=401)

    async def test_disk_failure_does_not_confirm_activation(self):
        invite = await self.invite()
        reservation = await self.claim(invite)
        await self.approve(invite, reservation)
        self.store.fail = True
        failure = await self.activate(reservation, status=503)
        self.assertEqual(failure, {"error": "pairing_unavailable"})
        self.assertEqual((await self.store.async_load())["associations"], [])
        await self.restart()
        await self.status(reservation, status=401)

    async def test_refuses_untrusted_or_plain_http_ingress(self):
        invite = await self.invite()
        self.secure = False
        await self.claim(invite, status=404)
        self.secure = True
        local = self.local_source
        self.local_source = None
        response = await self.client.post(
            f"{_BASE}/claim", json=self.payload,
            headers={"X-Horeveil-Invitation": invite.code, "X-Forwarded-For": str(local)},
        )
        self.assertEqual(response.status, 404)
        self.http = PairingHttp(self.registry)
        await self.claim(invite, status=404)
        self.assertIsNone(await self.registry.async_review_claim(self.admin, invite.invitation_id))

    async def test_request_validation(self):
        invite = await self.invite()
        headers = {"X-Horeveil-Invitation": invite.code, "Content-Type": "application/json"}
        for body, status in ((b"x" * 4097, 413), (b'{"claim_id":0,"claim_id":0}', 400)):
            response = await self.client.post(f"{_BASE}/claim", data=body, headers=headers)
            self.assertEqual(response.status, status)
        await self.claim(invite, payload={**self.payload, "phone_label": "x" * 65}, status=400)
        response = await self.client.post(
            f"{_BASE}/claim", json=self.payload,
            headers=[("X-Horeveil-Invitation", invite.code)] * 2,
        )
        self.assertEqual(response.status, 401)
