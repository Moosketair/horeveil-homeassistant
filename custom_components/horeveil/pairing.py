"""Internal invitation and association storage for direct pairing."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from hmac import compare_digest
from ipaddress import ip_address
import re
from secrets import token_bytes
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_TTL = timedelta(minutes=5)
_MAX_INVITATIONS = 8
_REGISTRY_KEY = f"{DOMAIN}.pairing_registry"
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE = re.compile(rf"[0-7][{_ALPHABET}]{{25}}", re.ASCII)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class PairingError(ValueError):
    """A rejected pairing operation, without private context."""


class AssociationState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class Consultation(StrEnum):
    CALENDAR = "calendar"
    ALARMS = "alarms"
    CATEGORIES_MODES = "categories_modes"
    ALARM_EVENTS = "alarm_events"


class Control(StrEnum):
    RESCHEDULE = "occurrence.reschedule"
    SKIP = "occurrence.skip"
    SET_MODE = "category.set_mode"
    SET_ENABLED = "alarm.set_enabled"


@dataclass(frozen=True, slots=True)
class Permissions:
    consultation: frozenset[Consultation] = frozenset()
    control: frozenset[Control] = frozenset()

    def as_storage(self) -> dict:
        return {"consultation": sorted(self.consultation), "control": sorted(self.control)}

    @classmethod
    def from_storage(cls, data: dict) -> "Permissions":
        return cls(
            frozenset(Consultation(scope) for scope in data["consultation"]),
            frozenset(Control(scope) for scope in data["control"]),
        )


@dataclass(frozen=True, slots=True)
class Association:
    pairing_id: UUID
    installation_id: UUID
    origin: str
    state: AssociationState
    ha_permissions: Permissions = Permissions()
    phone_permissions: Permissions = Permissions()
    credential_digest: bytes | None = field(default=None, repr=False)

    def as_storage(self) -> dict:
        return {
            "pairing_id": str(self.pairing_id),
            "installation_id": str(self.installation_id),
            "origin": self.origin,
            "state": self.state.value,
            "ha_permissions": self.ha_permissions.as_storage(),
            "phone_permissions": self.phone_permissions.as_storage(),
            "credential_digest": (
                self.credential_digest.hex() if self.credential_digest is not None else None
            ),
        }

    @classmethod
    def from_storage(cls, data: dict) -> "Association":
        state = AssociationState(data["state"])
        if state is AssociationState.ACTIVE:
            digest = bytes.fromhex(data["credential_digest"])
            if len(digest) != 32:
                raise PairingError("Invalid stored credential")
            ha_permissions = Permissions.from_storage(data["ha_permissions"])
            phone_permissions = Permissions.from_storage(data["phone_permissions"])
        else:
            digest = None
            ha_permissions = phone_permissions = Permissions()
        return cls(
            UUID(data["pairing_id"]), UUID(data["installation_id"]),
            validate_origin(data["origin"]), state, ha_permissions, phone_permissions, digest,
        )

    def revoked(self) -> "Association":
        return replace(
            self, state=AssociationState.REVOKED, credential_digest=None,
            ha_permissions=Permissions(), phone_permissions=Permissions(),
        )


@dataclass(frozen=True, slots=True)
class Invitation:
    invitation_id: UUID
    origin: str
    expires_at: datetime
    code: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _PendingInvitation:
    administrator_id: str
    origin: str
    deadline: float
    digest: bytes = field(repr=False)


def validate_origin(candidate: str) -> str:
    """Canonicalize an explicitly confirmed HTTPS origin without contacting it."""
    try:
        if not candidate.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in candidate):
            raise ValueError
        if "?" in candidate or "#" in candidate or "\\" in candidate:
            raise ValueError
        url = urlsplit(candidate)
        host = url.hostname
        port = url.port
        if (
            url.scheme != "https" or not host or url.username is not None
            or url.path not in ("", "/") or url.netloc.endswith(":") or port == 0
        ):
            raise ValueError
        if ":" in host or url.netloc.startswith("["):
            if "%" in host or (address := ip_address(host)).version != 6:
                raise ValueError
            closing = url.netloc.find("]")
            if url.netloc[closing + 1:] and not url.netloc[closing + 1:].startswith(":"):
                raise ValueError
            host = f"[{address.compressed}]"
        elif len(host) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in host.split(".")):
            raise ValueError
        return f"https://{host}" if port in (None, 443) else f"https://{host}:{port}"
    except ValueError:
        raise PairingError("Expected a valid HTTPS origin") from None


def _new_code() -> str:
    value = int.from_bytes(token_bytes(16), "big")
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))


class _BackupExcludedStore(Store):
    @property
    def path(self) -> str:
        return self.hass.config.path(self.key)


class PairingRegistry:
    """One registry per HA instance, created lazily by the future pairing flow."""

    def __init__(
        self, store: Store, guard: Store, *, clock: Callable[[], float] = monotonic,
    ) -> None:
        self._store = store
        self._guard = guard
        self._clock = clock
        self._lock = asyncio.Lock()
        self._server_id: UUID | None = None
        self._available = False
        self._associations: dict[UUID, Association] = {}
        self._invitations: dict[UUID, _PendingInvitation] = {}

    @classmethod
    async def async_open(cls, hass: HomeAssistant) -> "PairingRegistry":
        """Open one registry per HA instance, even with concurrent callers."""
        registry = hass.data.get(_REGISTRY_KEY)
        if registry is None:
            options = {"private": True, "atomic_writes": True}
            registry = cls(
                Store(hass, 1, f"{DOMAIN}.pairings", **options),
                _BackupExcludedStore(hass, 1, f".cache/{DOMAIN}.pairing_guard", **options),
            )
            hass.data[_REGISTRY_KEY] = registry
        await registry.async_load()
        return registry

    @property
    def server_id(self) -> UUID:
        if not self._available or self._server_id is None:
            raise PairingError("Pairing registry unavailable")
        return self._server_id

    async def async_load(self) -> None:
        """Load once per process, or recover after a failed write."""
        async with self._lock:
            if self._available:
                return
            self._invitations.clear()
            stored = await self._store.async_load()
            if stored is None:
                await self._commit(uuid4(), {})
                return
            try:
                server_id = UUID(stored["server_id"])
                generation = str(UUID(stored["generation"]))
                associations = {}
                installations = set()
                for raw in stored["associations"]:
                    association = Association.from_storage(raw)
                    if association.pairing_id in associations:
                        raise PairingError("Duplicate association")
                    if association.state is AssociationState.ACTIVE:
                        if association.installation_id in installations:
                            raise PairingError("Duplicate active installation")
                        installations.add(association.installation_id)
                    associations[association.pairing_id] = association
            except (AttributeError, KeyError, TypeError, ValueError):
                raise PairingError("Invalid pairing registry") from None
            if await self._guard.async_load() != {"generation": generation}:
                await self._commit(server_id, {
                    key: association.revoked() for key, association in associations.items()
                })
                return
            self._server_id = server_id
            self._associations = associations
            self._available = True

    async def _commit(self, server_id: UUID, associations: dict[UUID, Association]) -> None:
        """Persist under the registry lock and await completion even on cancellation."""
        async def write() -> None:
            generation = str(uuid4())
            guard = {"generation": generation}
            stored = {
                "server_id": str(server_id), "generation": generation,
                "associations": [association.as_storage() for association in associations.values()],
            }
            self._available = False
            # Advancing the guard first makes a partial transaction fail closed.
            await self._guard.async_save(guard)
            if await self._guard.async_load() != guard:
                raise PairingError("Unable to persist pairing guard")
            await self._store.async_save(stored)
            if await self._store.async_load() != stored:
                raise PairingError("Unable to persist pairing registry")
            self._server_id = server_id
            self._associations = associations
            self._available = True

        task = asyncio.create_task(write())
        cancellation = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = error
        task.result()
        if cancellation is not None:
            raise cancellation

    def _prune_expired(self, now: float) -> None:
        self._invitations = {
            key: pending for key, pending in self._invitations.items() if pending.deadline > now
        }

    async def async_invite(self, *, administrator_id: str, confirmed_origin: str) -> Invitation:
        """Issue one temporary code for the administrator and confirmed origin."""
        origin = validate_origin(confirmed_origin)
        async with self._lock:
            _ = self.server_id
            now = self._clock()
            self._prune_expired(now)
            if len(self._invitations) >= _MAX_INVITATIONS or any(
                pending.administrator_id == administrator_id
                for pending in self._invitations.values()
            ):
                raise PairingError("Invitation limit reached")
            invitation_id = uuid4()
            code = _new_code()
            self._invitations[invitation_id] = _PendingInvitation(
                administrator_id, origin, now + _TTL.total_seconds(), sha256(code.encode()).digest(),
            )
            return Invitation(invitation_id, origin, datetime.now(UTC) + _TTL, code)

    async def async_cancel(self, invitation_id: UUID, *, administrator_id: str) -> bool:
        """Cancel an outstanding invitation owned by the caller."""
        async with self._lock:
            self._prune_expired(self._clock())
            pending = self._invitations.get(invitation_id)
            if pending is None or pending.administrator_id != administrator_id:
                return False
            del self._invitations[invitation_id]
            return True

    async def async_activate_confirmed(
        self, invitation_id: UUID, code: str, *, installation_id: UUID,
        credential_digest: bytes, administrator_id: str,
    ) -> Association:
        """Consume an invitation after the caller has confirmed both sides."""
        if (
            not isinstance(code, str) or not _CODE.fullmatch(code)
            or not isinstance(installation_id, UUID)
            or not isinstance(credential_digest, bytes) or len(credential_digest) != 32
        ):
            raise PairingError("Invalid pairing credential")
        async with self._lock:
            server_id = self.server_id
            self._prune_expired(self._clock())
            pending = self._invitations.get(invitation_id)
            if (
                pending is None or pending.administrator_id != administrator_id
                or not compare_digest(pending.digest, sha256(code.encode()).digest())
            ):
                raise PairingError("Invalid invitation")
            if any(
                association.installation_id == installation_id
                and association.state is AssociationState.ACTIVE
                for association in self._associations.values()
            ):
                raise PairingError("Installation already paired")
            pairing_id = uuid4()
            association = Association(
                pairing_id, installation_id, pending.origin, AssociationState.ACTIVE,
                credential_digest=credential_digest,
            )
            del self._invitations[invitation_id]
            await self._commit(server_id, {**self._associations, pairing_id: association})
            return association

    async def async_authenticate(self, pairing_id: UUID, secret: bytes) -> Association | None:
        """Check proof of possession for exactly one active association."""
        async with self._lock:
            association = self._associations.get(pairing_id) if self._available else None
            if (
                association is not None and association.state is AssociationState.ACTIVE
                and association.credential_digest is not None and len(secret) == 32
                and compare_digest(association.credential_digest, sha256(secret).digest())
            ):
                return association
            return None

    async def async_revoke(self, pairing_id: UUID) -> bool:
        """Persist an irreversible tombstone for one association."""
        async with self._lock:
            server_id = self.server_id
            association = self._associations.get(pairing_id)
            if association is None or association.state is AssociationState.REVOKED:
                return False
            await self._commit(server_id, {**self._associations, pairing_id: association.revoked()})
            return True
