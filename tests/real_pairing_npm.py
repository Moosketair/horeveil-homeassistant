"""Exercise the official NPM container against Core on the Linux Docker host."""

import asyncio
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
from secrets import token_hex
from shutil import copytree
import socket
import ssl
import subprocess
from tempfile import TemporaryDirectory

from aiohttp import ClientConnectionError, ClientSession, ClientTimeout, FormData, TCPConnector
from aiohttp.abc import AbstractResolver
from homeassistant import bootstrap, loader
from homeassistant.core import HomeAssistant

from real_pairing_native import check_request, test_api
from real_pairing_smoke import _BASE, _DOMAIN, _PUBLIC_NAME, _ROOT, _certificate_files, _free_port, _token

_ADMIN = "ha.example.invalid"
_IMAGE = "jc21/nginx-proxy-manager:2.17.1@sha256:ddccef5311d962ce733bc3439f9efdc11f167888804e60c5912f8a37ecdc5d52"


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=240)
    if check and result.returncode:
        raise RuntimeError(f"Docker {args[0]} a échoué (code {result.returncode})")
    return result.stdout.strip()


@contextmanager
def npm_container(profile):
    name = "horeveil-npm-" + token_hex(6)
    docker("network", "create", name)
    try:
        docker(
            "run", "-d", "--name", name, "--network", name,
            "-p", "127.0.0.1::81", "-p", "127.0.0.1::80", "-p", "127.0.0.1::443",
            "--mount", f"type=bind,source={profile / 'proxy'},target=/data/nginx/custom",
            _IMAGE,
        )
        ports = [int(docker("port", name, f"{port}/tcp").rsplit(":", 1)[1]) for port in (81, 80, 443)]
        network = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", name))[name]
        yield name, *ports, network["Gateway"], network["IPAddress"]
    finally:
        docker("rm", "-fv", name, check=False)
        docker("network", "rm", name, check=False)


