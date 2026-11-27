"""Internal invitation and association storage for direct pairing."""

import asyncio
from base64 import urlsafe_b64encode
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from hmac import compare_digest, digest as hmac_digest
from ipaddress import ip_address
import re
from secrets import token_bytes
from time import monotonic
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .storage import HoreveilStore, async_load_store

if TYPE_CHECKING:
    from homeassistant.auth.models import User

_TTL = timedelta(minutes=5)
_MAX_INVITATIONS = 8
_REGISTRY_KEY = f"{DOMAIN}.pairing_registry"
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE = re.compile(rf"[0-7][{_ALPHABET}]{{25}}", re.ASCII)
_COMPARISON_CODE = re.compile(rf"[{_ALPHABET}]{{8}}", re.ASCII)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
type SignedOperation = Literal["enroll", "status", "activate", "cancel"]
type Enrollment = tuple[bytes, UUID, UUID, str]
type VerifySignature = Callable[[bytes, str, UUID], tuple[UUID, int, int]]


class PairingError(ValueError):
    """A rejected pairing operation, without private context."""


class InvalidPairingCredential(PairingError):
    """An absent, expired or incorrect credential."""


class PairingConflict(PairingError):
    """The invitation or installation already has an incompatible claim."""


class PairingApprovalRequired(PairingError):
    """The authenticated claim is awaiting administrator approval."""


class PairingRateLimited(PairingError):
    """The bounded replay window cannot accept another nonce."""


