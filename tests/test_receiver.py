"""Targeted Python tests using real HTTP and a Home Assistant API test double."""

import asyncio
from copy import deepcopy
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import gzip
from io import BytesIO
import json
from pathlib import Path
from secrets import token_hex
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
# Keep these real modules loaded when patch.dict restores sys.modules.
from cryptography import x509  # noqa: F401
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: F401


class DiskStore:
    """Model the public Store methods with independent reads after a restart."""

    def __init__(self, hass, version, key, **options):
        self.hass = hass
        self.key = key
        self.version = version
        self.options = options
        self.writes = 0
        self.fail = False
        hass.store = self

    @property
    def path(self):
        return self.hass.directory / self.key

    async def async_load(self):
        return json.loads(self.path.read_text("utf-8")) if self.path.exists() else None

    async def async_save(self, state):
        if not self.fail:
            self.path.parent.mkdir(exist_ok=True)
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


@dataclass
class CalendarEvent:
    start: datetime
    end: datetime
    summary: str
    description: str | None
    uid: str

    def __post_init__(self):
        # Core validates duration after converting to Home Assistant's local timezone.
        if dt_api.as_local(self.end) < dt_api.as_local(self.start):
            raise ValueError("Negative local event duration")


class CalendarEntity:
    async def async_added_to_hass(self):
        self.remove_callbacks = []
        self.writes = 0

    def async_on_remove(self, callback):
        self.remove_callbacks.append(callback)

    def async_write_ha_state(self):
        self.writes += 1
        self.latest_state = (self.available, self.event)


@dataclass
class SensorEntityDescription:
    key: str
    translation_key: str
    device_class: str | None = None
    options: list[str] | None = None


class SensorEntity(CalendarEntity):
    def async_write_ha_state(self):
        self.writes += 1
        self.latest_state = (self.available, self.native_value, self.extra_state_attributes)


class ConfigEntries:
    def __init__(self, hass):
        self.hass = hass

    async def async_forward_entry_setups(self, entry, platforms):
        assert platforms == ("calendar", "sensor")
        entities = []
        await setup_calendar(self.hass, entry, entities.extend)
        self.calendar = entities[0]
        await setup_sensors(self.hass, entry, entities.extend)
        self.sensors = {entity.entity_description.key: entity for entity in entities[1:]}
        for entity in entities:
            entity.hass = self.hass
            await entity.async_added_to_hass()
            entity.async_write_ha_state()

    async def async_unload_platforms(self, entry, platforms):
        assert platforms == ("calendar", "sensor")
        for entity in (self.calendar, *self.sensors.values()):
            for callback in entity.remove_callbacks:
                callback()
        return True


def add_callback(callbacks, key, callback):
    registered = callbacks.setdefault(key, [])
    registered.append(callback)

    def cancel():
        registered.remove(callback)
        if not registered:
            del callbacks[key]

    return cancel


def connect(hass, signal, callback):
    return add_callback(hass.listeners, signal, callback)


def send(hass, signal):
    for callback in hass.listeners.get(signal, ()):
        callback()


def track_time(hass, callback, point):
    return add_callback(hass.timers, point, callback)


dt_api = SimpleNamespace(
    utcnow=lambda: datetime.now(UTC),
    as_local=lambda value: value.astimezone(ZoneInfo("Europe/Paris")),
)


def register(hass, domain, name, webhook_id, handler, **options):
    hass.hooks[webhook_id] = (handler, options)


def unregister(hass, webhook_id):
    hass.hooks.pop(webhook_id)


def fake_module(name, **attributes):
    module = ModuleType(name)
    vars(module).update(attributes)
    return module


current_request = ContextVar("current_request", default=None)
is_cloud_request = ContextVar("is_cloud_request", default=False)


def websocket_command(schema):
    def decorate(handler):
        handler.command = schema["type"]
        return handler
    return decorate


def require_admin(handler):
    async def enforce(hass, connection, msg):
        if connection.user is None or not connection.user.is_admin:
            connection.send_error(msg["id"], "unauthorized", "Administrator required")
            return
        await handler(hass, connection, msg)
    return enforce


