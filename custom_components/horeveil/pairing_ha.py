"""Home Assistant administration and pairing views."""

from asyncio import Lock
from base64 import urlsafe_b64decode, urlsafe_b64encode
from ipaddress import IPv4Address
import logging
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit
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
from homeassistant.helpers.storage import Store

from .const import CALENDAR_RECEIVERS, DOMAIN, PAIRING_RUNTIME as _RUNTIME_KEY
from .pairing import InvalidPairingCredential, PairingError, PairingRegistry, _persist, validate_origin
from .pairing_http import PairingHttp
from .pairing_native import NativePairingIngress
from .pairing_proxy import PAIRING_BASE, FINGERPRINT, PairingProxy, build_proxy_tls, direct_tls, pinned_certificate

_LOGGER = logging.getLogger(__name__)
_WS = "horeveil/pairing_test"
_LOOPBACK = IPv4Address("127.0.0.1")
_OAEP = padding.OAEP(
    mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None,
)
_PANEL = "horeveil-association"
_PANEL_MODULE = "/api/horeveil/pairing-panel.js"
_PUBLIC_KEY = re.compile(r"[A-Za-z0-9_-]{392}", re.ASCII)


class _StorageOutcomeUnknown(PairingError):
    """The operation reached the Store and its durable result could not be verified."""


def _supports_local_test(server) -> bool:
    """Use the actual HTTP server settings, including any Core fallback."""
    return (
        getattr(server, "server_host", None) == ["127.0.0.1"]
        and getattr(server, "trusted_proxies", None) == []
        and getattr(server, "supervisor_unix_socket_path", None) is None
        and _supports_admin_tls(server)
    )


def _supports_admin_tls(server) -> bool:
    return (
        getattr(server, "ssl_peer_certificate", None) is not None
        and getattr(getattr(server, "context", None), "verify_mode", None) == ssl.CERT_REQUIRED
    )


class _PairingRuntime:
    def __init__(
        self, hass: HomeAssistant, registry: PairingRegistry, fingerprint: bytes | None, port: int, *,
        local_test: bool, proxy: PairingProxy | NativePairingIngress | None,
    ) -> None:
        self.registry = registry
        self.active = True
        self.fingerprint = fingerprint
        self.origin = validate_origin(f"https://{_LOOPBACK}:{port}") if local_test else None
        self.proxy = proxy
        self.http = PairingHttp(registry, local_source=self.local_source, origin=self.origin)
        self.universal = False
        self.labels: Store[dict[str, str]] = Store(
            hass, 1, f"{DOMAIN}.pairing_labels", private=True, atomic_writes=True,
        )
        # Label operations take this lock before consulting or revoking in the registry.
        self.labels_lock = Lock()

    @property
    def native(self) -> NativePairingIngress | None:
        return self.proxy if isinstance(self.proxy, NativePairingIngress) else None

    @property
    def phone_http(self) -> PairingHttp:
        native = self.native
        return native.http if native is not None else self.http

    def client_key(self, request: web.Request | None) -> rsa.RSAPublicKey | None:
        """Authenticate the direct loopback socket and its pinned client certificate."""
        if not self.active or self.fingerprint is None or remote.is_cloud_request.get():
            return None
        tls = direct_tls(request)
        if tls is None or (certificate := pinned_certificate(tls, self.fingerprint)) is None:
            return None
        try:
            key = x509.load_der_x509_certificate(certificate).public_key()
        except (UnsupportedAlgorithm, ValueError, ssl.SSLError):
            return None
        return key if isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048 else None

    def local_source(self, request: web.Request) -> IPv4Address | None:
        return _LOOPBACK if self.origin is not None and self.client_key(request) is not None else None

    async def stop(self, _event) -> None:
        self.active = False
        if self.proxy is not None:
            await self.proxy.async_disable()
        await self.registry.async_discard_invitations()

    async def async_load_labels(self) -> dict[str, str]:
        labels = await self.labels.async_load()
        if labels is None:
            return {}
        if not isinstance(labels, dict):
            raise PairingError("Invalid phone labels")
        return labels

    async def async_save_labels(self, labels: dict[str, str], previous: dict[str, str]) -> None:
        if getattr(self.labels.hass, "is_stopping", False):
            raise PairingError("Home Assistant is stopping")
        try:
            await self.labels.async_save(labels)
            persisted = await self.labels.async_load()
        except (HomeAssistantError, OSError, ValueError):
            raise _StorageOutcomeUnknown("Unable to verify persisted phone labels") from None
        if persisted == previous:
            raise PairingError("Previous phone labels were retained")
        if persisted != labels:
            raise _StorageOutcomeUnknown("Unable to verify persisted phone labels")

    async def async_prune_labels(self) -> None:
        """Reconcile the display store with the authoritative registry under labels_lock."""
        try:
            previous = await self.async_load_labels()
            if previous:
                active = {str(phone) for phone in await self.registry.async_list_active()}
                labels = {key: value for key, value in previous.items() if key in active}
                if labels != previous:
                    await self.async_save_labels(labels, previous)
        except (HomeAssistantError, OSError, ValueError):
            _LOGGER.warning("Horéveil could not clean up stored phone labels")


