"""Targeted Python tests using real HTTP and a Home Assistant API test double."""

import asyncio
from copy import deepcopy
from datetime import datetime
import gzip
from io import BytesIO
import json
from pathlib import Path
from secrets import token_hex
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class DiskStore:
    """Model the public Store methods with independent reads after a restart."""

    def __init__(self, hass, version, key, **options):
        self.path = hass.directory / key
        self.version = version
        self.options = options
        self.writes = 0
        self.fail = False
        hass.store = self

    async def async_load(self):
        return json.loads(self.path.read_text("utf-8")) if self.path.exists() else None

    async def async_save(self, state):
        if not self.fail:
            self.path.write_text(json.dumps(state), encoding="utf-8")
            self.writes += 1

    async def async_remove(self):
        self.path.unlink(missing_ok=True)


class ConfigFlow:
    def __init_subclass__(cls, *, domain):
        cls.domain = domain

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}


def register(hass, domain, name, webhook_id, handler, **options):
    hass.hooks[webhook_id] = (handler, options)


def unregister(hass, webhook_id):
    hass.hooks.pop(webhook_id)


def fake_module(name, **attributes):
    module = ModuleType(name)
    vars(module).update(attributes)
    return module


with patch.dict(
    "sys.modules",
    {
        "homeassistant": fake_module("homeassistant"),
        "homeassistant.components": fake_module(
            "homeassistant.components",
            webhook=SimpleNamespace(
                async_register=register,
                async_unregister=unregister,
                async_generate_id=lambda: token_hex(32),
                async_generate_path=lambda key: f"/api/webhook/{key}",
            ),
        ),
        "homeassistant.config_entries": fake_module(
            "homeassistant.config_entries", ConfigEntry=SimpleNamespace, ConfigFlow=ConfigFlow
        ),
        "homeassistant.core": fake_module("homeassistant.core", HomeAssistant=object),
        "homeassistant.exceptions": fake_module(
            "homeassistant.exceptions", ConfigEntryError=RuntimeError
        ),
        "homeassistant.helpers": fake_module("homeassistant.helpers"),
        "homeassistant.helpers.storage": fake_module("homeassistant.helpers.storage", Store=DiskStore),
    },
):
    from custom_components.horeveil import async_remove_entry, async_setup_entry, async_unload_entry
    from custom_components.horeveil.config_flow import HoreveilConfigFlow
    from custom_components.horeveil.const import RELAY_HEADER, RELAY_TOKEN, WEBHOOK_ID
    from custom_components.horeveil import receiver as receiver_module
    from custom_components.horeveil.snapshot import MAX_BYTES


CONTRACT = Path(__file__).resolve().parents[1] / "docs" / "CALENDAR_SNAPSHOT_SCHEMA_V1.md"
EXAMPLE = json.loads(CONTRACT.read_text("utf-8").split("```json\n", 1)[1].split("```", 1)[0])


