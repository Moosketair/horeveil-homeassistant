"""Expose the latest fresh Horéveil snapshot as a read-only calendar."""

from bisect import bisect_left
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from . import HoreveilConfigEntry
from .receiver import SnapshotReceiver
from .snapshot import CalendarOccurrence, Snapshot

DISPLAY_DURATION = timedelta(minutes=1)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HoreveilConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one calendar per configured Horéveil receiver."""
    async_add_entities([HoreveilCalendar(entry.entry_id, entry.runtime_data)])


def _calendar_event(installation_id: str, occurrence: CalendarOccurrence) -> CalendarEvent:
    start = occurrence.sound_start
    end = start + min(DISPLAY_DURATION, datetime.max.replace(tzinfo=UTC) - start)
    local_start = dt_util.as_local(start)
    if dt_util.as_local(end) < local_start:
        # Core validates duration in local wall time. Stop just before a clock rollback.
        offset = local_start.utcoffset()
        microseconds = (end - start) // timedelta(microseconds=1)
        first_changed = bisect_left(
            range(microseconds + 1),
            True,
            key=lambda value: (
                dt_util.as_local(start + timedelta(microseconds=value)).utcoffset() != offset
            ),
        )
        end = start + timedelta(microseconds=max(0, first_changed - 1))

    description = "\n".join(
        f"{label} : {value}"
        for label, value in (("Catégorie", occurrence.category), ("Mode", occurrence.mode))
        if value.strip()
    )
    return CalendarEvent(
        start=start,
        end=end,
        summary=occurrence.name.strip() or "Réveil Horéveil",
        description=description or None,
        uid=f"{installation_id}/{occurrence.occurrence_id}",
    )


class HoreveilCalendar(CalendarEntity):
    """Display the effective alarm times provided by Android."""

    _attr_name = "Horéveil"
    _attr_should_poll = False
    _attr_supported_features = 0

    def __init__(self, entry_id: str, receiver: SnapshotReceiver) -> None:
        self._attr_unique_id = entry_id
        self._receiver = receiver
        self._snapshot: Snapshot | None = None
        self._events: list[CalendarEvent] = []
        self._cancel_expiry: Callable[[], None] | None = None

    @property
    def available(self) -> bool:
        """An empty fresh snapshot is still an available calendar."""
        return self._receiver.current is not None

    def _current_events(self) -> list[CalendarEvent]:
        snapshot = self._receiver.current
        if self._snapshot is not snapshot:
            self._events = (
                sorted(
                    (_calendar_event(snapshot.installation_id, row)
                     for row in snapshot.iter_calendar_occurrences()),
                    key=lambda event: (event.start, event.uid),
                )
                if snapshot is not None else []
            )
            self._snapshot = snapshot
        return self._events

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current or next event, ordered by its effective start."""
        now = dt_util.utcnow()
        return next((event for event in self._current_events() if event.end > now), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return events overlapping the requested half-open range."""
        start, end = start_date.astimezone(UTC), end_date.astimezone(UTC)
        if start >= end:
            return []
        return [event for event in self._current_events() if event.end > start and event.start < end]

    async def async_added_to_hass(self) -> None:
        """Refresh on committed revisions and at the snapshot's deadline."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._receiver.update_signal, self._async_refresh)
        )
        self.async_on_remove(self._async_cancel_expiry)
        self._async_schedule_expiry()

    @callback
    def _async_cancel_expiry(self) -> None:
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None

    @callback
    def _async_schedule_expiry(self) -> None:
        self._async_cancel_expiry()
        if (snapshot := self._receiver.current) is not None:
            seconds, nanos = snapshot.expires_at
            # Round up so a sub-microsecond expiry cannot fire before the deadline.
            deadline = seconds + timedelta(microseconds=(nanos + 999) // 1000)
            self._cancel_expiry = async_track_point_in_utc_time(
                self.hass, self._async_expired, deadline
            )

    @callback
    def _async_expired(self, _now: datetime) -> None:
        self._cancel_expiry = None
        self._async_refresh()

    @callback
    def _async_refresh(self) -> None:
        self._async_schedule_expiry()
        self.async_write_ha_state()
