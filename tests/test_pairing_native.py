"""Regression tests for the native reverse-proxy boundary."""

from hashlib import sha256
from ipaddress import ip_network
from secrets import token_hex
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp.test_utils import make_mocked_request
from multidict import CIMultiDict

from test_receiver import is_cloud_request, pairing_ha_module

Ingress = pairing_ha_module.NativePairingIngress
_ORIGIN = "https://pairing.example.invalid"
_PEER = "172.19.0.5"
_CLIENT = "192.0.2.10"


class NativeIngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.secret = token_hex(32)
        self.server = SimpleNamespace(trusted_proxies=[ip_network(_PEER)], context=object())
        self.options = {
            "origin": _ORIGIN, "proxy_ip": _PEER, "upstream": "tls",
            "relay_secret_sha256": sha256(self.secret.encode()).hexdigest(),
        }
        self.registry = SimpleNamespace(async_discard_invitations=self.discard)
        self.discarded = []

    async def discard(self, *, origin):
        self.discarded.append(origin)

    def request(self, *, peer=_PEER, tls=True, remote=_CLIENT, headers=None, **overrides):
        values = CIMultiDict({
            "X-Horeveil-Relay-Secret": self.secret, "X-Horeveil-Proxy-Origin": _ORIGIN,
            "X-Forwarded-For": _CLIENT, "X-Forwarded-Proto": "https",
        } if headers is None else headers)
        values.update(overrides)
        transport = SimpleNamespace(get_extra_info={
            "peername": (peer, 40000), "ssl_object": object() if tls else None,
        }.get)
        return make_mocked_request("GET", "/status", headers=values, transport=transport).clone(
            remote=remote, scheme="https",
        )

    def test_activation_configuration_fails_closed(self):
        invalid = (
            {"upstream": "isolated_http"}, {"upstream": "isolated_http", "isolation_verified": "true"},
            {"proxy_ip": "172.19.0.0/24"}, {"proxy_ip": "172.19.0.6"},
            {"relay_secret_sha256": "abc"}, {"origin": "http://pairing.example.invalid"},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                Ingress(self.registry, self.server, self.options | overrides)
        with self.assertRaises(ValueError):
            Ingress(self.registry, SimpleNamespace(trusted_proxies=self.server.trusted_proxies), self.options)

    async def test_relay_requires_peer_secret_origin_and_core_forwarding(self):
        ingress = Ingress(self.registry, self.server, self.options)
        self.assertEqual((await ingress.http.status(self.request())).status, 404)
        with self.assertRaises(ValueError):
            await ingress.async_enable("https://ha.example.invalid")
        await ingress.async_enable(_ORIGIN)
        # No phone credential is present, so reaching the protocol returns 401.
        self.assertEqual((await ingress.http.status(self.request())).status, 401)
        invalid = (
            self.request(peer="172.19.0.6"), self.request(tls=False), self.request(headers={}),
            self.request(**{"X-Horeveil-Relay-Secret": token_hex(32)}),
            self.request(**{"X-Horeveil-Proxy-Origin": "https://ha.example.invalid"}),
            self.request(**{"X-Forwarded-Proto": "http"}), self.request(remote=_PEER),
            self.request(**{"X-Forwarded-For": _CLIENT + ", 192.0.2.11"}),
        )
        for request in invalid:
            self.assertEqual((await ingress.http.status(request)).status, 404)
        duplicates = self.request().headers.copy()
        duplicates.add("X-Horeveil-Relay-Secret", self.secret)
        self.assertEqual((await ingress.http.status(self.request(headers=duplicates))).status, 404)
        with patch.object(pairing_ha_module.remote, "is_cloud_request", is_cloud_request):
            token = is_cloud_request.set(True)
            try:
                self.assertEqual((await ingress.http.status(self.request())).status, 404)
            finally:
                is_cloud_request.reset(token)

    async def test_disable_restart_and_digest_rotation(self):
        options = self.options | {"upstream": "isolated_http", "isolation_verified": True}
        ingress = Ingress(self.registry, self.server, options)
        await ingress.async_enable(_ORIGIN)
        request = self.request(tls=False)
        self.assertEqual((await ingress.http.status(request)).status, 401)
        old_adapter = ingress.http
        await ingress.async_disable()
        self.assertEqual(self.discarded, [_ORIGIN])
        self.assertEqual((await ingress.http.status(request)).status, 404)
        await ingress.async_enable(_ORIGIN)
        self.assertEqual((await old_adapter.status(request)).status, 404)

        replacement = token_hex(32)
        rotated = Ingress(self.registry, self.server, options | {
            "relay_secret_sha256": sha256(replacement.encode()).hexdigest(),
        })
        self.assertIsNone(rotated.origin)
        await rotated.async_enable(_ORIGIN)
        self.assertEqual((await rotated.http.status(request)).status, 404)
        fresh = self.request(tls=False, **{"X-Horeveil-Relay-Secret": replacement})
        self.assertEqual((await rotated.http.status(fresh)).status, 401)