def _configured_https_origin(hass: HomeAssistant) -> str | None:
    for candidate in (hass.config.external_url, getattr(hass.config, "internal_url", None)):
        if candidate:
            try:
                return validate_origin(candidate)
            except PairingError:
                continue
    return None


def _https_origins(hass) -> list[str]:
    from homeassistant.helpers.network import NoURLAvailableError, get_url

    origins = []
    for options in (
        {"allow_internal": False, "allow_cloud": False},
        {"allow_external": False, "allow_cloud": False},
        {"allow_internal": False, "require_cloud": True},
    ):
        try:
            origin = validate_origin(get_url(hass, require_ssl=True, allow_ip=True, **options))
        except (NoURLAvailableError, PairingError):
            continue
        if origin not in origins:
            origins.append(origin)
    return origins


class _PairView(HomeAssistantView):
    requires_auth = False  # PairingHttp authenticates the phone after the socket gate.
    cors_allowed = False

    def __init__(self, test: _PairingRuntime) -> None:
        self.test = test


class _ClaimView(_PairView):
    url = f"{PAIRING_BASE}/claim"
    name = "api:horeveil:pair:claim"

    async def post(self, request: web.Request) -> web.Response:
        return await self.test.phone_http.claim(request)


class _StatusView(_PairView):
    url = f"{PAIRING_BASE}/status"
    name = "api:horeveil:pair:status"

    async def get(self, request: web.Request) -> web.Response:
        return await self.test.phone_http.status(request)


class _ActivateView(_PairView):
    url = f"{PAIRING_BASE}/activate"
    name = "api:horeveil:pair:activate"

    async def post(self, request: web.Request) -> web.Response:
        return await self.test.phone_http.activate(request)


class _PanelModuleView(_PairView):
    url = _PANEL_MODULE
    name = "api:horeveil:pair:panel"
    filename = "pairing_panel.js"

    async def get(self, request: web.Request) -> web.StreamResponse:
        # The native ES modules contain only static code and are fetched without a HA token.
        if not self.test.universal and self.test.native is None and self.test.client_key(request) is None:
            raise web.HTTPNotFound
        return web.FileResponse(
            Path(__file__).with_name(self.filename),
            headers={"Cache-Control": "no-store", "Content-Type": "text/javascript"},
        )


class _QrModuleView(_PanelModuleView):
    url = "/api/horeveil/pairing-qrcode.js"
    name = "api:horeveil:pair:qrcode"
    filename = "pairing_qrcode.js"


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


def _recipient(value: object, client_key: rsa.RSAPublicKey | None) -> rsa.RSAPublicKey:
    """Use the browser's ephemeral public key, or the original CLI certificate."""
    if value is None and client_key is not None:
        return client_key
    if not isinstance(value, str) or not _PUBLIC_KEY.fullmatch(value):
        raise ValueError
    key = load_der_public_key(urlsafe_b64decode(value))
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size != 2048 or key.public_numbers().e != 65537:
        raise ValueError
    return key


