"""Optional display information, independent of pairing and calendar state."""

from collections.abc import Iterable
import logging
from uuid import UUID

from .const import DOMAIN
from .storage import HoreveilStore, StoreReadError, async_load_store

MAX_REVISION = 2**63 - 1
_LIMITS = {"manufacturer": 64, "model": 128, "app_version": 48, "android_version": 32}
_LOGGER = logging.getLogger(__name__)


def validate_phone_metadata(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    fields = {}
    for key, limit in _LIMITS.items():
        value = raw.get(key)
        if not isinstance(value, str) or len(value) > limit or not value.isprintable():
            continue
        if len(value.encode("utf-8")) <= limit:
            value = value.strip()
            if value and value.casefold() != "unknown":
                fields[key] = value
    return fields


class PhoneMetadataStore:
    """Use under the registry lock for writes, and expose only active phones."""

    def __init__(self, hass) -> None:
        self._hass = hass
        self._store = HoreveilStore(hass, 1, f"{DOMAIN}.phone_metadata", private=True, atomic_writes=True)

    async def _load(self) -> dict:
        return await async_load_store(self._store) or {}

    async def _save(self, data: dict) -> None:
        if self._hass.is_stopping:
            raise StoreReadError("unavailable")
        await self._store.async_save(data)
        if await self._load() != data:
            raise StoreReadError("indeterminate")

    async def async_update(self, pairing_id: UUID, raw: object, revision: int, *, via_status: bool = False) -> str:
        fields = validate_phone_metadata(raw)
        if not fields:
            return "ignored"
        key = str(pairing_id)
        try:
            records = await self._load()
            previous = records.get(key)
            if previous is not None:
                old_revision = previous["calendar_revision"]
                if type(old_revision) is not int or not 0 <= old_revision <= MAX_REVISION:
                    raise StoreReadError("invalid")
                if revision < old_revision or (revision == old_revision and not via_status):
                    return "ignored"
                fields = validate_phone_metadata(previous.get("fields")) | fields
            record = {"calendar_revision": revision, "fields": fields}
            if record != previous:
                await self._save(records | {key: record})
        except Exception:
            # An optional display write must not change the accepted calendar response.
            _LOGGER.warning("Horéveil could not update stored phone information")
            return "unavailable"
        return "stored"

    async def async_read(self, active: Iterable[UUID]) -> dict[str, dict[str, str]]:
        try:
            records = await self._load()
        except Exception:
            _LOGGER.warning("Horéveil could not read stored phone information")
            return {}
        result = {}
        for phone in map(str, active):
            record = records.get(phone)
            if isinstance(record, dict) and (fields := validate_phone_metadata(record.get("fields"))):
                result[phone] = fields
        return result

    async def async_remove(self, pairing_ids: Iterable[UUID]) -> None:
        keys = {str(phone) for phone in pairing_ids}
        if not keys:
            return
        try:
            records = await self._load()
            if keys.intersection(records):
                await self._save({key: record for key, record in records.items() if key not in keys})
        except Exception:
            _LOGGER.warning("Horéveil could not clean up stored phone information")
