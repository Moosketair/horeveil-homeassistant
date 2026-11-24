"""Restricted pairing ingress on the Home Assistant HTTP server."""

import asyncio
from hashlib import sha256
from hmac import compare_digest
from ipaddress import ip_address
from urllib.parse import urlsplit

from aiohttp import web
from hass_nabucasa import remote

from .pairing import Invitation, PairingError, PairingRegistry, validate_origin
from .pairing_http import PairingHttp, Source
from .pairing_proxy import FINGERPRINT, _one_header


class NativePairingIngress:
    """Accept only the configured NPM peer and its vhost-specific credential."""

    def __init__(self, registry: PairingRegistry, server, options: object) -> None:
        if not isinstance(options, dict):
            raise PairingError("Invalid native pairing configuration")
        origin = options.get("origin")
        peer = options.get("proxy_ip")
        digest = options.get("relay_secret_sha256")
        if not isinstance(origin, str) or not isinstance(peer, str) or not isinstance(digest, str):
            raise PairingError("Expected an HTTPS origin, exact proxy IP and relay digest")
        self.configured_origin = validate_origin(origin)
        host = urlsplit(self.configured_origin).hostname
        if not host or "." not in host or not host.rsplit(".", 1)[1][0].isalpha() or host.endswith(".localhost"):
            raise PairingError("Expected a dedicated HTTPS DNS name")

        self._peer = ip_address(peer)
        if (
            not self._peer.is_private or self._peer.is_unspecified or self._peer.is_link_local
            or self._peer.is_multicast or "%" in peer
            or not any(self._peer in net for net in getattr(server, "trusted_proxies", ()))
            or not FINGERPRINT.fullmatch(digest)
        ):
            raise PairingError("Proxy peer or relay digest unavailable")
        self._require_tls = options.get("upstream") == "tls"
        if self._require_tls:
            if getattr(server, "context", None) is None:
                raise PairingError("Core HTTPS is required for this upstream")
        elif options.get("upstream") != "isolated_http" or options.get("isolation_verified") is not True:
            raise PairingError("Plain HTTP requires independently verified upstream isolation")

        self._digest = bytes.fromhex(digest)
        self._registry = registry
        self._lock = asyncio.Lock()
        self._session: object | None = None
        self.http = PairingHttp(registry)

    @property
    def origin(self) -> str | None:
        return self.configured_origin if self._session is not None else None

    def _source(self, request: web.Request, session: object) -> Source | None:
        if self._session is not session or remote.is_cloud_request.get() or request.transport is None:
            return None
        peer = request.transport.get_extra_info("peername")
        if not isinstance(peer, tuple) or not peer or peer[0] != str(self._peer):
            return None
        if self._require_tls and request.transport.get_extra_info("ssl_object") is None:
            return None
        secret = _one_header(request, "X-Horeveil-Relay-Secret")
        if not FINGERPRINT.fullmatch(secret) or not compare_digest(sha256(secret.encode("ascii")).digest(), self._digest):
            return None
        # The dedicated vhost overwrites these values on its HTTPS listener.
        # Core independently derives request.secure and request.remote from its trusted proxy.
        if (
            _one_header(request, "X-Horeveil-Proxy-Origin") != self.configured_origin
            or _one_header(request, "X-Forwarded-Proto") != "https"
        ):
            return None
        source = _one_header(request, "X-Forwarded-For")
        if len(source) > 45 or "%" in source or request.remote is None:
            return None
        try:
            address = ip_address(source)
            return address if ip_address(request.remote) == address else None
        except ValueError:
            return None

    async def async_enable(self, value: str) -> None:
        if validate_origin(value) != self.configured_origin:
            raise PairingError("Confirm the configured pairing origin")
        async with self._lock:
            if self._session is None:
                session = object()
                self.http = PairingHttp(
                    self._registry, origin=self.configured_origin,
                    local_source=lambda request: self._source(request, session),
                )
                self._session = session

    async def async_disable(self) -> None:
        async with self._lock:
            self._session = None
            await self._registry.async_discard_invitations(origin=self.configured_origin)

    async def async_invite(self, origin: str, administrator_id: str) -> Invitation:
        async with self._lock:
            if origin != self.origin:
                raise PairingError("Pairing origin unavailable")
            return await self._registry.async_invite(administrator_id=administrator_id, confirmed_origin=origin)
