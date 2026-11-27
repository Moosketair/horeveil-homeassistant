"""Scoped, signed requests for the native pairing profile."""

import asyncio
from base64 import urlsafe_b64decode, urlsafe_b64encode
from http import HTTPStatus
from ipaddress import ip_address
import re
from time import time
from types import SimpleNamespace

from aiohttp import web
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_der_public_key
from http_message_signatures import HTTPMessageVerifier, HTTPSignatureKeyResolver, InvalidSignature, algorithms

from .pairing import InvalidPairingCredential, PairingClockSkew
from .pairing_http import PairingHttp, _InvalidRequest, _credential, _decode_32, _read_json, _single_header, _uuid
from .receiver import SnapshotRequestError, _reply, async_read_snapshot_body

DIRECT_BASE = "/api/horeveil/direct/v1"
_COMPONENTS = ("@method", "@target-uri", "content-type", "content-digest", "horeveil-server-id")
_FIELDS = {"claim_id", "installation_id", "pairing_id", "server_id", "challenge", "public_key_spki"}
_SPKI = re.compile(r"[A-Za-z0-9_-]{122}", re.ASCII)
_DIGEST = re.compile(r"sha-256=:[A-Za-z0-9+/]{43}=:", re.ASCII)


class _Key(HTTPSignatureKeyResolver):
    def __init__(self, pairing_id, public_key):
        self.pairing_id = str(pairing_id)
        self.public_key = public_key

    def resolve_public_key(self, key_id):
        return self.public_key if key_id == self.pairing_id else None


class _Verifier(HTTPMessageVerifier):
    def validate_created_and_expires(self, sig_input, max_age=None):
        # Apply the Horéveil window below, after authenticating these parameters.
        return None


def _verify(request, body, operation, pairing_id, public_key, origin, server_id):
    try:
        key = load_der_public_key(public_key)
        if (not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1)
                or key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo) != public_key):
            raise InvalidPairingCredential("Invalid key")
        headers = {name: _single_header(request, name) for name in (
            "Content-Type", "Content-Digest", "Signature", "Signature-Input", "Horeveil-Server-Id",
        )}
        if headers["Horeveil-Server-Id"] != str(server_id) or not _DIGEST.fullmatch(headers["Content-Digest"]):
            raise InvalidPairingCredential("Invalid signed headers")
        if "," in headers["Signature"] or "," in headers["Signature-Input"]:
            raise InvalidPairingCredential("Expected one signature")
        target = f"{origin}{DIRECT_BASE}/{operation}"
        message = SimpleNamespace(method=request.method, url=target, headers=headers)
        verifier = _Verifier(signature_algorithm=algorithms.ECDSA_P256_SHA256, key_resolver=_Key(pairing_id, key))
        result, = verifier.verify(message, max_age=None, expect_tag=f"horeveil-{operation}-v1",
                                  expect_label="sig1", require_components=_COMPONENTS, body=body)
        parameters = result.parameters
        if set(parameters) != {"created", "expires", "nonce", "keyid", "tag", "alg"}:
            raise InvalidPairingCredential("Incomplete signature")
        now = int(time())
        created, expires = parameters["created"], parameters["expires"]
        nonce = _uuid(parameters["nonce"])
        if (nonce.version != 4 or type(created) is not int or type(expires) is not int
                or not created < expires <= created + 120):
            raise InvalidPairingCredential("Invalid signature window")
        if not now - 120 <= created <= now + 30 or expires < now - 30:
            raise PairingClockSkew("Signature outside the accepted window")
        return nonce, expires + 30, now
    except PairingClockSkew:
        raise
    except (InvalidSignature, UnsupportedAlgorithm, ValueError, TypeError):
        raise InvalidPairingCredential("Invalid signature") from None


def _source(request):
    try:
        return ip_address(request.remote) if request.remote else None
    except ValueError:
        return None


class SignedPairingHttp(PairingHttp):
    def __init__(self, registry):
        super().__init__(registry, local_source=_source, direct=True)

    async def calendar(self, request: web.Request, hass, find_receiver) -> web.Response:
        async def handler(request):
            if request.raw_path != f"{DIRECT_BASE}/calendar":
                raise _InvalidRequest
            pairing_id, secret = _credential(request)
            try:
                body = await async_read_snapshot_body(request)
            except SnapshotRequestError as error:
                return _reply(error.status, error.result)

            def verify(key, origin, server_id):
                return _verify(request, body, "calendar", pairing_id, key, origin, server_id)

            async def publish(association):
                receiver = await find_receiver(pairing_id)
                if receiver is None:
                    return _reply(HTTPStatus.FORBIDDEN, "source_not_authorized")
                return await receiver.async_handle_direct(body, pairing_id, association.installation_id)

            task = hass.async_create_task(self._registry.async_publish_calendar(pairing_id, secret, verify, publish))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Let the tracked task finish, and retrieve any failure without logging request data.
                await asyncio.gather(task, return_exceptions=True)
                raise

        return await self._run(request, handler)

    async def signed(self, request: web.Request, operation: str) -> web.Response:
        async def handler(request):
            if request.raw_path != f"{DIRECT_BASE}/{operation}":
                raise _InvalidRequest
            pairing_id, secret = _credential(request)
            fields = _FIELDS if operation == "enroll" else {"confirmed"} if operation == "activate" else set()
            payload, body = await _read_json(request, fields)
            enrollment = None
            if operation == "activate" and payload["confirmed"] is not True:
                raise _InvalidRequest
            if operation == "enroll":
                encoded = payload["public_key_spki"]
                if not isinstance(encoded, str) or not _SPKI.fullmatch(encoded):
                    raise _InvalidRequest
                key = urlsafe_b64decode(encoded + "==")
                if (len(key) != 91 or urlsafe_b64encode(key).decode().rstrip("=") != encoded
                        or _uuid(payload["pairing_id"]) != pairing_id
                        or _uuid(payload["server_id"]) != self._registry.server_id):
                    raise _InvalidRequest
                _decode_32(payload["challenge"])
                enrollment = (key, _uuid(payload["claim_id"]), _uuid(payload["installation_id"]), payload["challenge"])

            def verify(key, origin, server_id):
                return _verify(request, body, operation, pairing_id, key, origin, server_id)

            state = await self._registry.async_direct_request(pairing_id, secret, operation, verify, enrollment=enrollment)
            return {"pairing_id": str(pairing_id), "server_id": str(self._registry.server_id),
                    "state": state.value, "profile": "signed-v1"}

        return await self._run(request, handler)
