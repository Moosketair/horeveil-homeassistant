"""Native HA wiring for an explicitly enabled, isolated pairing test."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
from hmac import compare_digest
from ipaddress import IPv4Address
import logging
from pathlib import Path
import re
import ssl
from uuid import UUID

from aiohttp import web
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key
from hass_nabucasa import remote
import probatio

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView, current_request
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .pairing import InvalidPairingCredential, PairingError, PairingRegistry, validate_origin
from .pairing_http import PairingHttp

_LOGGER = logging.getLogger(__name__)
_BASE = "/api/horeveil/v1/pair"
_WS = "horeveil/pairing_test"
_LOOPBACK = IPv4Address("127.0.0.1")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}", re.ASCII)
_OAEP = padding.OAEP(
    mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None,
)
_RUNTIME_KEY = "horeveil.local_pairing_test"
_PANEL = "horeveil-association"
_PANEL_MODULE = "/api/horeveil/pairing-panel.js"
_PUBLIC_KEY = re.compile(r"[A-Za-z0-9_-]{392}", re.ASCII)


def _supports_local_test(server) -> bool:
    """Use the actual HTTP server settings, including any Core fallback."""
    return (
        getattr(server, "server_host", None) == ["127.0.0.1"]
        and getattr(server, "trusted_proxies", None) == []
        and getattr(server, "supervisor_unix_socket_path", None) is None
        and getattr(server, "ssl_peer_certificate", None) is not None
        and getattr(getattr(server, "context", None), "verify_mode", None) == ssl.CERT_REQUIRED
    )


class _LocalPairingTest:
    def __init__(self, registry: PairingRegistry, fingerprint: bytes, port: int) -> None:
        self.registry = registry
        self.active = True
        self.fingerprint = fingerprint
        self.origin = validate_origin(f"https://{_LOOPBACK}:{port}")
        self.http = PairingHttp(registry, local_source=self.local_source)

    def client_key(self, request: web.Request | None) -> rsa.RSAPublicKey | None:
        """Authenticate the direct loopback socket and its pinned client certificate."""
        if (
            not self.active or request is None or not request.secure
            or remote.is_cloud_request.get() or request.transport is None
            or any(
                name.lower().startswith(("x-forwarded-", "x-ha-cloud"))
                or name.lower() in {"forwarded", "via", "x-real-ip"}
                for name in request.headers
            )
        ):
            return None
        transport = request.transport
        peer = transport.get_extra_info("peername")
        local = transport.get_extra_info("sockname")
        tls = transport.get_extra_info("ssl_object")
        if (
            request.remote != str(_LOOPBACK) or not isinstance(peer, tuple) or len(peer) != 2
            or peer[0] != str(_LOOPBACK) or not isinstance(local, tuple) or len(local) != 2
            or local[0] != str(_LOOPBACK) or tls is None or tls.context.verify_mode != ssl.CERT_REQUIRED
        ):
            return None
        try:
            certificate = tls.getpeercert(binary_form=True)
            if not certificate or not compare_digest(sha256(certificate).digest(), self.fingerprint):
                return None
            key = x509.load_der_x509_certificate(certificate).public_key()
        except (UnsupportedAlgorithm, ValueError, ssl.SSLError):
            return None
        return key if isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048 else None

    def local_source(self, request: web.Request) -> IPv4Address | None:
        return _LOOPBACK if self.client_key(request) is not None else None

    async def stop(self, _event) -> None:
        self.active = False
        await self.registry.async_discard_invitations()


class _PairView(HomeAssistantView):
    requires_auth = False  # PairingHttp authenticates the phone after the socket gate.
    cors_allowed = False

    def __init__(self, test: _LocalPairingTest) -> None:
        self.test = test


class _ClaimView(_PairView):
    url = f"{_BASE}/claim"
    name = "api:horeveil:pair:claim"

    async def post(self, request: web.Request) -> web.Response:
        return await self.test.http.claim(request)


class _StatusView(_PairView):
    url = f"{_BASE}/status"
    name = "api:horeveil:pair:status"

    async def get(self, request: web.Request) -> web.Response:
        return await self.test.http.status(request)


class _ActivateView(_PairView):
    url = f"{_BASE}/activate"
    name = "api:horeveil:pair:activate"

    async def post(self, request: web.Request) -> web.Response:
        return await self.test.http.activate(request)


class _PanelModuleView(_PairView):
    url = _PANEL_MODULE
    name = "api:horeveil:pair:panel"

    async def get(self, request: web.Request) -> web.StreamResponse:
        if self.test.client_key(request) is None:
            raise web.HTTPNotFound
        return web.FileResponse(
            Path(__file__).with_name("pairing_panel.js"),
            headers={"Cache-Control": "no-store", "Content-Type": "text/javascript"},
        )


def _uuid(value: object) -> UUID:
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError
    return parsed


def _seal(key: rsa.RSAPublicKey, value: str) -> str:
    # Core can log entire WS frames. The recipient decrypts with its private key.
    return urlsafe_b64encode(key.encrypt(value.encode("ascii"), _OAEP)).decode("ascii").rstrip("=")


def _recipient(value: object, client_key: rsa.RSAPublicKey) -> rsa.RSAPublicKey:
    """Use the browser's ephemeral public key, or the original CLI certificate."""
    if value is None:
        return client_key
    if not isinstance(value, str) or not _PUBLIC_KEY.fullmatch(value):
        raise ValueError
    key = load_der_public_key(urlsafe_b64decode(value))
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size != 2048 or key.public_numbers().e != 65537:
        raise ValueError
    return key


