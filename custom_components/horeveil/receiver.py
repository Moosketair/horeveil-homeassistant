"""Serialize snapshot updates and serve the local webhook."""

import asyncio
from datetime import UTC, datetime
from http import HTTPStatus
from secrets import compare_digest
from uuid import UUID

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DOMAIN, RELAY_HEADER
from .snapshot import MAX_BYTES, Snapshot, decode_snapshot


def _reply(status: HTTPStatus, result: str) -> web.Response:
    return web.json_response({"result": result}, status=status, headers={"Cache-Control": "no-store"})


class SnapshotRequestError(ValueError):
    def __init__(self, status: HTTPStatus, result: str) -> None:
        self.status = status
        self.result = result


async def async_read_snapshot_body(request: web.Request) -> bytes:
    media_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip()
    encoding = request.headers.get("Content-Encoding", "identity").lower()
    if media_type.lower() != "application/json" or encoding != "identity":
        raise SnapshotRequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "json_required")
    if int(request.headers.get("Content-Length", "0")) > MAX_BYTES:
        raise SnapshotRequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large")
    body = bytearray()
    async with asyncio.timeout(15):
        while chunk := await request.content.read(65_536):
            if len(body) + len(chunk) > MAX_BYTES:
                raise SnapshotRequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large")
            body.extend(chunk)
    return bytes(body)


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
        self.direct_source: UUID | None = None
        self._direct_watermarks: dict[str, int] = {}

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
            if source := stored.get("direct_source"):
                self.direct_source = UUID(source)
            self._direct_watermarks = stored.get("direct_watermarks", {})
            if stored.get("snapshot") is not None:
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

    async def async_select_source(self, pairing_id: UUID | None, *, forget: UUID | None = None) -> bool:
        """Persist ownership and remove the previous projection in the same write."""
        async with self._lock:
            if (forget is not None and forget != self.direct_source
                    and str(forget) not in self._direct_watermarks):
                return True
            if self._closed or self._storage_uncertain or self._hass.is_stopping:
                if forget is not None and forget == self.direct_source:
                    self._hide_snapshot()
                return False
            watermarks = self._direct_watermarks.copy()
            if forget is not None:
                watermarks.pop(str(forget), None)
                pairing_id = self.direct_source
            changed = pairing_id != self.direct_source or (
                forget == self.direct_source and forget is not None and self._last is not None
            )
            if not changed and watermarks == self._direct_watermarks:
                return True
            stored = {
                "snapshot": self._last.raw if self._last is not None and not changed else None,
                "received_at": self.received_at.isoformat() if self.received_at and not changed else None,
                "direct_source": str(pairing_id) if pairing_id else None,
                "direct_watermarks": watermarks,
            }
            try:
                await self._store.async_save(stored)
                if await self._store.async_load() != stored:
                    raise OSError
            except Exception:
                self._storage_uncertain = True
                if forget is not None and forget == self.direct_source:
                    self._hide_snapshot()
                return False
            self.direct_source = pairing_id
            self._direct_watermarks = watermarks
            if changed:
                self._hide_snapshot()
            return True

    def _hide_snapshot(self) -> None:
        self._last = self.received_at = None
        async_dispatcher_send(self._hass, self.update_signal)

    async def _commit(self, snapshot: Snapshot, pairing_id: UUID | None = None) -> web.Response:
        async with self._lock:
            if self._closed or self._storage_uncertain or self._hass.is_stopping:
                return _reply(HTTPStatus.SERVICE_UNAVAILABLE, "receiver_unavailable")
            if self.direct_source != pairing_id:
                return _reply(HTTPStatus.FORBIDDEN, "source_not_authorized")
            last = self._last
            if last:
                if snapshot.installation_id != last.installation_id:
                    return _reply(HTTPStatus.CONFLICT, "different_installation")
                if snapshot.revision < last.revision:
                    return _reply(HTTPStatus.CONFLICT, "old_revision")
                if snapshot.revision == last.revision:
                    equal = await self._hass.async_add_executor_job(snapshot.same_content, last)
                    if equal:
                        return _reply(HTTPStatus.OK, "duplicate")
                    return _reply(HTTPStatus.CONFLICT, "revision_conflict")
            watermark = self._direct_watermarks.get(str(pairing_id), 0)
            if pairing_id is not None and snapshot.revision <= watermark:
                return _reply(HTTPStatus.CONFLICT, "old_revision")

            received_at = datetime.now(UTC)
            stored = {
                "snapshot": snapshot.raw,
                "received_at": received_at.isoformat(),
            }
            watermarks = self._direct_watermarks
            if pairing_id is not None:
                watermarks = {**watermarks, str(pairing_id): snapshot.revision}
                stored["direct_source"] = str(pairing_id)
            if watermarks:
                stored["direct_watermarks"] = watermarks
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
            self._direct_watermarks = watermarks
            self.received_at = received_at
            async_dispatcher_send(self._hass, self.update_signal)
            return _reply(HTTPStatus.OK, "accepted")

    async def async_handle_direct(self, body: bytes, pairing_id: UUID, installation_id: UUID) -> web.Response:
        """Accept a verified association's snapshot while its registry lock is held."""
        if self.direct_source != pairing_id:
            return _reply(HTTPStatus.FORBIDDEN, "source_not_authorized")
        try:
            snapshot = await self._hass.async_add_executor_job(decode_snapshot, body)
        except Exception:
            return _reply(HTTPStatus.BAD_REQUEST, "invalid_snapshot")
        if snapshot.installation_id != str(installation_id):
            return _reply(HTTPStatus.FORBIDDEN, "different_installation")
        if snapshot.expired_at(datetime.now(UTC)):
            return _reply(HTTPStatus.CONFLICT, "expired_snapshot")
        return await self._commit(snapshot, pairing_id)

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
        try:
            body = await async_read_snapshot_body(request)
            snapshot = await self._hass.async_add_executor_job(decode_snapshot, body)
        except SnapshotRequestError as error:
            return _reply(error.status, error.result)
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