class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        workspace = Path(__file__).resolve().parents[1]
        directory = self.enterContext(TemporaryDirectory(dir=workspace))
        self.directory = Path(directory)
        flow = HoreveilConfigFlow()
        self.form = await flow.async_step_user()
        config = await flow.async_step_user({})
        self.entry = SimpleNamespace(entry_id="test-entry", data=config["data"])
        self.path = f"/api/webhook/{self.entry.data[WEBHOOK_ID]}"
        self.headers = {RELAY_HEADER: self.entry.data[RELAY_TOKEN], "Content-Type": "application/json"}
        await self.start_receiver()

        app = web.Application()
        app.router.add_route("*", "/api/webhook/{webhook_id}", self.dispatch)
        self.client = await self.enterAsyncContext(TestClient(TestServer(app)))

    async def start_receiver(self):
        self.hass = SimpleNamespace(
            directory=self.directory,
            hooks={},
            is_stopping=False,
            async_add_executor_job=asyncio.to_thread,
            async_create_task=asyncio.create_task,
        )
        await async_setup_entry(self.hass, self.entry)

    async def dispatch(self, request):
        webhook_id = request.match_info["webhook_id"]
        hook = self.hass.hooks.get(webhook_id)
        if hook is None:
            return web.Response()
        handler, options = hook
        if request.method not in options["allowed_methods"]:
            return web.Response(status=405)
        return await handler(self.hass, webhook_id, request)

    async def post(self, snapshot=None, *, raw=None, headers=None, path=None):
        if raw is None:
            raw = json.dumps(EXAMPLE if snapshot is None else snapshot).encode()
        response = await self.client.post(
            path or self.path, data=raw, headers=self.headers if headers is None else headers
        )
        return response.status, await response.json() if response.content_type == "application/json" else None

    async def test_ui_credentials_and_local_post_registration(self):
        self.assertEqual(self.form["description_placeholders"]["path"], self.path)
        self.assertEqual(self.form["description_placeholders"]["token"], self.entry.data[RELAY_TOKEN])
        self.assertRegex(self.entry.data[WEBHOOK_ID], r"^[a-f0-9]{64}$")
        self.assertRegex(self.entry.data[RELAY_TOKEN], r"^[a-f0-9]{64}$")
        self.assertNotEqual(self.entry.data[RELAY_TOKEN], self.entry.data[WEBHOOK_ID])
        self.assertEqual(self.hass.hooks[self.entry.data[WEBHOOK_ID]][1],
                         {"local_only": True, "allowed_methods": ("POST",)})
        self.assertEqual(self.hass.store.options,
                         {"atomic_writes": True, "private": True, "serialize_in_event_loop": False})

    async def test_contract_example_and_unchanged_payload(self):
        raw = json.dumps(EXAMPLE, indent=2, ensure_ascii=False).encode("utf-8")
        self.assertEqual(await self.post(raw=raw), (200, {"result": "accepted"}))
        stored = await self.hass.store.async_load()
        self.assertEqual(stored["snapshot"].encode("utf-8"), raw)
        self.assertIsNotNone(datetime.fromisoformat(stored["received_at"]).tzinfo)

    async def test_empty_snapshot_and_catalog(self):
        self.assertEqual((await self.post(dict(EXAMPLE, occurrences=[], alarms={})))[0], 200)

    async def test_catalog_optional_unused_entries_and_implicit_target(self):
        snapshot = deepcopy(EXAMPLE)
        del snapshot["alarms"]
        del snapshot["occurrences"][0]["target_at"]
        self.assertEqual((await self.post(snapshot))[0], 200)
        self.assertEqual((await self.post(dict(EXAMPLE, occurrences=[], revision=8)))[0], 200)

    async def test_duplicate_ignores_object_order_and_whitespace(self):
        await self.post()
        self.assertEqual(await self.post(raw=json.dumps(EXAMPLE, sort_keys=True)),
                         (200, {"result": "duplicate"}))
        self.assertEqual(self.hass.store.writes, 1)

    async def test_same_revision_conflict_in_extension(self):
        await self.post(raw=json.dumps(EXAMPLE)[:-1] + ',"extension": [true, 1.00000000000000001]}')
        self.assertEqual((await self.post(dict(EXAMPLE, extension=[1, 1])))[1],
                         {"result": "revision_conflict"})
        self.assertEqual((await self.post(dict(EXAMPLE, extension=[True, 1])))[0], 409)
        self.assertEqual(self.hass.store.writes, 1)

    async def test_old_new_and_different_installation(self):
        await self.post()
        self.assertEqual(await self.post(dict(EXAMPLE, revision=6)),
                         (409, {"result": "old_revision"}))
        self.assertEqual((await self.post(dict(EXAMPLE, revision=8)))[0], 200)
        different = dict(EXAMPLE, installation_id="00000000-0000-0000-0000-000000000003", revision=9)
        self.assertEqual(await self.post(different), (409, {"result": "different_installation"}))
        self.assertEqual(json.loads((await self.hass.store.async_load())["snapshot"])["revision"], 8)

    async def test_invalid_json_and_media_type(self):
        for raw in (b"{", b"\xff", b"[]", b'{"key":1,"key":2}', b'{"number":NaN}'):
            with self.subTest(raw=raw):
                self.assertEqual((await self.post(raw=raw))[0], 400)
        self.assertEqual((await self.post(headers={**self.headers, "Content-Type": "text/plain"}))[0], 415)
        self.assertEqual(
            (await self.post(raw=gzip.compress(b"{}"), headers={**self.headers, "Content-Encoding": "gzip"}))[0],
            415,
        )
        self.assertIsNone(await self.hass.store.async_load())

    async def test_exact_limit_and_excessive_fixed_or_chunked_body(self):
        raw = json.dumps(EXAMPLE).encode()
        raw = raw.ljust(MAX_BYTES, b" ")
        self.assertEqual((await self.post(raw=BytesIO(raw)))[0], 200)
        self.assertEqual((await self.post(raw=BytesIO(raw + b" ")))[0], 413)

        async def extra_chunks():
            yield raw
            yield b" "

        self.assertEqual((await self.post(raw=extra_chunks()))[0], 413)
        self.assertEqual(self.hass.store.writes, 1)

    async def test_invalid_envelope_dates_and_timezone(self):
        for field, value in (
            ("schema_version", True), ("event", "alarm_event"), ("installation_id", "bad"),
            ("revision", True), ("revision", 2**63), ("timezone", "invalid/zone"),
            ("window_end_exclusive", "2027-04-26T08:00:00Z"),
            ("window_start", "2027-03-27T08:00:00+01:00"),
            ("expires_at", EXAMPLE["generated_at"]), ("expires_at", "2027-05-01T00:00:00Z"),
        ):
            with self.subTest(field=field, value=value):
                self.assertEqual((await self.post(dict(EXAMPLE, **{field: value})))[0], 400)
        self.assertIsNone(await self.hass.store.async_load())

    async def test_window_bounds_when_local_time_is_missing_or_ambiguous(self):
        windows = (
            ("2027-02-26T01:30:00Z", "2027-03-28T01:30:00Z", "2027-02-27T01:30:00Z"),
            ("2027-10-01T00:30:00Z", "2027-10-31T00:30:00Z", "2027-10-02T00:30:00Z"),
        )
        for revision, (start, end, expires) in enumerate(windows, start=1):
            with self.subTest(end=end):
                snapshot = dict(EXAMPLE, window_start=start, window_end_exclusive=end,
                                generated_at=start, expires_at=expires, occurrences=[], revision=revision)
                self.assertEqual((await self.post(snapshot))[0], 200)

    async def test_occurrence_and_catalog_validation_is_atomic(self):
        await self.post()
        occurrence = EXAMPLE["occurrences"][0]
        for changes in (
            {"alarms": []}, {"alarms": {}}, {"alarms": {occurrence["alarm_id"]: {"name": 123}}},
            {"occurrences": [occurrence, occurrence]},
            {"occurrences": [occurrence, dict(occurrence, occurrence_id="other")]},
            {"occurrences": [dict(occurrence, nominal_date="2027-02-30")]},
            {"occurrences": [dict(occurrence, sound_start=EXAMPLE["window_end_exclusive"])]},
            {"occurrences": [dict(occurrence, target_at=EXAMPLE["window_start"])]},
        ):
            with self.subTest(changes=changes):
                self.assertEqual((await self.post(dict(EXAMPLE, revision=8, **changes)))[0], 400)
        self.assertEqual(self.hass.store.writes, 1)

    async def test_expiration_boundary_at_nanosecond_precision(self):
        expiry = "2027-03-28T08:00:00.000000001Z"
        await self.post(dict(EXAMPLE, expires_at=expiry))
        receiver = self.entry.runtime_data
        with patch.object(receiver_module, "datetime") as clock:
            clock.now.return_value = datetime.fromisoformat("2027-03-28T08:00:00Z")
            self.assertEqual(receiver.freshness, "fresh")
            self.assertIsNotNone(receiver.current)
            clock.now.return_value = datetime.fromisoformat("2027-03-28T08:00:00.000001Z")
            self.assertEqual(receiver.freshness, "expired")
            self.assertIsNone(receiver.current)

    async def test_restart_restores_expired_watermark_and_disables_unloaded_webhook(self):
        snapshot = dict(EXAMPLE, window_start="2020-01-01T00:00:00Z", timezone="UTC",
                        generated_at="2020-01-01T00:00:00Z", expires_at="2020-01-02T00:00:00Z",
                        window_end_exclusive="2020-01-31T00:00:00Z", occurrences=[])
        self.assertEqual(self.entry.runtime_data.freshness, "empty")
        await self.post(snapshot)
        previous = self.hass
        await async_unload_entry(previous, self.entry)
        self.assertFalse(previous.hooks)
        await self.start_receiver()
        self.assertEqual(self.entry.runtime_data.freshness, "expired")
        self.assertIsNone(self.entry.runtime_data.current)
        self.assertEqual((await self.post(dict(snapshot, revision=6)))[0], 409)
        self.assertEqual(await self.post(snapshot), (200, {"result": "duplicate"}))
        await async_unload_entry(self.hass, self.entry)
        await async_remove_entry(self.hass, self.entry)
        self.assertFalse(self.hass.store.path.exists())

    async def test_unauthorized_and_unsupported_method(self):
        for headers in (
            {"Content-Type": "application/json"},
            {**self.headers, RELAY_HEADER: token_hex(32)},
        ):
            self.assertEqual(await self.post(headers=headers), (403, {"result": "unauthorized"}))
        self.assertEqual(await self.post(path="/api/webhook/unknown"), (200, None))
        self.assertEqual((await self.client.put(self.path, headers=self.headers)).status, 405)
        self.assertIsNone(await self.hass.store.async_load())

    async def test_storage_failure_can_retry_without_applying_revision(self):
        self.hass.store.fail = True
        self.assertEqual(await self.post(), (503, {"result": "storage_unavailable"}))
        self.assertEqual(self.entry.runtime_data.freshness, "empty")
        self.hass.store.fail = False
        self.assertEqual(await self.post(), (200, {"result": "accepted"}))

    async def test_concurrent_writers_do_not_overwrite_latest_revision(self):
        results = await asyncio.gather(self.post(), self.post(dict(EXAMPLE, revision=8)))
        self.assertEqual(results[1], (200, {"result": "accepted"}))
        self.assertIn(results[0][0], (200, 409))
        self.assertEqual(json.loads((await self.hass.store.async_load())["snapshot"])["revision"], 8)