class _AdminOperation:
    """Share the same local gate across the native administrator commands."""

    def __init__(self, hass, connection, msg) -> None:
        self.test = hass.data[_RUNTIME_KEY]
        self.connection = connection
        self.msg_id = msg["id"]
        self.user = connection.user
        self.key = None
        if self.user is not None and self.user.is_active and self.user.is_admin:
            self.key = self.test.client_key(current_request.get())
        if self.key is None:
            self.connection.send_error(self.msg_id, "unauthorized", "Local test access required")

    def result(self, data: dict) -> None:
        self.connection.send_result(self.msg_id, data)

    def error(self, code: str) -> None:
        self.connection.send_error(self.msg_id, code, "Pairing operation unavailable")


@websocket_api.websocket_command({
    "type": f"{_WS}/invite", "origin": str, probatio.Optional("display_key"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def _invite(hass, connection, msg) -> None:
    operation = _AdminOperation(hass, connection, msg)
    if operation.key is None:
        return
    try:
        origin = msg.get("origin")
        if not isinstance(origin, str) or validate_origin(origin) != operation.test.origin:
            operation.error("invalid_origin")
            return
        recipient = _recipient(msg.get("display_key"), operation.key)
        invitation = await operation.test.registry.async_invite(
            administrator_id=operation.user.id, confirmed_origin=operation.test.origin,
        )
        operation.result({
            "invitation_id": str(invitation.invitation_id),
            "origin": invitation.origin,
            "expires_at": invitation.expires_at.isoformat(),
            "code_encrypted": _seal(recipient, invitation.code),
        })
    except (UnsupportedAlgorithm, ValueError):
        operation.error("invitation_unavailable")


@websocket_api.websocket_command({
    "type": f"{_WS}/status", "invitation_id": str, probatio.Optional("display_key"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def _status(hass, connection, msg) -> None:
    operation = _AdminOperation(hass, connection, msg)
    if operation.key is None:
        return
    try:
        recipient = _recipient(msg.get("display_key"), operation.key)
        review = await operation.test.registry.async_review_claim(
            operation.user, _uuid(msg.get("invitation_id")),
        )
        if review is None:
            operation.result({"state": "issued"})
        else:
            operation.result({
                "state": review.status.value,
                "pairing_id": str(review.pairing_id),
                "comparison_code_encrypted": _seal(recipient, review.comparison_code),
            })
    except (UnsupportedAlgorithm, ValueError):
        operation.error("invitation_unavailable")


@websocket_api.websocket_command({
    "type": f"{_WS}/approve", "invitation_id": str, "pairing_id": str, "confirmed": bool,
})
@websocket_api.require_admin
@websocket_api.async_response
async def _approve(hass, connection, msg) -> None:
    operation = _AdminOperation(hass, connection, msg)
    if operation.key is None:
        return
    try:
        pairing_id = _uuid(msg.get("pairing_id"))
        review = await operation.test.registry.async_review_claim(
            operation.user, _uuid(msg.get("invitation_id")),
        )
        if msg.get("confirmed") is not True or review is None or review.pairing_id != pairing_id:
            raise InvalidPairingCredential("Explicit comparison required")
        await operation.test.registry.async_approve_claim(
            operation.user, pairing_id, review.comparison_code,
        )
        operation.result({"state": "approved"})
    except (PermissionError, PairingError, ValueError):
        operation.error("invitation_unavailable")


@websocket_api.websocket_command({"type": f"{_WS}/cancel", "invitation_id": str})
@websocket_api.require_admin
@websocket_api.async_response
async def _cancel(hass, connection, msg) -> None:
    operation = _AdminOperation(hass, connection, msg)
    if operation.key is None:
        return
    try:
        if await operation.test.registry.async_cancel(
            _uuid(msg.get("invitation_id")), administrator_id=operation.user.id,
        ):
            operation.result({"state": "cancelled"})
        else:
            operation.error("invitation_unavailable")
    except ValueError:
        operation.error("invitation_unavailable")


@websocket_api.websocket_command({"type": f"{_WS}/info"})
@websocket_api.require_admin
@websocket_api.async_response
async def _info(hass, connection, msg) -> None:
    operation = _AdminOperation(hass, connection, msg)
    if operation.key is None:
        return
    configured_url = hass.config.external_url
    try:
        suggestion = validate_origin(configured_url) if configured_url else None
    except PairingError:
        suggestion = None
    try:
        active = await operation.test.registry.async_list_active()
    except PairingError:
        operation.error("registry_unavailable")
        return
    operation.result({
        "suggested_origin": suggestion or operation.test.origin,
        "test_origin": operation.test.origin,
        "phones": [str(pairing_id) for pairing_id in active],
    })


@websocket_api.websocket_command({"type": f"{_WS}/revoke", "pairing_id": str, "confirmed": bool})
@websocket_api.require_admin
@websocket_api.async_response
async def _revoke(hass, connection, msg) -> None:
    operation = _AdminOperation(hass, connection, msg)
    if operation.key is None:
        return
    try:
        if msg.get("confirmed") is not True:
            operation.error("confirmation_required")
            return
        revoked = await operation.test.registry.async_revoke(_uuid(msg.get("pairing_id")))
        if revoked:
            operation.result({"state": "revoked"})
        else:
            operation.error("phone_unavailable")
    except (HomeAssistantError, OSError, ValueError):
        operation.error("registry_unavailable")


async def async_setup_local_pairing(hass: HomeAssistant, fingerprint: object) -> None:
    """Register only on an explicitly opted-in, isolated native HA HTTP server."""
    server = hass.http
    if (
        not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint)
        or not _supports_local_test(server)
    ):
        _LOGGER.warning("Horéveil local pairing test is disabled: incompatible HTTP configuration")
        return
    if _RUNTIME_KEY in hass.data:
        return
    try:
        registry = await PairingRegistry.async_open(hass)
    except (HomeAssistantError, PairingError, OSError):
        _LOGGER.warning("Horéveil local pairing test is disabled: storage unavailable")
        return
    if _RUNTIME_KEY in hass.data:
        return
    test = _LocalPairingTest(registry, bytes.fromhex(fingerprint), server.server_port)
    for view in (_ClaimView, _StatusView, _ActivateView):
        server.register_view(view(test))
    for command in (_invite, _status, _approve, _cancel, _info, _revoke):
        websocket_api.async_register_command(hass, command)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, test.stop)
    hass.data[_RUNTIME_KEY] = test
    if "frontend" in hass.config.components:
        server.register_view(_PanelModuleView(test))
        await async_register_panel(
            hass, frontend_url_path=_PANEL, webcomponent_name=_PANEL,
            sidebar_title="Horéveil", sidebar_icon="mdi:alarm",
            module_url=_PANEL_MODULE, require_admin=True,
        )
