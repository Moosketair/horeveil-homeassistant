"""Receive Horéveil snapshots over a local Home Assistant webhook."""

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.storage import Store

from .const import DOMAIN, RELAY_TOKEN, WEBHOOK_ID
from .receiver import SnapshotReceiver

type HoreveilConfigEntry = ConfigEntry[SnapshotReceiver]


def _store(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    return Store(
        hass, 1, f"{DOMAIN}.{entry.entry_id}", private=True,
        atomic_writes=True, serialize_in_event_loop=False,
    )


async def async_setup_entry(hass: HomeAssistant, entry: HoreveilConfigEntry) -> bool:
    """Restore the receiver before accepting requests."""
    try:
        receiver = SnapshotReceiver(hass, _store(hass, entry), entry.data[RELAY_TOKEN])
        await receiver.async_restore()
        webhook.async_register(
            hass, DOMAIN, "Horéveil", entry.data[WEBHOOK_ID], receiver.async_handle,
            local_only=True, allowed_methods=("POST",),
        )
    except Exception:
        raise ConfigEntryError("Unable to initialize the Horéveil receiver") from None
    entry.runtime_data = receiver
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HoreveilConfigEntry) -> bool:
    """Stop accepting requests on the entry."""
    webhook.async_unregister(hass, entry.data[WEBHOOK_ID])
    await entry.runtime_data.async_close()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: HoreveilConfigEntry) -> None:
    """Remove private snapshot storage along with the receiver entry."""
    await _store(hass, entry).async_remove()
