"""Internal invitation and association storage for direct pairing."""

import asyncio
from base64 import urlsafe_b64encode
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from hmac import compare_digest, digest as hmac_digest
from ipaddress import ip_address
import re
from secrets import token_bytes
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.auth.models import User

_TTL = timedelta(minutes=5)
_MAX_INVITATIONS = 8
_REGISTRY_KEY = f"{DOMAIN}.pairing_registry"
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE = re.compile(rf"[0-7][{_ALPHABET}]{{25}}", re.ASCII)
_COMPARISON_CODE = re.compile(rf"[{_ALPHABET}]{{8}}", re.ASCII)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class PairingError(ValueError):
    """A rejected pairing operation, without private context."""


class InvalidPairingCredential(PairingError):
    """An absent, expired or incorrect credential."""


class PairingConflict(PairingError):
    """The invitation or installation already has an incompatible claim."""


class PairingApprovalRequired(PairingError):
    """The authenticated claim is awaiting administrator approval."""


class PairingStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    ACTIVE = "active"


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
class PairingClaim:
    claim_id: UUID
    installation_id: UUID
    phone_label: str = field(repr=False)
    credential_digest: bytes = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PairingReservation:
    pairing_id: UUID
    server_id: UUID
    challenge: str
    claim: PairingClaim
    status: PairingStatus = PairingStatus.AWAITING_APPROVAL

    def comparison_code(self) -> str:
        message = (
            f"horeveil-pair-v1|{self.server_id}|{self.pairing_id}|"
            f"{self.claim.claim_id}|{self.challenge}"
        )
        output = hmac_digest(self.claim.credential_digest, message.encode("ascii"), "sha256")
        return _crockford(output[:5], 8)


@dataclass(frozen=True, slots=True)
class PairingReview:
    pairing_id: UUID
    origin: str
    status: PairingStatus
    phone_label: str = field(repr=False)
    comparison_code: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _PendingInvitation:
    administrator_id: str
    origin: str
    deadline: float
    digest: bytes = field(repr=False)
    reservation: PairingReservation | None = None


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


def _crockford(data: bytes, width: int) -> str:
    value = int.from_bytes(data, "big")
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range((width - 1) * 5, -1, -5))