class PairingClockSkew(PairingError):
    """An authenticated signature lies outside the permitted time window."""


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
    public_key: bytes | None = field(default=None, repr=False)

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
            "public_key": self.public_key.hex() if self.public_key is not None else None,
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
            key = bytes.fromhex(data["public_key"]) if data.get("public_key") is not None else None
            if key is not None and len(key) != 91:
                raise PairingError("Invalid stored public key")
        else:
            digest = None
            key = None
            ha_permissions = phone_permissions = Permissions()
        return cls(
            UUID(data["pairing_id"]), UUID(data["installation_id"]),
            validate_origin(data["origin"]), state, ha_permissions, phone_permissions, digest, key,
        )

    def revoked(self) -> "Association":
        return replace(
            self, state=AssociationState.REVOKED, credential_digest=None, public_key=None,
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
    public_key: bytes | None = field(default=None, repr=False)

    def comparison_code(self, origin: str | None = None) -> str:
        message = (
            f"horeveil-pair-v1|{self.server_id}|{self.pairing_id}|"
            f"{self.claim.claim_id}|{self.challenge}"
        )
        if self.public_key is not None:
            fingerprint = urlsafe_b64encode(sha256(self.public_key).digest()).decode("ascii").rstrip("=")
            message = "\n".join(("horeveil-direct-compare-v1", str(self.server_id), str(self.pairing_id),
                                 str(self.claim.installation_id), str(self.claim.claim_id), self.challenge,
                                 origin or "", fingerprint))
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
    direct: bool = False


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


class _BackupExcludedStore(HoreveilStore):
    @property
    def path(self) -> str:
        return self.hass.config.path(self.key)


async def _persist(write) -> None:
    """Finish a storage operation before releasing the caller's lock."""
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


class PairingRegistry:
    """One registry per HA instance, created lazily by the future pairing flow."""

    def __init__(
        self, store: Store, guard: Store, *, clock: Callable[[], float] = monotonic, replay: Store | None = None,
    ) -> None:
        self._store = store
        self._guard = guard
        self._clock = clock
        self._lock = asyncio.Lock()
        self._server_id: UUID | None = None
        self._available = False
        self._associations: dict[UUID, Association] = {}
        self._invitations: dict[UUID, _PendingInvitation] = {}
        self._replay = replay
        self._replay_ready = False

    @classmethod
    async def async_open(cls, hass: HomeAssistant) -> "PairingRegistry":
        """Open one registry per HA instance, even with concurrent callers."""
        registry = hass.data.get(_REGISTRY_KEY)
        if registry is None:
            options = {"private": True, "atomic_writes": True}
            registry = cls(
                HoreveilStore(hass, 1, f"{DOMAIN}.pairings", **options),
                _BackupExcludedStore(hass, 1, f".cache/{DOMAIN}.pairing_guard", **options),
                replay=_BackupExcludedStore(hass, 1, f".cache/{DOMAIN}.pairing_replay", **options),
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
            stored = await async_load_store(self._store)
            guard = await async_load_store(self._guard)
            if guard is not None:
                try:
                    if set(guard) != {"generation"} or str(UUID(guard["generation"])) != guard["generation"]:
                        raise ValueError
                except (AttributeError, TypeError, ValueError):
                    raise PairingError("Invalid pairing guard") from None
            if stored is None:
                await self._commit(uuid4(), {})
                return
            try:
                server_id = UUID(stored["server_id"])
                generation = str(UUID(stored["generation"]))
                associations = {}
                installations = set()
                enrolled_keys = set()
                for raw in stored["associations"]:
                    association = Association.from_storage(raw)
                    if association.pairing_id in associations:
                        raise PairingError("Duplicate association")
                    if association.state is AssociationState.ACTIVE:
                        if association.installation_id in installations:
                            raise PairingError("Duplicate active installation")
                        installations.add(association.installation_id)
                        if association.public_key is not None:
                            if association.public_key in enrolled_keys:
                                raise PairingError("Duplicate active key")
                            enrolled_keys.add(association.public_key)
                    associations[association.pairing_id] = association
            except (AttributeError, KeyError, TypeError, ValueError):
                raise PairingError("Invalid pairing registry") from None
            if guard != {"generation": generation}:
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

        await _persist(write)

    def _prune_expired(self, now: float) -> None:
        self._invitations = {
            key: pending for key, pending in self._invitations.items() if pending.deadline > now
        }

    async def async_invite(self, *, administrator_id: str, confirmed_origin: str, direct: bool = False) -> Invitation:
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
                administrator_id, origin, now + _TTL.total_seconds(), sha256(code.encode()).digest(), direct=direct,
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
        self, code: str, claim: PairingClaim, *, origin: str | None = None, direct: bool = False,
    ) -> PairingReservation:
        """Reserve an invitation or return the same reservation on an exact retry."""
        digest = _invitation_digest(code)
        if len(claim.credential_digest) != 32:
            raise InvalidPairingCredential("Invalid credential digest")
        async with self._lock:
            server_id = self.server_id
            self._prune_expired(self._clock())
            for invitation_id, pending in self._invitations.items():
                if (pending.direct != direct or (origin is not None and pending.origin != origin)
                        or not compare_digest(pending.digest, digest)):
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
            if reservation is None or (pending.direct and reservation.public_key is None):
                return None
            return PairingReview(
                reservation.pairing_id, pending.origin, reservation.status,
                reservation.claim.phone_label, reservation.comparison_code(pending.origin),
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
            if pending.direct and reservation.public_key is None:
                raise InvalidPairingCredential("Key enrollment required")
            if not _COMPARISON_CODE.fullmatch(comparison_code) or not compare_digest(
                reservation.comparison_code(pending.origin), comparison_code,
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
            if pending.direct or (origin is not None and pending.origin != origin) or not compare_digest(
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

    async def _consume_nonce(self, pairing_id: UUID, nonce: UUID, deadline: int, now: int) -> None:
        """Reserve before mutation while holding the registry lock."""
        store = self._replay
        if store is None:
            raise PairingError("Replay protection unavailable")
        saved = await async_load_store(store)
        if saved is None:
            if self._replay_ready or any(assoc.public_key is not None for assoc in self._associations.values()):
                raise PairingError("Replay state unavailable")
            saved = {"server_id": str(self.server_id), "time": now, "nonces": {}}
        try:
            floor = saved["time"]
            if saved["server_id"] != str(self.server_id) or type(floor) is not int or now < floor:
                raise PairingError("Replay clock unavailable")
            items = saved["nonces"]
            if not isinstance(items, dict) or len(items) > 4096 or any(type(value) is not int for value in items.values()):
                raise PairingError("Invalid replay state")
        except (KeyError, TypeError):
            raise PairingError("Invalid replay state") from None
        prefix = f"{pairing_id}/"
        identifier = prefix + str(nonce)
        live = {key: value for key, value in items.items() if value >= now}
        if identifier in live:
            raise InvalidPairingCredential("Signature already used")
        if len(live) >= 4096 or sum(key.startswith(prefix) for key in live) >= 64:
            raise PairingRateLimited("Signature limit reached")
        live[identifier] = deadline
        next_value = {"server_id": str(self.server_id), "time": now, "nonces": live}
        # A failed or uncertain write must never be followed by activation.
        async def write():
            self._replay_ready = True
            await store.async_save(next_value)
            if await store.async_load() != next_value:
                raise PairingError("Unable to persist replay state")

        await _persist(write)

    async def async_direct_request(
        self, pairing_id: UUID, secret: bytes, operation: SignedOperation, verify: VerifySignature,
        *, enrollment: Enrollment | None = None,
    ) -> PairingStatus:
        """Bind enrollment, signed confirmation and revocation under one lock."""
        if len(secret) != 32 or operation not in {"enroll", "status", "activate", "cancel"}:
            raise InvalidPairingCredential("Invalid pairing credential")
        digest = sha256(secret).digest()
        async with self._lock:
            server_id = self.server_id
            active = self._associations.get(pairing_id)
            if active is not None:
                if (active.public_key is None or active.credential_digest is None
                        or not compare_digest(active.credential_digest, digest) or operation not in {"status", "activate"}):
                    raise InvalidPairingCredential("Pairing unavailable")
                await self._consume_nonce(pairing_id, *verify(active.public_key, active.origin, server_id))
                return PairingStatus.ACTIVE
            invitation_id, pending, reservation = self._find_reservation(pairing_id)
            claim = reservation.claim
            if not pending.direct or not compare_digest(claim.credential_digest, digest):
                raise InvalidPairingCredential("Pairing unavailable")
            key = reservation.public_key
            if operation == "enroll":
                if enrollment is None:
                    raise InvalidPairingCredential("Enrollment required")
                proposed, claim_id, installation_id, challenge = enrollment
                if (claim_id != claim.claim_id or installation_id != claim.installation_id
                        or challenge != reservation.challenge or (key is not None and key != proposed)):
                    raise InvalidPairingCredential("Invalid enrollment")
                key = proposed
            if key is None:
                raise InvalidPairingCredential("Enrollment required")
            await self._consume_nonce(pairing_id, *verify(key, pending.origin, server_id))
            if operation == "enroll":
                self._invitations[invitation_id] = replace(pending, reservation=replace(reservation, public_key=key))
            elif operation == "activate":
                if reservation.status is not PairingStatus.APPROVED:
                    raise PairingApprovalRequired("Administrator approval required")
                await self._activate(invitation_id, pending, pairing_id, claim.installation_id, digest, public_key=key)
                return PairingStatus.ACTIVE
            elif operation == "cancel":
                del self._invitations[invitation_id]
            return reservation.status

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
                or pending.direct or pending.reservation is not None or not compare_digest(pending.digest, digest)
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
        installation_id: UUID, credential_digest: bytes, *, public_key: bytes | None = None,
    ) -> Association:
        if self._installation_active(installation_id):
            raise PairingConflict("Installation already paired")
        if public_key is not None and any(association.public_key == public_key for association in self._associations.values()):
            raise PairingConflict("Key already paired")
        server_id = self.server_id
        association = Association(
            pairing_id, installation_id, pending.origin, AssociationState.ACTIVE,
            credential_digest=credential_digest, public_key=public_key,
        )
        del self._invitations[invitation_id]
        await self._commit(server_id, {**self._associations, pairing_id: association})
        return association

    def _active_with_digest(self, pairing_id: UUID, digest: bytes) -> Association | None:
        association = self._associations.get(pairing_id) if self._available else None
        if (
            association is not None and association.state is AssociationState.ACTIVE
            and association.public_key is None
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

    async def async_select_calendar_source(
        self, pairing_id: UUID | None, select: Callable[[UUID | None], Awaitable[bool]],
    ) -> None:
        """Authorize a signed phone as the single calendar writer for its entry."""
        async with self._lock:
            if pairing_id is not None:
                association = self._associations.get(pairing_id)
                if (association is None or association.state is not AssociationState.ACTIVE
                        or association.public_key is None):
                    raise InvalidPairingCredential("Pairing unavailable")
            async def write():
                if not await select(pairing_id):
                    raise PairingError("Calendar storage unavailable")

            await _persist(write)

    async def async_prepare_calendar_receiver(
        self, pairing_id: UUID, purge: Callable[[UUID], Awaitable[bool]],
    ) -> None:
        """Remove projections for a writer revoked while the entry was unloaded."""
        async with self._lock:
            _ = self.server_id
            association = self._associations.get(pairing_id)
            if association is None or association.state is AssociationState.REVOKED:
                if not await purge(pairing_id):
                    raise PairingError("Calendar storage unavailable")

    async def async_publish_calendar[T](
        self, pairing_id: UUID, secret: bytes, verify: VerifySignature,
        publish: Callable[[Association], Awaitable[T]],
    ) -> T:
        """Keep authorization, replay reservation and persistence ordered with revocation."""
        async with self._lock:
            server_id = self.server_id
            association = self._associations.get(pairing_id)
            if (association is None or association.state is not AssociationState.ACTIVE
                    or association.public_key is None or association.credential_digest is None
                    or not compare_digest(association.credential_digest, sha256(secret).digest())):
                raise InvalidPairingCredential("Pairing unavailable")
            await self._consume_nonce(pairing_id, *verify(association.public_key, association.origin, server_id))
            return await publish(association)

    async def async_revoke(
        self, pairing_id: UUID, *, purge: Callable[[UUID], Awaitable[None]] | None = None,
    ) -> bool:
        """Persist an irreversible tombstone for one association."""
        async with self._lock:
            server_id = self.server_id
            association = self._associations.get(pairing_id)
            if association is None or association.state is AssociationState.REVOKED:
                return False
            async def write():
                await self._commit(server_id, {**self._associations, pairing_id: association.revoked()})
                if purge is not None:
                    await purge(pairing_id)

            await _persist(write)
            return True

    async def async_list_active(self, *, signed_only: bool = False) -> tuple[UUID, ...]:
        """Return active association identifiers without credentials or labels."""
        async with self._lock:
            _ = self.server_id
            return tuple(
                pairing_id for pairing_id, association in self._associations.items()
                if association.state is AssociationState.ACTIVE
                and (not signed_only or association.public_key is not None)
            )
