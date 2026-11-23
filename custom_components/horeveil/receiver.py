"""Serialize snapshot updates and serve the local webhook."""

import asyncio
from datetime import UTC, datetime
from http import HTTPStatus
from secrets import compare_digest

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DOMAIN, RELAY_HEADER
from .snapshot import MAX_BYTES, Snapshot, decode_snapshot


def _reply(status: HTTPStatus, result: str) -> web.Response:
    return web.json_response({"result": result}, status=status)


class SnapshotReceiver:
    """Coordinate a single phone and its durable revision watermark."""

    def __init__(self, hass: HomeAssistant, store: Store, token: str, entry_id: str) -> None:
        self.update_signal = f"{DOMAIN}_snapshot_updated_{entry_id}"
        self._hass = hass
        self._store = store
        self._token = token
        self._lock = asyncio.Lock()
        self._last: Snapshot | None = None
        self.received_at: datetime | None = None
        self._closed = False
        self._storage_uncertain = False

    @property
    def freshness(self) -> str:
        """Calculate freshness at read time, including after a restart."""
        if self._last is None:
            return "empty"
        return "expired" if self._last.expired_at(datetime.now(UTC)) else "fresh"

    @property
    def current(self) -> Snapshot | None:
        """Return a snapshot only while it is fresh."""
        return self._last if self.freshness == "fresh" else None

    async def async_restore(self) -> None:
        """Restore the payload and its source/revision watermark."""
        stored = await self._store.async_load()
        if stored is not None:
            self._last = await self._hass.async_add_executor_job(
                decode_snapshot, stored["snapshot"].encode("utf-8")
            )
            if isinstance(value := stored.get("received_at"), str):
                try:
                    received_at = datetime.fromisoformat(value)
                except ValueError:
                    pass
                else:
                    if received_at.tzinfo is not None:
                        self.received_at = received_at.astimezone(UTC)

    async def async_close(self) -> None:
        """Prevent new writes and wait for an existing commit to finish."""
        self._closed = True
        async with self._lock:
            pass

    async def _commit(self, snapshot: Snapshot) -> web.Response:
        async with self._lock:
            if self._closed or self._storage_uncertain or self._hass.is_stopping:
                return _reply(HTTPStatus.SERVICE_UNAVAILABLE, "receiver_unavailable")
            if last := self._last:
                if snapshot.installation_id != last.installation_id:
                    return _reply(HTTPStatus.CONFLICT, "different_installation")
                if snapshot.revision < last.revision:
                    return _reply(HTTPStatus.CONFLICT, "old_revision")
                if snapshot.revision == last.revision:
                    equal = await self._hass.async_add_executor_job(snapshot.same_content, last)
                    if equal:
                        return _reply(HTTPStatus.OK, "duplicate")
                    return _reply(HTTPStatus.CONFLICT, "revision_conflict")

            received_at = datetime.now(UTC)
            stored = {
                "snapshot": snapshot.raw,
                "received_at": received_at.isoformat(),
            }
            try:
                await self._store.async_save(stored)
                # Store can log an I/O failure instead of propagating it.
                persisted = await self._store.async_load()
            except Exception:
                self._storage_uncertain = True
                return _reply(HTTPStatus.SERVICE_UNAVAILABLE, "storage_unavailable")
            if self._hass.is_stopping:
                return _reply(HTTPStatus.SERVICE_UNAVAILABLE, "receiver_unavailable")
            if persisted != stored:
                expected = last.raw if last else None
                previous = persisted.get("snapshot") if isinstance(persisted, dict) else None
                self._storage_uncertain = previous != expected
                return _reply(HTTPStatus.SERVICE_UNAVAILABLE, "storage_unavailable")
            self._last = snapshot
            self.received_at = received_at
            async_dispatcher_send(self._hass, self.update_signal)
            return _reply(HTTPStatus.OK, "accepted")

    async def async_handle(self, _hass, _webhook_id, request: web.Request) -> web.Response:
        """Authenticate and validate before committing any state."""
        if request.method != "POST":
            return _reply(HTTPStatus.METHOD_NOT_ALLOWED, "post_required")
        tokens = request.headers.getall(RELAY_HEADER, ())
        if (
            len(tokens) != 1
            or not tokens[0].isascii()
            or not compare_digest(tokens[0], self._token)
        ):
            return _reply(HTTPStatus.FORBIDDEN, "unauthorized")
        media_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip()
        encoding = request.headers.get("Content-Encoding", "identity").lower()
        if media_type.lower() != "application/json" or encoding != "identity":
            return _reply(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "json_required")
        try:
            if int(request.headers.get("Content-Length", "0")) > MAX_BYTES:
                return _reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large")
            body = bytearray()
            stream = request.content
            async with asyncio.timeout(15):
                while chunk := await stream.read(65_536):
                    if len(body) + len(chunk) > MAX_BYTES:
                        return _reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large")
                    body.extend(chunk)
            snapshot = await self._hass.async_add_executor_job(decode_snapshot, bytes(body))
        except TimeoutError:
            return _reply(HTTPStatus.REQUEST_TIMEOUT, "request_timeout")
        except Exception:
            # Exception messages from HTTP/JSON libraries can include private data.
            return _reply(HTTPStatus.BAD_REQUEST, "invalid_snapshot")

        # A disconnected caller must not cancel a persistent write mid-commit.
        try:
            task = self._hass.async_create_task(self._commit(snapshot))
            return await asyncio.shield(task)
        except Exception:
            return _reply(HTTPStatus.SERVICE_UNAVAILABLE, "storage_unavailable")
