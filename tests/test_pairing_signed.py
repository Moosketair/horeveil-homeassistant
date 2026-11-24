"""Exercise the RFC 9421 policy and persistent signed pairing registry."""

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from secrets import token_bytes
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid4

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from http_message_signatures import HTTPMessageSigner, HTTPSignatureKeyResolver, algorithms

from test_pairing_protocol import b64
from test_receiver import DiskStore, pairing_module, pairing_signed_module

ORIGIN = "https://ha.example"
DIRECT_BASE = pairing_signed_module.DIRECT_BASE
SignedPairingHttp = pairing_signed_module.SignedPairingHttp


class KeyResolver(HTTPSignatureKeyResolver):
    def __init__(self, key):
        self.key = key

    def resolve_private_key(self, key_id):
        return self.key


def signed_headers(key, origin, operation, pairing_id, server_id, secret, body, *, now=None, nonce=None):
    now = now or datetime.now(UTC)
    target = SimpleNamespace(method="POST", url=f"{origin}{DIRECT_BASE}/{operation}", headers={
        "Content-Type": "application/json", "Horeveil-Server-Id": server_id,
        "X-Horeveil-Authorization": f"v1 {pairing_id}.{b64(secret)}",
    })
    signer = HTTPMessageSigner(signature_algorithm=algorithms.ECDSA_P256_SHA256, key_resolver=KeyResolver(key))
    signer.sign(target, key_id=pairing_id, created=now, expires=now + timedelta(seconds=120),
                nonce=str(nonce or uuid4()), label="sig1", tag=f"horeveil-{operation}-v1",
                covered_component_ids=("@method", "@target-uri", "content-type", "content-digest", "horeveil-server-id"), body=body)
    return target.headers


class SignedPairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(TemporaryDirectory(dir=Path(__file__).resolve().parents[1]))
        self.hass = SimpleNamespace(directory=Path(directory))
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.secret = token_bytes(32)
        self.admin = SimpleNamespace(id="test", is_admin=True, is_active=True)
        self.monotonic = 1
        await self.restart()
        app = web.Application()
        app.router.add_post(f"{DIRECT_BASE}/{{operation}}", self.dispatch)
        self.client = await self.enterAsyncContext(TestClient(TestServer(app)))

    async def restart(self):
        stores = [DiskStore(self.hass, 1, name) for name in ("registry", "guard", "replay")]
        self.registry = pairing_module.PairingRegistry(*stores[:2], replay=stores[2], clock=lambda: self.monotonic)
        await self.registry.async_load()
        self.http = SignedPairingHttp(self.registry)

    async def dispatch(self, request):
        request = request.clone(scheme="https")
        operation = request.match_info["operation"]
        if operation == "claim":
            return await self.http.claim(request)
        return await self.http.signed(request, operation)

    async def reserve(self, *, direct=True):
        invitation = await self.registry.async_invite(administrator_id="test", confirmed_origin=ORIGIN, direct=direct)
        data = {"claim_id": str(uuid4()), "installation_id": str(uuid4()), "phone_label": "Test",
                "credential_digest": b64(sha256(self.secret).digest())}
        reply = await self.client.post(f"{DIRECT_BASE}/claim", json=data, headers={"X-Horeveil-Invitation": invitation.code})
        self.assertEqual(reply.status, 200 if direct else 401)
        if not direct:
            return invitation, data, {}
        response = await reply.json()
        self.assertEqual(response["profile"], "signed-v1")
        return invitation, data, response

    def enroll_body(self, data, reply, key=None):
        public_key = (key or self.key).public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        return {"claim_id": data["claim_id"], "installation_id": data["installation_id"],
                "pairing_id": reply["pairing_id"], "server_id": reply["server_id"], "challenge": reply["challenge"],
                "public_key_spki": b64(public_key)}

    def prepare(self, operation, reply, payload, *, key=None, origin=ORIGIN, now=None):
        body = json.dumps(payload, separators=(",", ":")).encode()
        return body, signed_headers(key or self.key, origin, operation, reply["pairing_id"], reply["server_id"], self.secret, body, now=now)

    async def send(self, operation, prepared, expected):
        body, headers = prepared
        async with self.client.post(f"{DIRECT_BASE}/{operation}", data=body, headers=headers) as response:
            self.assertEqual(response.status, expected, (operation, await response.json()))
            return await response.json()

    async def approve(self, invitation):
        review = await self.registry.async_review_claim(self.admin, invitation.invitation_id)
        await self.registry.async_approve_claim(self.admin, review.pairing_id, review.comparison_code)

    async def test_end_to_end_requires_same_key_and_replay_survives_restart(self):
        invitation, data, reply = await self.reserve()
        self.assertIsNone(await self.registry.async_review_claim(self.admin, invitation.invitation_id))
        await self.send("activate", self.prepare("activate", reply, {"confirmed": True}), 401)
        enroll = self.prepare("enroll", reply, self.enroll_body(data, reply))
        statuses = await asyncio.gather(*[
            self.client.post(f"{DIRECT_BASE}/enroll", data=enroll[0], headers=enroll[1]) for _ in range(2)
        ])
        self.assertEqual(sorted(response.status for response in statuses), [200, 401])
        await asyncio.gather(*(response.read() for response in statuses))
        await self.send("activate", self.prepare("activate", reply, {"confirmed": True}), 403)
        await self.approve(invitation)
        other = ec.generate_private_key(ec.SECP256R1())
        await self.send("enroll", self.prepare("enroll", reply, self.enroll_body(data, reply, other), key=other), 401)
        await self.send("activate", self.prepare("activate", reply, {"confirmed": True}, key=other), 401)
        activation = self.prepare("activate", reply, {"confirmed": True})
        self.assertEqual((await self.send("activate", activation, 200))["state"], "active")
        await self.restart()
        await self.send("activate", activation, 401)
        self.assertEqual((await self.send("activate", self.prepare("activate", reply, {"confirmed": True}), 200))["state"], "active")
        entry = self.registry._associations[UUID(reply["pairing_id"])]
        self.assertEqual(entry.ha_permissions, pairing_module.Permissions())
        self.assertEqual(entry.phone_permissions, pairing_module.Permissions())
        self.assertIsNone(await self.registry.async_authenticate(entry.pairing_id, self.secret))
        self.assertTrue(await self.registry.async_revoke(entry.pairing_id))
        await self.restart()
        await self.send("status", self.prepare("status", reply, {}), 401)
        await self.send("activate", self.prepare("activate", reply, {"confirmed": True}), 401)
        replacement = await self.registry.async_invite(administrator_id="test", confirmed_origin=ORIGIN, direct=True)
        fresh = await self.client.post(f"{DIRECT_BASE}/claim", json=data, headers={"X-Horeveil-Invitation": replacement.code})
        self.assertEqual(fresh.status, 200)
        self.assertNotEqual((await fresh.json())["pairing_id"], reply["pairing_id"])

    async def test_two_installations_cannot_reserve_the_same_qr(self):
        invitation = await self.registry.async_invite(administrator_id="test", confirmed_origin=ORIGIN, direct=True)

        async def claim():
            data = {"claim_id": str(uuid4()), "installation_id": str(uuid4()), "phone_label": "Test",
                    "credential_digest": b64(sha256(token_bytes(32)).digest())}
            async with self.client.post(f"{DIRECT_BASE}/claim", json=data,
                                        headers={"X-Horeveil-Invitation": invitation.code}) as response:
                return response.status

        self.assertEqual(sorted(await asyncio.gather(claim(), claim())), [200, 409])

    async def test_tampering_clock_expiry_and_cancel(self):
        await self.reserve(direct=False)
        await self.registry.async_discard_invitations()
        invitation, data, reply = await self.reserve()
        body = self.enroll_body(data, reply)
        await self.send("enroll", self.prepare("enroll", reply, body, origin="https://different.example"), 401)
        await self.send("enroll", self.prepare("enroll", reply, body, now=datetime.now(UTC) + timedelta(seconds=40)), 422)
        await self.send("enroll", self.prepare("enroll", reply, body | {"challenge": b64(token_bytes(32))}), 401)
        raw, headers = self.prepare("enroll", reply, body)
        await self.send("enroll", (raw + b" ", headers), 401)
        await self.send("enroll", self.prepare("enroll", reply, body), 200)
        await self.send("cancel", self.prepare("cancel", reply, {}), 200)
        self.assertFalse(await self.registry.async_cancel(invitation.invitation_id, administrator_id="test"))
        await self.send("status", self.prepare("status", reply, {}), 401)
        _, data, reply = await self.reserve()
        self.monotonic += 301
        await self.send("enroll", self.prepare("enroll", reply, self.enroll_body(data, reply)), 401)

    async def test_full_registry_does_not_evict_live_nonces_and_missing_state_fails_closed(self):
        invitation, data, reply = await self.reserve()
        await self.send("enroll", self.prepare("enroll", reply, self.enroll_body(data, reply)), 200)
        await self.approve(invitation)
        await self.send("activate", self.prepare("activate", reply, {"confirmed": True}), 200)
        replay = self.registry._replay
        saved = await replay.async_load()
        deadline = saved["time"] + 100
        for _ in range(64 - len(saved["nonces"])):
            saved["nonces"][reply["pairing_id"] + "/" + str(uuid4())] = deadline
        await replay.async_save(saved)
        await self.send("status", self.prepare("status", reply, {}), 429)
        self.assertEqual(await replay.async_load(), saved)
        replay.path.unlink()
        await self.restart()
        await self.send("status", self.prepare("status", reply, {}), 503)
