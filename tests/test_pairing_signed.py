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
from test_receiver import DiskStore, pairing_module, pairing_signed_module, receiver_module

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
        self.hass = SimpleNamespace(directory=Path(directory), is_stopping=False, listeners={},
                                    async_add_executor_job=asyncio.to_thread, async_create_task=asyncio.create_task)
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
        self.metadata = pairing_signed_module.PhoneMetadataStore(self.hass)
        self.http = SignedPairingHttp(self.registry, self.metadata)
        self.calendar_store = DiskStore(self.hass, 1, "calendar")
        self.receiver = receiver_module.SnapshotReceiver(self.hass, self.calendar_store, "test", "test")
        await self.receiver.async_restore()

    async def dispatch(self, request):
        request = request.clone(scheme="https")
        operation = request.match_info["operation"]
        if operation == "claim":
            return await self.http.claim(request)
        if operation == "calendar":
            async def find_receiver(_):
                return self.receiver

            return await self.http.calendar(request, self.hass, find_receiver)
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
        payload, _headers = await self.send_response(operation, prepared, expected)
        return payload

    async def send_response(self, operation, prepared, expected):
        body, headers = prepared
        async with self.client.post(f"{DIRECT_BASE}/{operation}", data=body, headers=headers) as response:
            payload = await response.json()
            self.assertEqual(response.status, expected, (operation, payload))
            return payload, response.headers

    async def approve(self, invitation):
        review = await self.registry.async_review_claim(self.admin, invitation.invitation_id)
        await self.registry.async_approve_claim(self.admin, review.pairing_id, review.comparison_code)

    async def activate_calendar_phone(self):
        invitation, data, reply = await self.reserve()
        await self.send("enroll", self.prepare("enroll", reply, self.enroll_body(data, reply)), 200)
        await self.approve(invitation)
        await self.send("activate", self.prepare("activate", reply, {"confirmed": True}), 200)
        return data["installation_id"], reply

    def calendar_payload(self, installation_id, revision=1):
        now = datetime.now(UTC)
        return {
            "schema_version": 1, "event": "calendar_snapshot", "installation_id": installation_id,
            "revision": revision, "generated_at": now.isoformat(), "window_start": now.isoformat(),
            "window_end_exclusive": (now + timedelta(days=30)).isoformat(), "timezone": "UTC",
            "expires_at": (now + timedelta(hours=1)).isoformat(), "occurrences": [], "alarms": {},
        }

    async def test_calendar_source_signature_duplicate_and_restart(self):
        installation, reply = await self.activate_calendar_phone()
        pairing_id = UUID(reply["pairing_id"])
        self.assertEqual(await self.registry.async_list_active(signed_only=True), (pairing_id,))
        payload = self.calendar_payload(installation)

        async def publish(status, data=payload):
            return await self.send("calendar", self.prepare("calendar", reply, data), status)

        self.assertEqual((await publish(403))["result"], "source_not_authorized")
        await self.registry.async_select_calendar_source(pairing_id, self.receiver.async_select_source)
        self.assertEqual((await publish(403, self.calendar_payload(str(uuid4()))))["result"], "different_installation")
        body, headers = self.prepare("calendar", reply, payload)
        await self.send("calendar", (body + b" ", headers), 401)
        self.assertEqual((await self.send("calendar", (body, headers), 200))["result"], "accepted")
        self.assertEqual((await self.calendar_store.async_load())["snapshot"], body.decode())
        await self.restart()
        self.assertEqual(self.receiver.direct_source, pairing_id)
        await self.send("calendar", (body, headers), 401)
        self.assertEqual((await publish(200))["result"], "duplicate")
        self.assertEqual(self.calendar_store.writes, 0)
        await self.registry.async_select_calendar_source(None, self.receiver.async_select_source)
        self.assertIsNone(self.receiver.current)
        await self.registry.async_select_calendar_source(pairing_id, self.receiver.async_select_source)
        self.assertEqual((await publish(409))["result"], "old_revision")
        self.assertEqual((await publish(200, payload | {"revision": 2}))["result"], "accepted")

    async def test_calendar_acknowledgment_and_revocation_wait_for_storage(self):
        installation, reply = await self.activate_calendar_phone()
        pairing_id = UUID(reply["pairing_id"])
        payload = self.calendar_payload(installation)
        await self.registry.async_select_calendar_source(pairing_id, self.receiver.async_select_source)
        self.calendar_store.fail = True
        self.assertEqual(
            (await self.send("calendar", self.prepare("calendar", reply, payload), 503))["result"], "storage_unavailable"
        )
        self.assertIsNone(self.receiver.current)
        self.calendar_store.fail = False
        entered, release = asyncio.Event(), asyncio.Event()
        save = self.calendar_store.async_save

        async def blocked_save(data):
            entered.set()
            await release.wait()
            await save(data)

        self.calendar_store.async_save = blocked_save
        publish = asyncio.create_task(self.send("calendar", self.prepare("calendar", reply, payload), 200))
        await asyncio.wait_for(entered.wait(), 5)

        async def purge(source):
            self.assertTrue(await self.receiver.async_select_source(None, forget=source))

        revoke = asyncio.create_task(self.registry.async_revoke(pairing_id, purge=purge))
        await asyncio.sleep(0)
        self.assertFalse(publish.done())
        self.assertFalse(revoke.done())
        release.set()
        self.assertEqual((await publish)["result"], "accepted")
        self.assertTrue(await revoke)
        self.assertIsNone((await self.calendar_store.async_load())["snapshot"])
        self.assertIsNone(self.receiver.current)
        await self.restart()
        await self.send("calendar", self.prepare("calendar", reply, payload), 401)
        self.assertEqual(self.receiver.direct_source, pairing_id)

    async def test_calendar_metadata_updates_only_after_authorized_commit_or_exact_retry(self):
        installation, reply = await self.activate_calendar_phone()
        phone = UUID(reply["pairing_id"])
        payload = self.calendar_payload(installation)
        payload["occurrences"] = [{
            "occurrence_id": "3a93297a-3f55-4ff3-8439-e3767f8b9b43/" + payload["window_start"][:10],
            "alarm_id": "3a93297a-3f55-4ff3-8439-e3767f8b9b43",
            "nominal_date": payload["window_start"][:10],
            "sound_start": payload["window_start"],
        }]
        payload["alarms"] = {payload["occurrences"][0]["alarm_id"]: {"name": "Réveil"}}

        async def publish(data, status=200):
            return (await self.send("calendar", self.prepare("calendar", reply, data), status))["result"]

        await self.registry.async_select_calendar_source(phone, self.receiver.async_select_source)
        self.assertEqual(await publish(payload), "accepted")
        self.assertEqual(await self.metadata.async_read([phone]), {})
        expected_projection = tuple(self.receiver.current.iter_calendar_occurrences())
        enriched = payload | {"revision": 2, "phone_metadata": {
            "manufacturer": "é" * 32, "model": "<b>Modèle</b>", "app_version": "é" * 25,
            "android_version": 17, "serial": "ignoré",
        }}
        self.metadata._store.fail = True
        with self.assertLogs(self.metadata.__module__, level="WARNING"):
            self.assertEqual(await publish(enriched), "accepted")
        self.metadata._store.fail = False
        self.assertEqual(await self.metadata.async_read([phone]), {})
        self.assertEqual(await publish(dict(reversed(list(enriched.items())))), "duplicate")
        self.assertEqual(await self.metadata.async_read([phone]), {})
        self.assertEqual(await publish(enriched), "duplicate")
        expected = {"manufacturer": "é" * 32, "model": "<b>Modèle</b>"}
        self.assertEqual(await self.metadata.async_read([phone]), {str(phone): expected})
        self.assertEqual(tuple(self.receiver.current.iter_calendar_occurrences()), expected_projection)
        self.assertEqual(self.calendar_store.writes, 3)  # Source selection, then two accepted calendars.

        newer = enriched | {"revision": 3, "phone_metadata": {
            "app_version": " 2.7.5 ", "android_version": "17", "manufacturer": "unknown",
            "model": "MAUVAIS\u0000", "ignored": "inutile",
        }}
        self.assertEqual(await publish(newer), "accepted")
        expected |= {"app_version": "2.7.5", "android_version": "17"}
        self.assertEqual(await publish(enriched, 409), "old_revision")
        self.assertEqual(await publish(newer | {"phone_metadata": {"model": "Conflit"}}, 409), "revision_conflict")
        self.assertEqual(await publish(newer | {"installation_id": str(uuid4()), "revision": 4}, 403),
                         "different_installation")
        self.assertEqual(await self.metadata.async_read([phone]), {str(phone): expected})
        await self.restart()
        self.assertEqual(await self.metadata.async_read([phone]), {str(phone): expected})
        self.assertEqual(tuple(self.receiver.current.iter_calendar_occurrences()), expected_projection)

    async def test_manual_metadata_is_active_only_and_orders_retried_calendars(self):
        invitation, data, reply = await self.reserve()
        phone = UUID(reply["pairing_id"])
        extension = {"calendar_revision": 2, "phone_metadata": {"app_version": "2.7.5"}}
        await self.send("enroll", self.prepare("enroll", reply, self.enroll_body(data, reply)), 200)
        pending, headers = await self.send_response("status", self.prepare("status", reply, {}), 200)
        self.assertNotEqual(pending["state"], "active")
        self.assertNotIn("Horeveil-Phone-Metadata", headers)
        await self.send("status", self.prepare("status", reply, extension), 401)
        await self.approve(invitation)
        await self.send("activate", self.prepare("activate", reply, {"confirmed": True}), 200)
        historic, headers = await self.send_response("status", self.prepare("status", reply, {}), 200)
        self.assertEqual(historic, {
            "pairing_id": str(phone), "server_id": reply["server_id"], "state": "active", "profile": "signed-v1",
        })
        self.assertEqual(headers["Horeveil-Phone-Metadata"], "1")
        self.assertNotIn("Horeveil-Phone-Metadata-Result", headers)

        old = self.calendar_payload(data["installation_id"], revision=2)
        old["phone_metadata"] = {"app_version": "2.7.4", "model": "Ancien"}
        prepared_status = self.prepare("status", reply, extension)
        await self.send("calendar", prepared_status, 401)
        await self.send("status", self.prepare("status", reply, extension | {"occurrences": []}), 400)
        await self.send("status", self.prepare("status", reply, extension | {"calendar_revision": True}), 400)
        await self.send("status", self.prepare("status", reply, extension, key=ec.generate_private_key(ec.SECP256R1())), 401)
        state, headers = await self.send_response("status", prepared_status, 200)
        self.assertEqual(state, historic)
        self.assertEqual(headers["Horeveil-Phone-Metadata-Result"], "stored")
        self.assertIsNone(self.receiver.direct_source)
        self.assertIsNone(self.receiver.current)
        await self.send("status", prepared_status, 401)
        await self.send("calendar", self.prepare("calendar", reply, old), 403)

        await self.registry.async_select_calendar_source(phone, self.receiver.async_select_source)
        await self.send("calendar", self.prepare("calendar", reply, old), 200)
        self.assertEqual(await self.metadata.async_read([phone]), {str(phone): {"app_version": "2.7.5"}})
        newer = old | {"revision": 3, "phone_metadata": {"app_version": "2.7.6"}}
        await self.send("calendar", self.prepare("calendar", reply, newer), 200)
        _, headers = await self.send_response("status", self.prepare("status", reply, extension), 200)
        self.assertEqual(headers["Horeveil-Phone-Metadata-Result"], "ignored")
        self.assertEqual(await self.metadata.async_read([phone]), {str(phone): {"app_version": "2.7.6"}})
        self.assertTrue(await self.registry.async_revoke(phone))
        await self.send("status", self.prepare("status", reply, extension | {"calendar_revision": 4}), 401)
        self.assertEqual(await self.metadata.async_read(await self.registry.async_list_active()), {})

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