class _AdminOperation:
    """Require an active HA administrator and the transport policy for this mode."""

    def __init__(self, hass, connection, msg) -> None:
        self.test = hass.data[_RUNTIME_KEY]
        self.connection = connection
        self.msg_id = msg["id"]
        self.user = connection.user
        self.key = None
        self.authorized = False
        if self.test.active and self.user is not None and self.user.is_active and self.user.is_admin:
            request = current_request.get()
            if self.test.universal:
                self.authorized = request is not None and request.secure
            elif self.test.native is None:
                self.key = self.test.client_key(request)
                self.authorized = self.key is not None
            else:
                self.authorized = request is not None and request.secure and not remote.is_cloud_request.get()
        if not self.authorized:
            self.connection.send_error(self.msg_id, "unauthorized", "Secure administrator session required")

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
    if not operation.authorized:
        return
    try:
        origin = msg.get("origin")
        if not isinstance(origin, str):
            operation.error("invalid_origin")
            return
        origin = validate_origin(origin)
        recipient = _recipient(msg.get("display_key"), operation.key)
        if operation.test.universal and origin in _https_origins(hass):
            invitation = await operation.test.registry.async_invite(
                administrator_id=operation.user.id, confirmed_origin=origin, direct=True,
            )
        elif origin == operation.test.origin:
            invitation = await operation.test.registry.async_invite(
                administrator_id=operation.user.id, confirmed_origin=origin,
            )
        elif operation.test.proxy is not None and origin == operation.test.proxy.origin:
            invitation = await operation.test.proxy.async_invite(origin, operation.user.id)
        else:
            operation.error("invalid_origin")
            return
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
    if not operation.authorized:
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
    if not operation.authorized:
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
    if not operation.authorized:
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
    if not operation.authorized:
        return
    suggestion = _configured_https_origin(hass)
    async with operation.test.labels_lock:
        try:
            active = await operation.test.registry.async_list_active()
            signed = await operation.test.registry.async_list_active(signed_only=True) if operation.test.universal else ()
        except PairingError:
            operation.error("registry_unavailable")
            return
        try:
            labels = await operation.test.async_load_labels() if active else {}
        except (HomeAssistantError, OSError, ValueError):
            labels = None
    phones = [str(pairing_id) for pairing_id in active]
    result = {
        "suggested_origin": suggestion or operation.test.origin,
        "test_origin": operation.test.origin,
        "phones": phones,
    }
    if active:
        result["phone_labels"] = None if labels is None else {
            key: labels[key] for key in phones if isinstance(labels.get(key), str)
        }
    if operation.test.proxy is not None:
        result.update(direct_configured=True, direct_origin=operation.test.proxy.origin)
    if (native := operation.test.native) is not None:
        result["configured_origin"] = native.configured_origin
    if operation.test.universal:
        result["https_origins"] = origins = _https_origins(hass)
        result["suggested_origin"] = origins[0] if origins else None
        result["calendar_phones"] = [str(pairing_id) for pairing_id in signed]
        calendars = result["calendars"] = []
        receivers = hass.data.get(CALENDAR_RECEIVERS, {})
        for entry in hass.config_entries.async_entries(DOMAIN):
            if (receiver := receivers.get(entry.entry_id)) is None:
                continue
            uncertain = receiver.source_selection_uncertain
            source = None if uncertain else receiver.direct_source
            received_at = None if uncertain else receiver.received_at
            calendars.append({
                "entry_id": entry.entry_id,
                "title": entry.title,
                "source_uncertain": uncertain,
                "pairing_id": str(source) if source else None,
                "last_success": received_at.isoformat() if received_at else None,
            })
    operation.result(result)


@websocket_api.websocket_command({"type": f"{_WS}/rename", "pairing_id": str, "label": str})
@websocket_api.require_admin
@websocket_api.async_response
async def _rename(hass, connection, msg) -> None:
    operation = _AdminOperation(hass, connection, msg)
    if not operation.authorized:
        return
    try:
        pairing_id = _uuid(msg["pairing_id"])
        label = msg["label"].strip()
        if len(label) > 64 or (label and not label.isprintable()):
            operation.error("invalid_label")
            return
        async with operation.test.labels_lock:
            active = {str(phone) for phone in await operation.test.registry.async_list_active()}
            phone_id = str(pairing_id)
            if phone_id not in active:
                operation.error("phone_unavailable")
                return
            previous = await operation.test.async_load_labels()
            labels = {
                key: value for key, value in previous.items()
                if key in active and isinstance(value, str)
            }
            if label:
                labels[phone_id] = label
            else:
                labels.pop(phone_id, None)
            if labels != previous:
                await _persist(lambda: operation.test.async_save_labels(labels, previous))
            operation.result({"state": "renamed"})
    except _StorageOutcomeUnknown:
        operation.error("storage_outcome_unknown")
    except (HomeAssistantError, OSError, ValueError):
        operation.error("rename_unavailable")