class TestDns(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        assert host in {_PUBLIC_NAME, _ADMIN}
        return [{"hostname": host, "host": "127.0.0.1", "port": port,
                 "family": socket.AF_INET, "proto": socket.IPPROTO_TCP, "flags": 0}]

    async def close(self):
        pass


async def configure_proxy(client, profile, api_port, public, gateway, core_port):
    url = f"http://127.0.0.1:{api_port}/api"
    for _ in range(100):
        try:
            async with client.get(url + "/") as response:
                if response.status == 200:
                    break
        except ClientConnectionError:
            pass
        await asyncio.sleep(1)
    else:
        raise RuntimeError("API NPM indisponible sur le port de recette")

    email, password = "recette@example.invalid", token_hex(24)
    await check_request(client, "POST", url + "/users", 201, json={
        "name": "Recette", "nickname": "Recette", "email": email, "roles": ["admin"],
        "auth": {"type": "password", "secret": password},
    })
    auth = await check_request(client, "POST", url + "/tokens", 200, json={"identity": email, "secret": password})
    headers = {"Authorization": "Bearer " + auth["token"]}

    certificate = url + "/nginx/certificates"
    hosts = url + "/nginx/proxy-hosts"
    common = {"forward_scheme": "https", "forward_host": gateway, "forward_port": core_port, "enabled": True}
    advanced = (_ROOT / "docs/npm-horeveil-pairing.conf").read_text("utf-8").replace(
        "https://pairing.example.invalid;", public + ";",
    ).replace("ha-interne.example.invalid", "horeveil-gateway.example.invalid")
    normal = """
location ^~ /api/horeveil/v1/pair/ { return 404; }
location ^~ /api/webhook/ { access_log off; return 404; }
proxy_ssl_verify on;
proxy_ssl_name horeveil-gateway.example.invalid;
proxy_ssl_trusted_certificate /data/nginx/custom/horeveil-upstream-ca.pem;
"""
    for hostname, pem, config in ((_PUBLIC_NAME, "public", advanced), (_ADMIN, "alternate", normal)):
        entry = await check_request(client, "POST", certificate, 201, headers=headers, json={
            "provider": "other", "nice_name": hostname, "domain_names": [hostname],
        })
        files = FormData()
        files.add_field("certificate", (profile / f"{pem}.pem").read_bytes(), filename="certificate.pem")
        files.add_field("certificate_key", (profile / f"{pem}.key").read_bytes(), filename="key.pem")
        await check_request(client, "POST", f"{certificate}/{entry['id']}/upload", 200, headers=headers, data=files)
        await check_request(client, "POST", hosts, 201, headers=headers, json={
            **common, "domain_names": [hostname], "advanced_config": config, "certificate_id": entry["id"],
            "allow_websocket_upgrade": hostname == _ADMIN,
        })


async def exercise(profile, secret, container, api_port, http_port, tls_port, gateway, peer):
    port = _free_port()
    origin = f"https://127.0.0.1:{port}"
    public, alternate = (f"https://{host}:{tls_port}" for host in (_PUBLIC_NAME, _ADMIN))
    config = {
        "homeassistant": {"external_url": alternate, "internal_url": origin},
        "http": {"server_host": ["127.0.0.1", gateway], "server_port": port,
                 "ssl_certificate": str(profile / "serveur.pem"), "ssl_key": str(profile / "serveur.key"),
                 "use_x_forwarded_for": True, "trusted_proxies": [peer, "127.0.0.1"]},
        "frontend": {},
        _DOMAIN: {"native_pairing": {"origin": public, "proxy_ip": peer, "upstream": "tls",
                                      "relay_secret_sha256": sha256(secret.encode()).hexdigest()}},
    }
    hass = HomeAssistant(str(profile))
    loader.async_setup(hass)
    try:
        assert await bootstrap.async_from_config_dict(config, hass) is hass
        await hass.async_start()
        admin = await _token(hass, "Administrateur NPM", alternate, administrator=True)
        guest = await _token(hass, "Utilisateur NPM", alternate, administrator=False)
        tls = ssl.create_default_context(cafile=profile / "ca.pem")
        async with ClientSession(connector=TCPConnector(ssl=tls, resolver=TestDns()), timeout=ClientTimeout(total=8)) as client:
            await configure_proxy(client, profile, api_port, public, gateway, port)
            docker("exec", container, "nginx", "-t")
            registry = hass.data["horeveil.local_pairing_test"].registry

            async def boundary(direct, dedicated):
                status = _BASE + "/status"
                attestation = {
                    "X-Forwarded-For": "192.0.2.55", "X-Forwarded-Proto": "https",
                    "X-Horeveil-Proxy-Origin": dedicated, "X-Horeveil-Relay-Secret": secret,
                }
                await check_request(client, "GET", direct + status, 404, headers=attestation)
                for offered, expected in ((None, "404"), (token_hex(32), "404"), (secret, "401")):
                    headers = dict(attestation)
                    headers.pop("X-Horeveil-Relay-Secret")
                    if offered is not None:
                        headers["X-Horeveil-Relay-Secret"] = offered
                    raw_headers = [value for key, item in headers.items() for value in ("-H", f"{key}: {item}")]
                    host = "horeveil-gateway.example.invalid"
                    result = await asyncio.to_thread(
                        docker, "exec", container, "curl", "-sS", "--max-time", "5", "-o", "/dev/null",
                        "-w", "%{http_code}", "--cacert", "/data/nginx/custom/horeveil-upstream-ca.pem",
                        "--resolve", f"{host}:{port}:{gateway}", *raw_headers, f"https://{host}:{port}{status}",
                    )
                    assert result == expected, (result, expected)

            await test_api(client, client, origin, public, f"http://{_PUBLIC_NAME}:{http_port}", alternate,
                           admin, guest, registry=registry, boundary=boundary)
            print(f"{_IMAGE} : configuration Advanced, routes et protocole validés.", flush=True)
    finally:
        await hass.async_stop(force=True)


if __name__ == "__main__":
    with TemporaryDirectory(prefix="horeveil-npm-") as directory:
        profile = Path(directory)
        copytree(_ROOT / "custom_components" / _DOMAIN, profile / "custom_components" / _DOMAIN)
        _certificate_files(profile, nginx=True, alternate_name=_ADMIN)
        proxy = profile / "proxy"
        proxy.mkdir(mode=0o700)
        (proxy / "horeveil-upstream-ca.pem").write_bytes((profile / "ca.pem").read_bytes())
        secret = token_hex(32)
        include = proxy / "horeveil-relay-secret.inc"
        include.touch(mode=0o600)
        include.write_text(f"proxy_set_header X-Horeveil-Relay-Secret {secret};\n", encoding="ascii")
        with npm_container(profile) as settings:
            asyncio.run(exercise(profile, secret, *settings))