async def register_panel(hass, **options):
    hass.panels[options["frontend_url_path"]] = options


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
            websocket_api=SimpleNamespace(
                websocket_command=websocket_command,
                require_admin=require_admin,
                async_response=lambda handler: handler,
                async_register_command=lambda hass, handler: hass.ws_commands.update(
                    {handler.command: handler},
                ),
            ),
        ),
        "homeassistant.components.http": fake_module(
            "homeassistant.components.http", HomeAssistantView=object, current_request=current_request,
        ),
        "homeassistant.components.panel_custom": fake_module(
            "homeassistant.components.panel_custom", async_register_panel=register_panel,
        ),
        "homeassistant.config_entries": fake_module(
            "homeassistant.config_entries", ConfigEntry=SimpleNamespace, ConfigFlow=ConfigFlow
        ),
        "homeassistant.components.calendar": fake_module(
            "homeassistant.components.calendar", CalendarEntity=CalendarEntity,
            CalendarEvent=CalendarEvent,
        ),
        "homeassistant.components.sensor": fake_module(
            "homeassistant.components.sensor", SensorEntity=SensorEntity,
            SensorEntityDescription=SensorEntityDescription,
            SensorDeviceClass=SimpleNamespace(TIMESTAMP="timestamp", ENUM="enum"),
        ),
        "homeassistant.const": fake_module(
            "homeassistant.const", Platform=SimpleNamespace(CALENDAR="calendar", SENSOR="sensor"),
            EVENT_HOMEASSISTANT_STOP="homeassistant_stop",
        ),
        "homeassistant.core": fake_module(
            "homeassistant.core", HomeAssistant=object, callback=lambda function: function
        ),
        "homeassistant.exceptions": fake_module(
            "homeassistant.exceptions", ConfigEntryError=RuntimeError, HomeAssistantError=RuntimeError
        ),
        "homeassistant.helpers": fake_module("homeassistant.helpers"),
        "homeassistant.helpers.dispatcher": fake_module(
            "homeassistant.helpers.dispatcher", async_dispatcher_connect=connect,
            async_dispatcher_send=send,
        ),
        "homeassistant.helpers.entity_platform": fake_module(
            "homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object
        ),
        "homeassistant.helpers.event": fake_module(
            "homeassistant.helpers.event", async_track_point_in_utc_time=track_time
        ),
        "homeassistant.helpers.storage": fake_module("homeassistant.helpers.storage", Store=DiskStore),
        "homeassistant.util": fake_module("homeassistant.util", dt=dt_api),
        "hass_nabucasa": fake_module(
            "hass_nabucasa", remote=SimpleNamespace(is_cloud_request=is_cloud_request),
        ),
        "probatio": fake_module(
            "probatio", Optional=lambda name: name, Any=lambda *choices: choices,
            Schema=lambda _schema, **_options: dict, ALLOW_EXTRA=object(),
        ),
    },
):
    from custom_components.horeveil import async_remove_entry, async_setup_entry, async_unload_entry
    from custom_components.horeveil.calendar import async_setup_entry as setup_calendar
    from custom_components.horeveil.config_flow import HoreveilConfigFlow
    from custom_components.horeveil.const import RELAY_HEADER, RELAY_TOKEN, WEBHOOK_ID
    from custom_components.horeveil import pairing as pairing_module
    from custom_components.horeveil import pairing_http as pairing_http_module
    from custom_components.horeveil import pairing_ha as pairing_ha_module
    from custom_components.horeveil import async_setup as setup_domain
    from custom_components.horeveil import receiver as receiver_module
    from custom_components.horeveil.sensor import async_setup_entry as setup_sensors
    from custom_components.horeveil.snapshot import MAX_BYTES


CONTRACT = Path(__file__).resolve().parents[1] / "docs" / "CALENDAR_SNAPSHOT_SCHEMA_V1.md"
EXAMPLE = json.loads(CONTRACT.read_text("utf-8").split("```json\n", 1)[1].split("```", 1)[0])


