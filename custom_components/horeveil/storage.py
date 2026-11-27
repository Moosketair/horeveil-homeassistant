"""Distinguish an absent Horéveil Store from an ambiguous Home Assistant read."""

import asyncio
import os
from pathlib import Path

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store


class StoreReadError(OSError):
    """A Store cannot be trusted for an authorization or recovery decision."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Horéveil storage {reason}")


class HoreveilStore(Store):
    """Horéveil has no migrations for its authorization and calendar formats."""

    async def _async_migrate_func(self, old_major_version, old_minor_version, old_data):
        # Core otherwise rewrites unfamiliar minor versions of the same major.
        raise StoreReadError("invalid")


def _verify_absence(path: Path) -> None:
    # Core 2026.10+ preserves an unreadable JSON file beside the original path.
    # Keep that evidence across repeated reads, entry reloads and Core restarts.
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise StoreReadError("indeterminate")
    try:
        with os.scandir(path.parent) as entries:
            names = {entry.name for entry in entries}
    except FileNotFoundError:
        return
    if any(name.startswith(path.name + ".corrupt.") for name in names):
        raise StoreReadError("quarantined")
    if path.name in names:
        raise StoreReadError("indeterminate")


async def async_load_store(store: Store) -> dict | None:
    """Return a mapping or a verified absence, without repairing or deleting data."""
    try:
        stored = await store.async_load()
        if stored is None:
            await asyncio.to_thread(_verify_absence, Path(store.path))
        elif not isinstance(stored, dict):
            raise StoreReadError("invalid")
    except StoreReadError:
        raise
    except (HomeAssistantError, OSError, NotImplementedError):
        raise StoreReadError("unavailable") from None
    except (AttributeError, KeyError, TypeError, ValueError):
        raise StoreReadError("invalid") from None
    return stored
