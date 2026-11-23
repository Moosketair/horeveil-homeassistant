"""Read-only sensors derived from the latest calendar snapshot."""

from bisect import bisect_right
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from . import HoreveilConfigEntry
from .receiver import SnapshotReceiver
from .snapshot import CalendarOccurrence, Snapshot

SENSORS = (
    SensorEntityDescription(
        key="next_alarm", translation_key="next_alarm",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    SensorEntityDescription(key="upcoming_alarms", translation_key="upcoming_alarms"),
    SensorEntityDescription(
        key="calendar_received_at", translation_key="calendar_received_at",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    SensorEntityDescription(
        key="calendar_freshness", translation_key="calendar_freshness",
        device_class=SensorDeviceClass.ENUM, options=["fresh", "expired", "empty"],
    ),
)
NEXT_ALARM_ATTRIBUTES = frozenset({"name", "category", "mode", "occurrence_id", "target_at"})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HoreveilConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Share one deadline and snapshot projection across the sensors."""
    schedule = SnapshotSensorSchedule(hass, entry.runtime_data)
    async_add_entities(
        [HoreveilSensor(entry.entry_id, description, schedule) for description in SENSORS]
    )


class SnapshotSensorSchedule:
    """Refresh on an accepted revision or the next relevant UTC deadline."""

    def __init__(self, hass: HomeAssistant, receiver: SnapshotReceiver) -> None:
        self._hass = hass
        self.receiver = receiver
        self._snapshot: Snapshot | None = None
        self._rows: list[CalendarOccurrence] = []
        self._listeners: set[Callable[[], None]] = set()
        self._unsubscribe: Callable[[], None] | None = None
        self._cancel_timer: Callable[[], None] | None = None
        self.next_alarm: CalendarOccurrence | None = None
        self.upcoming_count = 0

    @callback
    def async_subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Start tracking when the first enabled sensor is added."""
        if not self._listeners:
            self._unsubscribe = async_dispatcher_connect(
                self._hass, self.receiver.update_signal, self._async_refresh
            )
            self._async_refresh()
        self._listeners.add(listener)

        @callback
        def unsubscribe() -> None:
            self._listeners.remove(listener)
            if not self._listeners:
                if self._unsubscribe is not None:
                    self._unsubscribe()
                    self._unsubscribe = None
                self._async_cancel_timer()

        return unsubscribe

    @callback
    def _async_cancel_timer(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    @callback
    def _async_at_deadline(self, _now: datetime) -> None:
        self._cancel_timer = None
        self._async_refresh()

    @callback
    def _async_refresh(self) -> None:
        self._async_cancel_timer()
        snapshot = self.receiver.current
        if self._snapshot is not snapshot:
            self._rows = (
                sorted(snapshot.iter_calendar_occurrences(),
                       key=lambda row: (row.sound_start_instant, row.occurrence_id))
                if snapshot is not None else []
            )
            self._snapshot = snapshot

        now = dt_util.utcnow()
        instant = (now.replace(microsecond=0), now.microsecond * 1000)
        start = bisect_right(self._rows, instant, key=lambda row: row.sound_start_instant)
        self.upcoming_count = len(self._rows) - start
        self.next_alarm = self._rows[start] if self.upcoming_count else None
        if snapshot is not None and not snapshot.expired_at(now):
            deadline = snapshot.expires_at
            if self.next_alarm is not None:
                deadline = min(deadline, self.next_alarm.sound_start_instant)
            seconds, nanos = deadline
            self._cancel_timer = async_track_point_in_utc_time(
                self._hass, self._async_at_deadline,
                seconds + timedelta(microseconds=(nanos + 999) // 1000),
            )

        for listener in self._listeners:
            listener()


class HoreveilSensor(SensorEntity):
    """Expose a single value from the shared snapshot projection."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _unrecorded_attributes = NEXT_ALARM_ATTRIBUTES

    def __init__(
        self, entry_id: str, description: SensorEntityDescription, schedule: SnapshotSensorSchedule
    ) -> None:
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._schedule = schedule

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._schedule.async_subscribe(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return (
            self.entity_description.key in {"calendar_received_at", "calendar_freshness"}
            or self._schedule.receiver.current is not None
        )

    @property
    def native_value(self) -> datetime | int | str | None:
        if not self.available:
            return None
        match self.entity_description.key:
            case "next_alarm":
                alarm = self._schedule.next_alarm
                return alarm.sound_start if alarm is not None else None
            case "upcoming_alarms":
                return self._schedule.upcoming_count
            case "calendar_received_at":
                return self._schedule.receiver.received_at
            case "calendar_freshness":
                return self._schedule.receiver.freshness
        return None

    @property
    def extra_state_attributes(self) -> dict[str, str | None] | None:
        if self.entity_description.key != "next_alarm" or not self.available:
            return None
        if (alarm := self._schedule.next_alarm) is None:
            return None
        attributes = {
            "name": alarm.name.strip() or "Réveil Horéveil",
            "category": alarm.category or None,
            "mode": alarm.mode or None,
            "occurrence_id": alarm.occurrence_id,
        }
        if alarm.target_at is not None:
            attributes["target_at"] = alarm.target_at
        return attributes