class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime.fromisoformat("2027-03-27T08:00:00Z")
        self.enterContext(patch.object(receiver_module, "datetime", wraps=datetime)).now.side_effect = (
            lambda _zone: self.now
        )
        self.enterContext(patch.object(dt_api, "utcnow", lambda: self.now))
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
            listeners={},
            timers={},
            is_stopping=False,
            async_add_executor_job=asyncio.to_thread,
            async_create_task=asyncio.create_task,
        )
        self.hass.config_entries = ConfigEntries(self.hass)
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

    def advance_to(self, instant):
        self.now = instant
        due = sorted(point for point in self.hass.timers if point <= instant)
        for point in due:
            for callback in self.hass.timers.pop(point, ()):
                callback(point)

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

    @property
    def calendar(self):
        return self.hass.config_entries.calendar

    async def events(self, start="2027-03-27T08:00:00Z", end="2027-04-26T07:00:00Z"):
        return await self.calendar.async_get_events(
            self.hass, datetime.fromisoformat(start), datetime.fromisoformat(end)
        )

    async def test_calendar_shows_sorted_effective_occurrences_and_catalog_details(self):
        first = EXAMPLE["occurrences"][0]
        second = dict(
            first, nominal_date="2027-03-29", sound_start="2027-03-29T05:00:00Z",
            target_at="2027-03-29T05:05:00Z",
            occurrence_id=f"{first['alarm_id']}/2027-03-29",
        )
        self.assertFalse(self.calendar.available)
        self.assertEqual((await self.post(dict(EXAMPLE, occurrences=[second, first])))[0], 200)
        events = await self.events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].summary, "Réveil du matin")
        self.assertEqual(events[0].description, "Catégorie : Travail\nMode : Matin")
        self.assertEqual(events[0].start, datetime.fromisoformat(first["sound_start"]))
        self.assertEqual(events[0].end - events[0].start, timedelta(minutes=1))
        self.assertEqual(events[0].start.astimezone(ZoneInfo("Europe/Paris")).hour, 3)
        self.assertEqual(self.calendar.latest_state, (True, events[0]))
        self.assertEqual(self.calendar._attr_supported_features, 0)
        self.now = events[0].end
        self.assertEqual(self.calendar.event, events[1])

    async def test_calendar_fallback_with_missing_catalog_or_blank_name(self):
        snapshot = deepcopy(EXAMPLE)
        del snapshot["alarms"]
        await self.post(snapshot)
        self.assertEqual((await self.events())[0].summary, "Réveil Horéveil")
        self.assertIsNone((await self.events())[0].description)
        catalog = {EXAMPLE["occurrences"][0]["alarm_id"]: {"name": "  ", "mode": "Nuit"}}
        await self.post(dict(snapshot, revision=8, alarms=catalog))
        event, = await self.events()
        self.assertEqual(event.summary, "Réveil Horéveil")
        self.assertEqual(event.description, "Mode : Nuit")

    async def test_calendar_period_uses_exclusive_boundaries_and_accepts_other_timezones(self):
        await self.post()
        self.assertEqual(await self.events(end="2027-03-28T03:45:00+02:00"), [])
        self.assertEqual(await self.events(start="2027-03-28T03:46:00+02:00"), [])
        self.assertEqual(len(await self.events(
            start="2027-03-28T03:45:30+02:00", end="2027-03-28T03:46:00+02:00"
        )), 1)
        self.assertEqual(await self.events(
            start="2027-03-28T03:45:30+02:00", end="2027-03-28T03:45:30+02:00"
        ), [])

    async def test_calendar_fall_back_preserves_instants_and_shortens_crossing_event(self):
        source = EXAMPLE["occurrences"][0]
        rows = [
            dict(source, occurrence_id=f"{source['alarm_id']}/{day}", nominal_date=day,
                 sound_start=start)
            for day, start in (
                ("2027-10-31", "2027-10-31T00:59:30Z"),
                ("2027-11-01", "2027-10-31T01:59:30Z"),
            )
        ]
        for row in rows:
            del row["target_at"]
        snapshot = dict(
            EXAMPLE, window_start="2027-10-02T08:00:00Z", window_end_exclusive="2027-11-01T09:00:00Z",
            generated_at="2027-10-02T08:00:00Z", expires_at="2027-10-31T08:00:00Z", occurrences=rows,
        )
        self.now = datetime.fromisoformat("2027-10-31T00:00:00Z")
        self.assertEqual((await self.post(snapshot))[0], 200)
        begin = datetime(2027, 10, 31, 2, 58, tzinfo=ZoneInfo("Europe/Paris"))
        end = begin.replace(fold=1)
        first, = await self.calendar.async_get_events(self.hass, begin, end)
        self.assertEqual(first.start, datetime.fromisoformat(rows[0]["sound_start"]))
        self.assertEqual(first.end, datetime.fromisoformat("2027-10-31T00:59:59.999999Z"))
        events = await self.events("2027-10-31T00:00:00Z", "2027-10-31T03:00:00Z")
        self.assertEqual(len(events), 2)
        self.assertEqual([dt_api.as_local(event.start).fold for event in events], [0, 1])

    async def test_calendar_new_revision_replaces_events_and_keeps_uid_after_move(self):
        stale = dict(EXAMPLE, occurrences=[])
        first = EXAMPLE["occurrences"][0]
        deleted = dict(first, occurrence_id=f"{first['alarm_id']}/2027-03-29",
                       nominal_date="2027-03-29", sound_start="2027-03-29T01:45:00Z",
                       target_at="2027-03-29T01:50:00Z")
        await self.post(dict(EXAMPLE, occurrences=[first, deleted]))
        original, _ = await self.events()
        previous_writes = self.calendar.writes
        moved = dict(first, sound_start="2027-03-28T06:30:00Z", target_at="2027-03-28T06:35:00Z")
        self.assertEqual((await self.post(dict(EXAMPLE, revision=8, occurrences=[moved])))[0], 200)
        event, = await self.events()
        self.assertEqual(event.uid, original.uid)
        self.assertEqual(event.start, datetime.fromisoformat(moved["sound_start"]))
        self.assertEqual(self.calendar.writes, previous_writes + 1)
        self.assertEqual((await self.post(stale))[0], 409)
        self.assertEqual((await self.events())[0].start, event.start)
        self.assertEqual(self.calendar.writes, previous_writes + 1)

    async def test_calendar_empty_snapshot_clears_previous_events_but_is_available(self):
        await self.post()
        await self.post(dict(EXAMPLE, revision=8, occurrences=[], alarms={}))
        self.assertEqual(self.calendar.latest_state, (True, None))
        self.assertEqual(await self.events(), [])

    async def test_calendar_expires_at_deadline_without_new_webhook_and_recovers(self):
        await self.post(dict(EXAMPLE, expires_at="2027-03-28T08:00:00.000000001Z"))
        deadline = datetime.fromisoformat("2027-03-28T08:00:00.000001Z")
        self.assertIn(deadline, self.hass.timers)
        self.advance_to(deadline)
        self.assertEqual(self.calendar.latest_state, (False, None))
        self.assertEqual(await self.events(), [])
        await self.post(dict(EXAMPLE, revision=8, expires_at="2027-03-29T08:00:00Z"))
        self.assertTrue(self.calendar.available)
        self.assertEqual(len(await self.events()), 1)

    async def test_calendar_restoration_reuses_entry_and_unload_removes_subscription(self):
        await self.post()
        uid = self.calendar._attr_unique_id
        previous = self.hass
        await async_unload_entry(previous, self.entry)
        self.assertFalse(previous.timers)
        self.assertFalse(previous.listeners)
        await self.start_receiver()
        self.assertEqual(self.calendar._attr_unique_id, uid)
        self.assertEqual((await self.events())[0].summary, "Réveil du matin")
        await async_unload_entry(self.hass, self.entry)
        self.now = datetime.fromisoformat(EXAMPLE["expires_at"])
        await self.start_receiver()
        self.assertFalse(self.calendar.available)
        self.assertEqual(await self.events(), [])

    @property
    def sensors(self):
        return self.hass.config_entries.sensors

    async def test_sensors_show_next_alarm_metadata_and_strict_future_count(self):
        occurrence = EXAMPLE["occurrences"][0]
        later = dict(
            occurrence, occurrence_id=f"{occurrence['alarm_id']}/2027-03-29",
            nominal_date="2027-03-29", sound_start="2027-03-29T05:00:00Z",
            target_at="2027-03-29T05:05:00Z",
        )
        at_now = dict(
            occurrence, occurrence_id=f"{occurrence['alarm_id']}/2027-03-26",
            nominal_date="2027-03-26", sound_start=self.now.isoformat(),
        )
        await self.post(dict(EXAMPLE, occurrences=[later, at_now, occurrence]))
        next_alarm = self.sensors["next_alarm"]
        self.assertEqual(next_alarm.entity_description.device_class, "timestamp")
        self.assertEqual(next_alarm.native_value, datetime.fromisoformat(occurrence["sound_start"]))
        self.assertEqual(next_alarm.extra_state_attributes, {
            "name": "Réveil du matin", "category": "Travail", "mode": "Matin",
            "occurrence_id": occurrence["occurrence_id"], "target_at": occurrence["target_at"],
        })
        self.assertEqual(self.sensors["upcoming_alarms"].native_value, 2)

    async def test_sensors_advance_at_sound_start_without_webhook_or_calendar_minute(self):
        first = EXAMPLE["occurrences"][0]
        later = dict(
            first, occurrence_id=f"{first['alarm_id']}/2027-03-29",
            nominal_date="2027-03-29", sound_start="2027-03-28T02:45:00Z",
            target_at="2027-03-28T02:50:00Z",
        )
        await self.post(dict(EXAMPLE, occurrences=[first, later]))
        first_time, later_time = (datetime.fromisoformat(row["sound_start"]) for row in (first, later))
        self.advance_to(first_time)
        self.assertEqual(self.sensors["next_alarm"].latest_state[1], later_time)
        self.assertEqual(self.sensors["upcoming_alarms"].latest_state[1], 1)
        self.assertEqual(self.calendar.event.start, first_time)
        self.advance_to(later_time)
        self.assertEqual(self.sensors["next_alarm"].latest_state, (True, None, None))
        self.assertEqual(self.sensors["upcoming_alarms"].latest_state[1], 0)

    async def test_sensors_distinguish_missing_empty_and_expired_snapshots(self):
        next_alarm, count = (self.sensors[key] for key in ("next_alarm", "upcoming_alarms"))
        freshness = self.sensors["calendar_freshness"]
        receipt = self.sensors["calendar_received_at"]
        self.assertEqual(freshness.native_value, "empty")
        self.assertIsNone(receipt.native_value)
        self.assertFalse(next_alarm.available)
        self.assertFalse(count.available)
        await self.post(dict(EXAMPLE, occurrences=[], alarms={}))
        accepted_at = self.now
        self.assertEqual(next_alarm.latest_state, (True, None, None))
        self.assertEqual(count.latest_state, (True, 0, None))
        self.assertEqual(freshness.native_value, "fresh")
        self.advance_to(datetime.fromisoformat(EXAMPLE["expires_at"]))
        self.assertEqual(freshness.latest_state[1], "expired")
        self.assertEqual(next_alarm.latest_state, (False, None, None))
        self.assertEqual(count.latest_state, (False, None, None))
        self.assertEqual(receipt.latest_state[1], accepted_at)

    async def test_sensors_keep_receipt_across_duplicate_failure_and_restart(self):
        first_received = self.now
        await self.post()
        self.now += timedelta(minutes=1)
        self.assertEqual((await self.post())[1], {"result": "duplicate"})
        self.hass.store.fail = True
        self.assertEqual((await self.post(dict(EXAMPLE, revision=8)))[0], 503)
        self.assertEqual(self.sensors["calendar_received_at"].native_value, first_received)
        self.hass.store.fail = False
        self.assertEqual((await self.post(dict(EXAMPLE, revision=8, occurrences=[])))[0], 200)
        received = self.now
        self.assertEqual(self.sensors["calendar_received_at"].latest_state[1], received)
        self.assertEqual(self.sensors["upcoming_alarms"].latest_state[1], 0)
        self.assertEqual(await self.events(), [])
        await async_unload_entry(self.hass, self.entry)
        self.now = datetime.fromisoformat(EXAMPLE["expires_at"])
        await self.start_receiver()
        self.assertEqual(self.sensors["calendar_received_at"].native_value, received)
        self.assertEqual(self.sensors["calendar_freshness"].native_value, "expired")

    async def test_sensors_use_utc_and_handle_nanoseconds_and_optional_target(self):
        source = EXAMPLE["occurrences"][0]
        row = {**source, "sound_start": "2027-03-28T01:45:00.000000001+00:00"}
        del row["target_at"]
        snapshot = dict(EXAMPLE, occurrences=[row])
        del snapshot["alarms"]
        self.now = datetime.fromisoformat("2027-03-28T01:45:00Z")
        await self.post(snapshot)
        alarm = self.sensors["next_alarm"]
        self.assertEqual(alarm.native_value.tzinfo, UTC)
        self.assertEqual(dt_api.as_local(alarm.native_value).hour, 3)
        self.assertEqual(alarm.extra_state_attributes["name"], "Réveil Horéveil")
        self.assertNotIn("target_at", alarm.extra_state_attributes)
        self.assertEqual(self.sensors["upcoming_alarms"].native_value, 1)
        self.advance_to(self.now + timedelta(microseconds=1))
        self.assertIsNone(alarm.native_value)
        self.assertEqual(self.sensors["upcoming_alarms"].native_value, 0)
