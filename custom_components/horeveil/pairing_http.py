"""Unregistered HTTP handlers for the direct pairing protocol."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Awaitable, Callable
from ipaddress import IPv4Address, IPv6Address
import json
from math import ceil
import re
from time import monotonic
from uuid import UUID

from aiohttp import web
from homeassistant.exceptions import HomeAssistantError

from .pairing import (
    InvalidPairingCredential,
    PairingApprovalRequired,
    PairingClaim,
    PairingConflict,
    PairingError,
    PairingRegistry,
)

_INVITATION_HEADER = "X-Horeveil-Invitation"
_AUTH_HEADER = "X-Horeveil-Authorization"
_MAX_BODY = 4096
_WINDOW_SECONDS = 60
_PER_SOURCE_LIMIT = 30
_TOTAL_LIMIT = 120
_BASE64URL_32 = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)

type Source = IPv4Address | IPv6Address


class _InvalidRequest(ValueError):
    """A malformed request whose content must not be echoed."""


class _BodyTooLarge(_InvalidRequest):
    """A request body over the size limit."""


def _single_header(request: web.Request, name: str) -> str:
    values = request.headers.getall(name, ())
    if len(values) != 1:
        raise InvalidPairingCredential("Invalid credential header")
    return values[0]


def _uuid(value: object) -> UUID:
    if not isinstance(value, str) or len(value) != 36:
        raise _InvalidRequest
    try:
        parsed = UUID(value)
    except ValueError:
        raise _InvalidRequest from None
    if str(parsed) != value:
        raise _InvalidRequest
    return parsed


def _decode_32(value: object) -> bytes:
    if not isinstance(value, str) or not _BASE64URL_32.fullmatch(value):
        raise _InvalidRequest
    decoded = urlsafe_b64decode(value + "=")
    if urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value:
        raise _InvalidRequest
    return decoded


def _credential(request: web.Request) -> tuple[UUID, bytes]:
    header = _single_header(request, _AUTH_HEADER)
    version, _, token = header.partition(" ")
    pairing_id, _, raw_secret = token.partition(".")
    try:
        if version != "v1":
            raise _InvalidRequest
        return _uuid(pairing_id), _decode_32(raw_secret)
    except ValueError:
        raise InvalidPairingCredential("Invalid pairing credential") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise _InvalidRequest
    return result


async def _json_body(request: web.Request, fields: set[str]) -> dict:
    if request.content_type != "application/json" or "Content-Encoding" in request.headers:
        raise _InvalidRequest
    if request.content_length is not None and request.content_length > _MAX_BODY:
        raise _BodyTooLarge
    body = bytearray()
    async for chunk in request.content.iter_chunked(_MAX_BODY + 1):
        if len(body) + len(chunk) > _MAX_BODY:
            raise _BodyTooLarge
        body.extend(chunk)
    payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(payload, dict) or payload.keys() != fields:
        raise _InvalidRequest
    return payload


def _response(payload: dict, status: int = 200, *, retry_after: int | None = None) -> web.Response:
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return web.json_response(payload, status=status, headers=headers)


class PairingHttp:
    """HTTP adapter with no registration and a default-deny ingress policy.

    A future caller must attest locality from trusted ingress information.
    Forwarding headers and Host supplied by the client are not evidence.
    """

    def __init__(
        self,
        registry: PairingRegistry,
        *,
        local_source: Callable[[web.Request], Source | None] | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._registry = registry
        self._local_source = local_source
        self._clock = clock
        self._window_end = 0.0
        self._total = 0
        self._attempts: dict[Source, int] = {}

    def _limit(self, source: Source) -> int | None:
        now = self._clock()
        if now >= self._window_end:
            self._window_end = now + _WINDOW_SECONDS
            self._total = 0
            self._attempts.clear()
        if isinstance(source, IPv6Address) and source.ipv4_mapped is not None:
            source = source.ipv4_mapped
        attempts = self._attempts.get(source, 0)
        if self._total >= _TOTAL_LIMIT or attempts >= _PER_SOURCE_LIMIT:
            return ceil(self._window_end - now)
        self._total += 1
        self._attempts[source] = attempts + 1
        return None

    async def _run(
        self, request: web.Request, operation: Callable[[web.Request], Awaitable[dict]],
    ) -> web.Response:
        if not request.secure or self._local_source is None or (
            source := self._local_source(request)
        ) is None:
            return _response({"error": "not_found"}, 404)
        if (retry_after := self._limit(source)) is not None:
            return _response({"error": "rate_limited"}, 429, retry_after=retry_after)
        try:
            if request.query_string:
                raise _InvalidRequest
            return _response(await operation(request))
        except InvalidPairingCredential:
            return _response({"error": "invalid_credential"}, 401)
        except PairingConflict:
            return _response({"error": "pairing_conflict"}, 409)
        except PairingApprovalRequired:
            return _response({"error": "approval_required"}, 403)
        except (HomeAssistantError, PairingError, OSError):
            return _response({"error": "pairing_unavailable"}, 503)
        except _BodyTooLarge:
            return _response({"error": "body_too_large"}, 413)
        except (_InvalidRequest, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return _response({"error": "invalid_request"}, 400)

    async def claim(self, request: web.Request) -> web.Response:
        return await self._run(request, self._claim)

    async def status(self, request: web.Request) -> web.Response:
        return await self._run(request, self._status)

    async def activate(self, request: web.Request) -> web.Response:
        return await self._run(request, self._activate)

    async def _claim(self, request: web.Request) -> dict:
        code = _single_header(request, _INVITATION_HEADER)
        payload = await _json_body(request, {"claim_id", "installation_id", "phone_label", "credential_digest"})
        label = payload["phone_label"]
        if not isinstance(label, str) or not label.strip() or len(label) > 64 or not label.isprintable():
            raise _InvalidRequest
        reservation = await self._registry.async_claim(
            code,
            PairingClaim(
                _uuid(payload["claim_id"]), _uuid(payload["installation_id"]),
                label, _decode_32(payload["credential_digest"]),
            ),
        )
        return {
            "pairing_id": str(reservation.pairing_id),
            "server_id": str(reservation.server_id),
            "challenge": reservation.challenge,
            "state": reservation.status.value,
        }

    async def _status(self, request: web.Request) -> dict:
        pairing_id, secret = _credential(request)
        if request.can_read_body:
            raise _InvalidRequest
        return await self._check(pairing_id, secret, confirm=False)

    async def _activate(self, request: web.Request) -> dict:
        pairing_id, secret = _credential(request)
        if (await _json_body(request, {"confirmed"}))["confirmed"] is not True:
            raise _InvalidRequest
        return await self._check(pairing_id, secret, confirm=True)

    async def _check(self, pairing_id: UUID, secret: bytes, *, confirm: bool) -> dict:
        state = await self._registry.async_check_pairing(pairing_id, secret, confirm=confirm)
        return {"pairing_id": str(pairing_id), "server_id": str(self._registry.server_id), "state": state.value}