@websocket_api.websocket_command({
    "type": f"{_WS}/calendar_source", "entry_id": str, "confirmed": bool,
    "pairing_id": probatio.Any(None, str),
})
@websocket_api.require_admin
@websocket_api.async_response
async def _calendar_source(hass, connection, msg) -> None:
    operation = _AdminOperation(hass, connection, msg)
    if not operation.authorized:
        return
    try:
        receivers = hass.data.get(CALENDAR_RECEIVERS, {})
        receiver = receivers.get(msg["entry_id"])
        if not operation.test.universal or msg["confirmed"] is not True or receiver is None:
            raise PairingError("Calendar source unavailable")
        pairing_id = _uuid(msg["pairing_id"]) if msg["pairing_id"] is not None else None

        async def select(source):
            if source is not None and any(
                candidate is not receiver and candidate.direct_source == source
                for candidate in receivers.values()
            ):
                return False
            selected = await receiver.async_select_source(source)
            if not selected and receiver.source_selection_uncertain:
                raise _StorageOutcomeUnknown("Unable to verify persisted calendar source")
            return selected

        await operation.test.registry.async_select_calendar_source(pairing_id, select)
        operation.result({"state": "selected"})
    except _StorageOutcomeUnknown:
        operation.error("storage_outcome_unknown")
    except (HomeAssistantError, OSError, ValueError):
        operation.error("calendar_source_unavailable")


@websocket_api.websocket_command({"type": f"{_WS}/revoke", "pairing_id": str, "confirmed": bool})
@websocket_api.require_admin
@websocket_api.async_response
async def _revoke(hass, connection, msg) -> None:
    operation = _AdminOperation(hass, connection, msg)
    if not operation.authorized:
        return
    try:
        if msg.get("confirmed") is not True:
            operation.error("confirmation_required")
            return
        async def purge(pairing_id):
            receivers = tuple(hass.data.get(CALENDAR_RECEIVERS, {}).values())
            cleaned = [await receiver.async_select_source(None, forget=pairing_id) for receiver in receivers]
            if not all(cleaned):
                raise PairingError("Calendar storage unavailable")

        pairing_id = _uuid(msg.get("pairing_id"))

        async def revoke():
            try:
                revoked = await operation.test.registry.async_revoke(pairing_id, purge=purge)
            finally:
                await operation.test.async_prune_labels()
            if revoked:
                operation.result({"state": "revoked"})
            else:
                operation.error("phone_unavailable")

        async with operation.test.labels_lock:
            await _persist(revoke)
    except (HomeAssistantError, OSError, ValueError):
        operation.error("registry_unavailable")


