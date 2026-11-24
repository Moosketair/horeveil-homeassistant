"""Private mTLS listener for the three phone pairing exchanges."""

import asyncio
from hashlib import sha256
from hmac import compare_digest
from ipaddress import ip_address
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit

from aiohttp import web

from .pairing import Invitation, PairingError, PairingRegistry, validate_origin
from .pairing_http import PairingHttp, Source

PAIRING_BASE = "/api/horeveil/v1/pair"
LOOPBACK = "127.0.0.1"
FINGERPRINT = re.compile(r"[0-9a-f]{64}", re.ASCII)


def direct_tls(request: web.Request | None) -> ssl.SSLObject | None:
    """Use the actual socket and reject metadata from other proxy chains."""
    if request is None or not request.secure or request.transport is None:
        return None
    if any(
        name.lower().startswith(("x-forwarded-", "x-ha-cloud"))
        or name.lower() in {"forwarded", "via", "x-real-ip"}
        for name in request.headers
    ):
        return None
    transport = request.transport
    for field in ("peername", "sockname"):
        address = transport.get_extra_info(field)
        if not isinstance(address, tuple) or len(address) != 2 or address[0] != LOOPBACK:
            return None
    if request.remote != LOOPBACK:
        return None
    tls = transport.get_extra_info("ssl_object")
    if tls is None or tls.context.verify_mode != ssl.CERT_REQUIRED:
        return None
    return tls


def pinned_certificate(tls: ssl.SSLObject, fingerprint: bytes) -> bytes | None:
    try:
        certificate = tls.getpeercert(binary_form=True)
    except (ValueError, ssl.SSLError):
        return None
    if certificate and compare_digest(sha256(certificate).digest(), fingerprint):
        return certificate
    return None


def _one_header(request: web.Request, name: str) -> str:
    values = request.headers.getall(name, ())
    return values[0] if len(values) == 1 else ""


def build_proxy_tls(options: dict) -> tuple[ssl.SSLContext, int, bytes]:
    """Load only explicitly configured paths and require a specific proxy leaf."""
    port = options.get("port")
    fingerprint = options.get("proxy_certificate_sha256")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise PairingError("Invalid proxy listener port")
    if not isinstance(fingerprint, str) or not FINGERPRINT.fullmatch(fingerprint):
        raise PairingError("Invalid proxy certificate fingerprint")
    paths = [options.get(key) for key in ("certificate", "key", "client_ca")]
    if not all(isinstance(path, str) and Path(path).is_absolute() for path in paths):
        raise PairingError("Expected absolute certificate paths")
    certificate, key, client_ca = paths
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(certificate, key)
    tls.load_verify_locations(cafile=client_ca)
    tls.verify_mode = ssl.CERT_REQUIRED
    return tls, port, bytes.fromhex(fingerprint)


class PairingProxy:
    """Start a dedicated listener only after the HA administrator opts in."""

    def __init__(self, registry: PairingRegistry, tls: ssl.SSLContext, port: int, fingerprint: bytes) -> None:
        self._registry = registry
        self._tls = tls
        self._port = port
        self._fingerprint = fingerprint
        self._lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._session_origin: str | None = None
        self._accepting = False

    @property
    def origin(self) -> str | None:
        return self._session_origin if self._accepting else None

    def _source(self, request: web.Request, origin: str) -> Source | None:
        if self.origin != origin:
            return None
        tls = direct_tls(request)
        if tls is None or pinned_certificate(tls, self._fingerprint) is None:
            return None
        if (
            _one_header(request, "X-Horeveil-Proxy-Scheme") != "https"
            or _one_header(request, "X-Horeveil-Proxy-Origin") != origin
        ):
            return None
        address = _one_header(request, "X-Horeveil-Proxy-Client-IP")
        if "%" in address or len(address) > 45:
            return None
        try:
            return ip_address(address)
        except ValueError:
            return None

    async def async_enable(self, value: str) -> str:
        origin = validate_origin(value)
        host = urlsplit(origin).hostname
        if host is None or host == "localhost" or host.endswith(".localhost"):
            raise PairingError("Use the HTTPS gateway origin")
        try:
            address = ip_address(host)
        except ValueError:
            address = None
        if address is not None and (address.is_loopback or address.is_unspecified or address.is_link_local):
            raise PairingError("Use the HTTPS gateway origin")
        async with self._lock:
            if self.origin == origin:
                return origin
            if self._runner is not None:
                raise PairingError("Disable the current origin first")
            http = PairingHttp(
                self._registry, local_source=lambda request: self._source(request, origin),
                origin=origin,
            )
            app = web.Application(handler_args={"auto_decompress": False})
            app.router.add_post(f"{PAIRING_BASE}/claim", http.claim)
            app.router.add_get(f"{PAIRING_BASE}/status", http.status, allow_head=False)
            app.router.add_post(f"{PAIRING_BASE}/activate", http.activate)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
            try:
                await runner.setup()
                await web.TCPSite(runner, LOOPBACK, self._port, ssl_context=self._tls).start()
            except BaseException:
                await runner.cleanup()
                raise
            self._runner = runner
            self._session_origin = origin
            self._accepting = True
            return origin

    async def async_disable(self) -> None:
        async with self._lock:
            if self._runner is None:
                return
            self._accepting = False
            await self._runner.cleanup()
            assert self._session_origin is not None
            await self._registry.async_discard_invitations(origin=self._session_origin)
            self._runner = None
            self._session_origin = None

    async def async_invite(self, origin: str, administrator_id: str) -> Invitation:
        async with self._lock:
            if self.origin is None or origin != self.origin:
                raise PairingError("Gateway origin unavailable")
            return await self._registry.async_invite(administrator_id=administrator_id, confirmed_origin=origin)