def _invitation_digest(code: str) -> bytes:
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise InvalidPairingCredential("Invalid invitation")
    return sha256(code.encode("ascii")).digest()


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
            code = _crockford(token_bytes(16), 26)
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

    async def async_discard_invitations(self, *, origin: str | None = None) -> None:
        """Clear pending invitations for one origin, or all on shutdown."""
        async with self._lock:
            self._invitations = {
                key: value for key, value in self._invitations.items()
                if origin is not None and value.origin != origin
            }

    async def async_claim(
        self, code: str, claim: PairingClaim, *, origin: str | None = None,
    ) -> PairingReservation:
        """Reserve an invitation or return the same reservation on an exact retry."""
        digest = _invitation_digest(code)
        if len(claim.credential_digest) != 32:
            raise InvalidPairingCredential("Invalid credential digest")
        async with self._lock:
            server_id = self.server_id
            self._prune_expired(self._clock())
            for invitation_id, pending in self._invitations.items():
                if (origin is not None and pending.origin != origin) or not compare_digest(pending.digest, digest):
                    continue
                reservation = pending.reservation
                if reservation is not None:
                    if reservation.claim == claim and compare_digest(
                        reservation.claim.credential_digest, claim.credential_digest,
                    ):
                        return reservation
                    raise PairingConflict("Invitation already reserved")
                if self._installation_active(claim.installation_id) or self._installation_reserved(
                    claim.installation_id,
                ):
                    raise PairingConflict("Installation already paired or reserved")
                reservation = PairingReservation(
                    uuid4(), server_id, urlsafe_b64encode(token_bytes(32)).decode().rstrip("="), claim,
                )
                self._invitations[invitation_id] = replace(pending, reservation=reservation)
                return reservation
            raise InvalidPairingCredential("Invalid invitation")

    def _find_reservation(
        self, pairing_id: UUID,
    ) -> tuple[UUID, _PendingInvitation, PairingReservation]:
        _ = self.server_id
        self._prune_expired(self._clock())
        for invitation_id, pending in self._invitations.items():
            if (reservation := pending.reservation) is not None and reservation.pairing_id == pairing_id:
                return invitation_id, pending, reservation
        raise InvalidPairingCredential("Pairing claim unavailable")

    @staticmethod
    def _administrator_id(user: "User | None") -> str:
        if user is None or not user.is_active or not user.is_admin:
            raise PermissionError("Administrator authentication required")
        return user.id

    async def async_review_claim(
        self, user: "User | None", invitation_id: UUID,
    ) -> PairingReview | None:
        """Provide the comparison material to the authenticated invitation owner."""
        async with self._lock:
            administrator_id = self._administrator_id(user)
            _ = self.server_id
            self._prune_expired(self._clock())
            pending = self._invitations.get(invitation_id)
            if pending is None or pending.administrator_id != administrator_id:
                raise InvalidPairingCredential("Invitation unavailable")
            reservation = pending.reservation
            if reservation is None:
                return None
            return PairingReview(
                reservation.pairing_id, pending.origin, reservation.status,
                reservation.claim.phone_label, reservation.comparison_code(),
            )

    async def async_approve_claim(
        self, user: "User | None", pairing_id: UUID, comparison_code: str,
    ) -> None:
        """Record the explicit decision of the authenticated administrator."""
        async with self._lock:
            administrator_id = self._administrator_id(user)
            invitation_id, pending, reservation = self._find_reservation(pairing_id)
            if pending.administrator_id != administrator_id:
                raise PermissionError("Invitation owner required")
            if not _COMPARISON_CODE.fullmatch(comparison_code) or not compare_digest(
                reservation.comparison_code(), comparison_code,
            ):
                raise InvalidPairingCredential("Comparison code mismatch")
            self._invitations[invitation_id] = replace(
                pending, reservation=replace(reservation, status=PairingStatus.APPROVED),
            )

    async def async_check_pairing(
        self, pairing_id: UUID, secret: bytes, *, confirm: bool = False, origin: str | None = None,
    ) -> PairingStatus:
        """Authenticate a status request or explicitly finalize an approved claim."""
        if len(secret) != 32:
            raise InvalidPairingCredential("Invalid pairing credential")
        digest = sha256(secret).digest()
        async with self._lock:
            _ = self.server_id
            active = self._active_with_digest(pairing_id, digest)
            if active is not None and (origin is None or active.origin == origin):
                return PairingStatus.ACTIVE
            invitation_id, pending, reservation = self._find_reservation(pairing_id)
            if (origin is not None and pending.origin != origin) or not compare_digest(
                reservation.claim.credential_digest, digest,
            ):
                raise InvalidPairingCredential("Invalid pairing credential")
            if not confirm:
                return reservation.status
            if reservation.status is not PairingStatus.APPROVED:
                raise PairingApprovalRequired("Administrator approval required")
            await self._activate(
                invitation_id, pending, pairing_id, reservation.claim.installation_id, digest,
            )
            return PairingStatus.ACTIVE

    async def async_activate_confirmed(
        self, invitation_id: UUID, code: str, *, installation_id: UUID,
        credential_digest: bytes, administrator_id: str,
    ) -> Association:
        """Consume an invitation after the caller has confirmed both sides."""
        digest = _invitation_digest(code)
        if (
            not isinstance(installation_id, UUID) or not isinstance(credential_digest, bytes)
            or len(credential_digest) != 32
        ):
            raise PairingError("Invalid pairing credential")
        async with self._lock:
            _ = self.server_id
            self._prune_expired(self._clock())
            pending = self._invitations.get(invitation_id)
            if (
                pending is None or pending.administrator_id != administrator_id
                or pending.reservation is not None or not compare_digest(pending.digest, digest)
            ):
                raise PairingError("Invalid invitation")
            if self._installation_reserved(installation_id):
                raise PairingConflict("Installation already reserved")
            return await self._activate(
                invitation_id, pending, uuid4(), installation_id, credential_digest,
            )

    def _installation_active(self, installation_id: UUID) -> bool:
        return any(
            association.installation_id == installation_id and association.state is AssociationState.ACTIVE
            for association in self._associations.values()
        )

    def _installation_reserved(self, installation_id: UUID) -> bool:
        return any(
            pending.reservation is not None and pending.reservation.claim.installation_id == installation_id
            for pending in self._invitations.values()
        )

    async def _activate(
        self, invitation_id: UUID, pending: _PendingInvitation, pairing_id: UUID,
        installation_id: UUID, credential_digest: bytes,
    ) -> Association:
        if self._installation_active(installation_id):
            raise PairingConflict("Installation already paired")
        server_id = self.server_id
        association = Association(
            pairing_id, installation_id, pending.origin, AssociationState.ACTIVE,
            credential_digest=credential_digest,
        )
        del self._invitations[invitation_id]
        await self._commit(server_id, {**self._associations, pairing_id: association})
        return association

    def _active_with_digest(self, pairing_id: UUID, digest: bytes) -> Association | None:
        association = self._associations.get(pairing_id) if self._available else None
        if (
            association is not None and association.state is AssociationState.ACTIVE
            and association.credential_digest is not None
            and compare_digest(association.credential_digest, digest)
        ):
            return association
        return None

    async def async_authenticate(self, pairing_id: UUID, secret: bytes) -> Association | None:
        """Check proof of possession for exactly one active association."""
        async with self._lock:
            return self._active_with_digest(pairing_id, sha256(secret).digest()) if len(secret) == 32 else None

    async def async_authorize_consultation(
        self, pairing_id: UUID, secret: bytes, origin: str, permission: Consultation,
    ) -> bool:
        """Check the two grants for a future read-only capability."""
        async with self._lock:
            association = self._active_with_digest(pairing_id, sha256(secret).digest()) if len(secret) == 32 else None
            return (
                association is not None and association.origin == origin
                and permission in association.ha_permissions.consultation
                and permission in association.phone_permissions.consultation
            )

    async def async_revoke(self, pairing_id: UUID) -> bool:
        """Persist an irreversible tombstone for one association."""
        async with self._lock:
            server_id = self.server_id
            association = self._associations.get(pairing_id)
            if association is None or association.state is AssociationState.REVOKED:
                return False
            await self._commit(server_id, {**self._associations, pairing_id: association.revoked()})
            return True

    async def async_list_active(self) -> tuple[UUID, ...]:
        """Return active association identifiers without credentials or labels."""
        async with self._lock:
            _ = self.server_id
            return tuple(
                pairing_id for pairing_id, association in self._associations.items()
                if association.state is AssociationState.ACTIVE
            )