@websocket_api.websocket_command({
    "type": f"{_WS}/direct_access", "enabled": bool, "confirmed": bool,
    probatio.Optional("origin"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def _direct_access(hass, connection, msg) -> None:
    operation = _AdminOperation(hass, connection, msg)
    if not operation.authorized:
        return
    proxy = operation.test.proxy
    if proxy is None or msg.get("confirmed") is not True:
        operation.error("direct_unavailable")
        return
    try:
        origin = msg.get("origin")
        if msg.get("enabled") is True and isinstance(origin, str):
            await proxy.async_enable(origin)
        elif msg.get("enabled") is False:
            await proxy.async_disable()
        else:
            raise PairingError("Explicit gateway origin required")
        operation.result({"direct_origin": proxy.origin})
    except (OSError, PairingError):
        operation.error("direct_unavailable")


async def async_setup_local_pairing(
    hass: HomeAssistant, fingerprint: object, direct_options: object = None,
    *, native_options: object = None, universal: bool = False,
) -> None:
    """Prepare native administration and any explicitly configured access modes."""
    server = hass.http
    local_test = isinstance(fingerprint, str) and FINGERPRINT.fullmatch(fingerprint) and _supports_local_test(server)
    if fingerprint is not None and not local_test:
        _LOGGER.warning("Horéveil local pairing test is disabled: incompatible HTTP configuration")
    if _RUNTIME_KEY in hass.data:
        return
    if native_options is not None and (fingerprint is not None or direct_options is not None):
        _LOGGER.warning("Horéveil pairing is disabled: choose one transport configuration")
        return
    proxy_config = None
    admin_fingerprint = fingerprint if local_test else None
    if direct_options is not None:
        try:
            if not isinstance(direct_options, dict) or not _supports_admin_tls(server):
                raise PairingError("Native administrator mTLS required")
            admin = direct_options.get("admin_certificate_sha256")
            if (
                not isinstance(admin, str) or not FINGERPRINT.fullmatch(admin)
                or admin == direct_options.get("proxy_certificate_sha256")
                or (admin_fingerprint is not None and admin_fingerprint != admin)
            ):
                raise PairingError("Distinct administrator and gateway certificates required")
            proxy_config = await hass.async_add_executor_job(build_proxy_tls, direct_options)
            admin_fingerprint = admin
        except (OSError, PairingError, ValueError):
            _LOGGER.warning("Horéveil direct pairing is disabled: incompatible TLS configuration")
    if admin_fingerprint is None and native_options is None and not universal:
        return
    try:
        registry = await PairingRegistry.async_open(hass)
    except (HomeAssistantError, PairingError, OSError):
        _LOGGER.warning("Horéveil local pairing test is disabled: storage unavailable")
        return
    if _RUNTIME_KEY in hass.data:
        return
    proxy = PairingProxy(registry, *proxy_config) if proxy_config is not None else None
    if native_options is not None:
        try:
            proxy = NativePairingIngress(registry, server, native_options)
            external = hass.config.external_url
            if external and urlsplit(external).hostname == urlsplit(proxy.configured_origin).hostname:
                raise PairingError("The relay must have its own DNS name")
        except (PairingError, ValueError):
            _LOGGER.warning("Horéveil native pairing is disabled: incompatible proxy configuration")
            return
    test = _PairingRuntime(
        hass, registry, bytes.fromhex(admin_fingerprint) if admin_fingerprint is not None else None,
        server.server_port, local_test=bool(local_test), proxy=proxy,
    )
    if universal:
        from .pairing_signed import DIRECT_BASE, SignedPairingHttp

        test.universal = True
        test.http = SignedPairingHttp(registry)

        class DirectView(_PairView):
            def __init__(self, operation):
                super().__init__(test)
                self.operation = operation
                self.url = f"{DIRECT_BASE}/{operation}"
                self.name = f"api:horeveil:direct:{operation}"

            async def post(self, request):
                if self.operation == "claim":
                    return await test.http.claim(request)
                if self.operation == "calendar":
                    def find_receiver(pairing_id):
                        found = [receiver for receiver in hass.data.get(CALENDAR_RECEIVERS, {}).values()
                                 if receiver.direct_source == pairing_id]
                        return found[0] if len(found) == 1 else None

                    return await test.http.calendar(request, hass, find_receiver)
                return await test.http.signed(request, self.operation)

        for operation in ("claim", "enroll", "status", "activate", "cancel", "calendar"):
            server.register_view(DirectView(operation))
    if local_test or test.native is not None:
        for view in (_ClaimView, _StatusView, _ActivateView):
            server.register_view(view(test))
    for command in (_invite, _status, _approve, _cancel, _info, _rename, _revoke):
        websocket_api.async_register_command(hass, command)
    if universal:
        websocket_api.async_register_command(hass, _calendar_source)
    if proxy is not None:
        websocket_api.async_register_command(hass, _direct_access)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, test.stop)
    hass.data[_RUNTIME_KEY] = test
    async with test.labels_lock:
        await test.async_prune_labels()
    if "frontend" in hass.config.components:
        server.register_view(_PanelModuleView(test))
        server.register_view(_QrModuleView(test))
        await async_register_panel(
            hass, frontend_url_path=_PANEL, webcomponent_name=_PANEL,
            sidebar_title="Horéveil", sidebar_icon="mdi:alarm",
            module_url=_PANEL_MODULE, require_admin=True, config_panel_domain=DOMAIN,
            config={"https_origin": _configured_https_origin(hass)},
        )
