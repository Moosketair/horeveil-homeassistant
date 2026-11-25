"""Receive Horéveil snapshots over a local Home Assistant webhook."""

import probatio

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.storage import Store

from .const import CALENDAR_RECEIVERS, DOMAIN, PAIRING_RUNTIME, RELAY_TOKEN, WEBHOOK_ID
from .receiver import SnapshotReceiver

type HoreveilConfigEntry = ConfigEntry[SnapshotReceiver]

PLATFORMS = (Platform.CALENDAR, Platform.SENSOR)
CONFIG_SCHEMA = probatio.Schema({probatio.Optional(DOMAIN): probatio.Any(None, dict)}, extra=probatio.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up optional pairing access separately from the receiver entries."""
    options = config.get(DOMAIN)
    if isinstance(options, dict) and {"local_pairing_test_client_sha256", "direct_pairing", "native_pairing"} & options.keys():
        from .pairing_ha import async_setup_local_pairing

        await async_setup_local_pairing(
            hass, options.get("local_pairing_test_client_sha256"), options.get("direct_pairing"),
            native_options=options.get("native_pairing"),
        )
    elif "frontend" in getattr(getattr(hass, "config", None), "components", ()):
        from .pairing_ha import async_setup_local_pairing

        await async_setup_local_pairing(hass, None, universal=True)
    return True


def _store(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    return Store(
        hass, 1, f"{DOMAIN}.{entry.entry_id}", private=True,
        atomic_writes=True, serialize_in_event_loop=False,
    )


async def async_setup_entry(hass: HomeAssistant, entry: HoreveilConfigEntry) -> bool:
    """Restore the receiver before accepting requests."""
    try:
        receiver = SnapshotReceiver(
            hass, _store(hass, entry), entry.data[RELAY_TOKEN], entry.entry_id
        )
        await receiver.async_restore()
        data = getattr(hass, "data", None)
        if receiver.direct_source is not None:
            runtime = data.get(PAIRING_RUNTIME) if data is not None else None
            if runtime is None or not runtime.universal:
                raise ConfigEntryError("Unable to verify the Horéveil calendar source")
            await runtime.registry.async_prepare_calendar_receiver(
                receiver.direct_source, lambda source: receiver.async_select_source(None, forget=source)
            )
        webhook.async_register(
            hass, DOMAIN, "Horéveil", entry.data[WEBHOOK_ID], receiver.async_handle,
            local_only=True, allowed_methods=("POST",),
        )
    except Exception:
        raise ConfigEntryError("Unable to initialize the Horéveil receiver") from None
    entry.runtime_data = receiver
    if data is not None:
        data.setdefault(CALENDAR_RECEIVERS, {})[entry.entry_id] = receiver
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        if data is not None:
            data[CALENDAR_RECEIVERS].pop(entry.entry_id, None)
        webhook.async_unregister(hass, entry.data[WEBHOOK_ID])
        await receiver.async_close()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HoreveilConfigEntry) -> bool:
    """Stop accepting requests on the entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    webhook.async_unregister(hass, entry.data[WEBHOOK_ID])
    await entry.runtime_data.async_close()
    if (data := getattr(hass, "data", None)) is not None:
        data.get(CALENDAR_RECEIVERS, {}).pop(entry.entry_id, None)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: HoreveilConfigEntry) -> None:
    """Remove private snapshot storage along with the receiver entry."""
    await _store(hass, entry).async_remove()
