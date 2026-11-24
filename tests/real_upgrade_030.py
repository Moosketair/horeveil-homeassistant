"""Reuse a Core profile created with the published Horéveil 0.3.0 code."""

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from shutil import copytree
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4
from zipfile import ZipFile

from aiohttp import ClientSession, TCPConnector, ThreadedResolver
from homeassistant import bootstrap, loader
from homeassistant.components.webhook import _HANDLERS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry

ROOT = Path(__file__).resolve().parents[1]


def snapshot():
    now = datetime.now(UTC)
    alarm = str(uuid4())
    start = now + timedelta(minutes=15)
    date = start.date().isoformat()
    return {
        "schema_version": 1, "event": "calendar_snapshot", "installation_id": str(uuid4()),
        "revision": 7, "generated_at": now.isoformat(),
        "window_start": (now - timedelta(hours=1)).isoformat(),
        "window_end_exclusive": (now - timedelta(hours=1) + timedelta(days=30)).isoformat(),
        "timezone": "UTC", "expires_at": (now + timedelta(hours=3)).isoformat(),
        "alarms": {alarm: {"name": "Alarme de recette", "category": "Test", "mode": "Test"}},
        "occurrences": [{
            "alarm_id": alarm, "nominal_date": date, "occurrence_id": f"{alarm}/{date}",
            "sound_start": start.isoformat(),
        }],
    }


async def phase(profile, upgrade):
    data_file = profile / "verification.json"
    before = json.loads(data_file.read_text("utf-8")) if upgrade else None
    port_socket = socket.socket()
    port_socket.bind(("127.0.0.1", 0))
    port = port_socket.getsockname()[1]
    port_socket.close()
    config = {"homeassistant": {"time_zone": "UTC"},
              "http": {"server_host": ["127.0.0.1"], "server_port": port}, "horeveil": {}}
    hass = HomeAssistant(str(profile))
    loader.async_setup(hass)
    try:
        assert await bootstrap.async_from_config_dict(config, hass) is hass
        await hass.async_start()
        if not upgrade:
            flow = await hass.config_entries.flow.async_init("horeveil", context={"source": "user"})
            assert (await hass.config_entries.flow.async_configure(flow["flow_id"], {}))["type"] == "create_entry"
        await hass.async_block_till_done()
        entries = hass.config_entries.async_entries("horeveil")
        assert len(entries) == 1 and entries[0].state is ConfigEntryState.LOADED
        entry = entries[0]
        identity = sha256(json.dumps(dict(entry.data), sort_keys=True).encode()).hexdigest()
        hook = hass.data[_HANDLERS][entry.data["webhook_id"]]
        assert hook.local_only and hook.allowed_methods == frozenset({"POST"})
        assert "horeveil.local_pairing_test" not in hass.data
        assert not any(x.startswith("horeveil/pairing_test/") for x in hass.data["websocket_api"])
        body = before["body"] if upgrade else snapshot()
        if upgrade:
            assert entry.runtime_data.current.installation_id == body["installation_id"]
            assert entry.runtime_data.current.revision == 7
            assert entry.runtime_data.received_at.isoformat() == before["received_at"]
            assert entry.entry_id == before["entry_id"] and identity == before["identity"]
        url = f"http://127.0.0.1:{hass.http.server_port}/api/webhook/{entry.data['webhook_id']}"
        async with ClientSession(connector=TCPConnector(resolver=ThreadedResolver())) as client:
            try:
                async with client.post(url, json=body) as response:
                    if response.status != 403:
                        raise RuntimeError(f"Refus sans jeton inattendu : {response.status}")
                async with client.post(url, json=body, headers={"X-Horeveil-Token": entry.data["relay_token"]}) as response:
                    result = await response.json()
                    if response.status != 200 or result.get("result") != ("duplicate" if upgrade else "accepted"):
                        raise RuntimeError(f"Résultat du récepteur inattendu : {response.status}, {result.get('result')}")
            except RuntimeError:
                raise
            except Exception as error:
                raise RuntimeError(f"Le contrôle du webhook de recette a échoué : {type(error).__name__}") from None
            await hass.async_block_till_done()
            entities = entity_registry.async_entries_for_config_entry(entity_registry.async_get(hass), entry.entry_id)
            assert sorted(x.domain for x in entities) == ["calendar", "sensor", "sensor", "sensor", "sensor"]
            calendar_id = next(entity.entity_id for entity in entities if entity.domain == "calendar")
            events = await hass.services.async_call("calendar", "get_events", {
                "entity_id": calendar_id, "start_date_time": body["generated_at"], "end_date_time": body["expires_at"],
            }, blocking=True, return_response=True)
            assert len(events[calendar_id]["events"]) == 1
            states = {x.entity_id: hass.states.get(x.entity_id).state for x in entities}
            assert "unavailable" not in states.values()
            assert "1" in states.values() and "fresh" in states.values()
            if upgrade:
                assert states == before["states"]
                try:
                    async with client.post(url, json=dict(body, revision=8),
                                           headers={"X-Horeveil-Token": entry.data["relay_token"]}) as response:
                        assert (await response.json())["result"] == "accepted"
                except Exception:
                    raise RuntimeError("La nouvelle révision de recette a été refusée") from None
                assert entry.runtime_data.current.revision == 8
            else:
                data_file.write_text(json.dumps({"states": states, "body": body, "entry_id": entry.entry_id,
                                                 "identity": identity, "received_at": entry.runtime_data.received_at.isoformat()}), "utf-8")
        print("Bascule réelle 0.3.0 : entrée, données, entités et webhook OK." if upgrade else "Profil natif 0.3.0 prêt.", flush=True)
    finally:
        await hass.async_stop(force=True)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        asyncio.run(phase(Path(sys.argv[1]), sys.argv[2] == "candidate"))
    else:
        with TemporaryDirectory(prefix="horeveil-upgrade-") as name:
            data = subprocess.check_output(
                ["git", "archive", "--format=zip", "0491149", "custom_components/horeveil"], cwd=ROOT,
            )
            with ZipFile(BytesIO(data)) as archive:
                archive.extractall(name)
            subprocess.run([sys.executable, "-X", "utf8", __file__, name, "baseline"], check=True)
            components = Path("custom_components/horeveil")
            copytree(ROOT / components, Path(name) / components, dirs_exist_ok=True)
            subprocess.run([sys.executable, "-X", "utf8", __file__, name, "candidate"], check=True)
