"""Decode the Android calendar_snapshot v1 contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_BYTES = 1_048_576
_INSTANT = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(?:Z|\+00:00)"
)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_OFFSET = re.compile(r"(?:UTC|GMT|UT)?([+-])([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?")
type Instant = tuple[datetime, int]


class InvalidSnapshot(ValueError):
    """The request is not a valid calendar snapshot."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A validated snapshot retaining the unmodified UTF-8 JSON text."""

    installation_id: str
    revision: int
    expires_at: Instant
    raw: str = field(repr=False)
    _content: dict[str, Any] = field(repr=False)

    def expired_at(self, now: datetime) -> bool:
        """Expiration is inclusive and preserves Android's nanosecond precision."""
        return (now.replace(microsecond=0), now.microsecond * 1000) >= self.expires_at

    def same_content(self, other: Snapshot) -> bool:
        """Compare the entire JSON, including extensions."""
        return self.raw == other.raw or _equal_json(self._content, other._content)


def _equal_json(left: Any, right: Any) -> bool:
    pending = [(left, right)]
    while pending:
        left, right = pending.pop()
        if isinstance(left, bool) or isinstance(right, bool):
            if left is not right:
                return False
        elif isinstance(left, dict) and isinstance(right, dict):
            if left.keys() != right.keys():
                return False
            pending.extend((value, right[key]) for key, value in left.items())
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                return False
            pending.extend(zip(left, right))
        elif left != right:
            return False
    return True


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSnapshot("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise InvalidSnapshot("Invalid JSON number")


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidSnapshot("Expected string")
    return value


def _uuid(value: Any) -> str:
    value = _text(value)
    if str(UUID(value)) != value:
        raise InvalidSnapshot("Expected canonical UUID")
    return value


def _instant(value: Any) -> Instant:
    match = _INSTANT.fullmatch(_text(value))
    if match is None:
        raise InvalidSnapshot("Expected UTC timestamp")
    seconds, fraction = match.groups()
    return datetime.fromisoformat(seconds).replace(tzinfo=UTC), int((fraction or "").ljust(9, "0"))


def _zone(value: Any) -> tzinfo:
    value = _text(value)
    if value in {"Z", "UTC", "UT", "GMT"}:
        return UTC
    if match := _OFFSET.fullmatch(value):
        sign, hours, minutes, seconds = match.groups()
        hours, minutes, seconds = int(hours), int(minutes), int(seconds or 0)
        if hours > 18 or minutes > 59 or seconds > 59 or (hours == 18 and (minutes or seconds)):
            raise InvalidSnapshot("Invalid timezone offset")
        offset = timedelta(hours=hours, minutes=minutes, seconds=seconds)
        return timezone(offset if sign == "+" else -offset)
    return ZoneInfo(value)


def _local_end(start: Instant, zone: tzinfo, days: int) -> Instant:
    """Mirror ZonedDateTime.plusDays across gaps and overlaps."""
    seconds, nanos = start
    local_start = seconds.astimezone(zone)
    local_end = local_start.replace(tzinfo=None) + timedelta(days=days)
    earlier = local_end.replace(tzinfo=zone, fold=0).astimezone(UTC)
    later = local_end.replace(tzinfo=zone, fold=1).astimezone(UTC)
    if earlier < later and later.astimezone(zone).utcoffset() == local_start.utcoffset():
        return later, nanos
    return earlier, nanos


def _validate_occurrences(
    data: dict[str, Any], start: Instant, end: Instant
) -> None:
    catalog = data.get("alarms")
    if "alarms" in data:
        if not isinstance(catalog, dict):
            raise InvalidSnapshot("Expected alarm catalog object")
        for alarm_id, metadata in catalog.items():
            _uuid(alarm_id)
            if not isinstance(metadata, dict):
                raise InvalidSnapshot("Expected alarm metadata object")
            _text(metadata["name"])
            for key in ("category", "mode"):
                if key in metadata:
                    _text(metadata[key])

    occurrences = data["occurrences"]
    if not isinstance(occurrences, list):
        raise InvalidSnapshot("Expected occurrence array")
    seen = set()
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            raise InvalidSnapshot("Expected occurrence object")
        alarm_id = _uuid(occurrence["alarm_id"])
        nominal = _text(occurrence["nominal_date"])
        if not _DATE.fullmatch(nominal):
            raise InvalidSnapshot("Invalid nominal date")
        date.fromisoformat(nominal)
        identity = _text(occurrence["occurrence_id"])
        if identity != f"{alarm_id}/{nominal}" or identity in seen:
            raise InvalidSnapshot("Invalid or duplicate occurrence identity")
        seen.add(identity)
        if catalog is not None and alarm_id not in catalog:
            raise InvalidSnapshot("Missing catalog reference")
        sound = _instant(occurrence["sound_start"])
        if not start <= sound < end:
            raise InvalidSnapshot("Sound start outside calendar window")
        if "target_at" in occurrence and _instant(occurrence["target_at"]) < sound:
            raise InvalidSnapshot("Target precedes sound start")


def decode_snapshot(body: bytes) -> Snapshot:
    """Validate a full snapshot before any state can change."""
    if len(body) > MAX_BYTES:
        raise InvalidSnapshot("Snapshot too large")
    try:
        raw = body.decode("utf-8")
        data = json.loads(
            raw, object_pairs_hook=_object, parse_float=Decimal,
            parse_constant=_reject_constant,
        )
        if not isinstance(data, dict):
            raise InvalidSnapshot("Expected snapshot object")
        version = data["schema_version"]
        if type(version) is not int or version != 1 or data["event"] != "calendar_snapshot":
            raise InvalidSnapshot("Unsupported snapshot type")
        installation = _uuid(data["installation_id"])
        revision = data["revision"]
        if type(revision) is not int or not 1 <= revision <= 2**63 - 1:
            raise InvalidSnapshot("Invalid revision")
        start = _instant(data["window_start"])
        end = _instant(data["window_end_exclusive"])
        zone = _zone(data["timezone"])
        if not any(_local_end(start, zone, days) == end for days in (30, 60, 90)):
            raise InvalidSnapshot("Invalid calendar window")
        generated = _instant(data["generated_at"])
        expires = _instant(data["expires_at"])
        if not generated < expires <= end:
            raise InvalidSnapshot("Invalid expiration")
        _validate_occurrences(data, start, end)
    except (ValueError, KeyError, ZoneInfoNotFoundError, OverflowError, RecursionError,
            InvalidOperation):
        raise InvalidSnapshot("Invalid calendar snapshot") from None
    return Snapshot(installation, revision, expires, raw, data)
