"""Focused pairing tests with independent reads of simulated HA stores."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from secrets import token_bytes
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_receiver import pairing_module

AssociationState = pairing_module.AssociationState
PairingError = pairing_module.PairingError
PairingRegistry = pairing_module.PairingRegistry
Permissions = pairing_module.Permissions
validate_origin = pairing_module.validate_origin


class PairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(TemporaryDirectory(dir=Path(__file__).resolve().parents[1]))
        self.directory = Path(directory)
        _, self.registry = await self.open_registry()
        self.store = self.registry._store
        self.guard = self.registry._guard

    async def open_registry(self):
        hass = SimpleNamespace(
            config=SimpleNamespace(path=self.directory.joinpath), data={}, directory=self.directory,
        )
        return hass, await PairingRegistry.async_open(hass)

    async def invite(self, administrator_id="admin"):
        return await self.registry.async_invite(
            administrator_id=administrator_id, confirmed_origin="https://HA.example:443/",
        )

    async def activate(self, invite, *, secret=None, installation_id=None, administrator_id="admin"):
        secret = token_bytes(32) if secret is None else secret
        association = await self.registry.async_activate_confirmed(
            invite.invitation_id, invite.code, administrator_id=administrator_id,
            installation_id=installation_id or uuid4(), credential_digest=sha256(secret).digest(),
        )
        return association, secret

    async def test_valid_and_default_permissions(self):
        before = datetime.now(UTC)
        invite = await self.invite()
        self.assertRegex(invite.code, r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
        self.assertNotIn(invite.code, repr(invite))
        self.assertEqual(invite.origin, "https://ha.example")
        self.assertGreaterEqual(invite.expires_at, before + timedelta(minutes=5))
        self.assertLessEqual(invite.expires_at, datetime.now(UTC) + timedelta(minutes=5))
        entry, secret = await self.activate(invite)
        self.assertEqual(entry.state, AssociationState.ACTIVE)
        self.assertEqual(entry.ha_permissions, Permissions())
        self.assertEqual(entry.phone_permissions, Permissions())
        self.assertEqual(await self.registry.async_authenticate(entry.pairing_id, secret), entry)
        self.assertIsNone(await self.registry.async_authenticate(entry.pairing_id, sha256(secret).digest()))
        saved = self.store.path.read_text("utf-8")
        self.assertNotIn(secret.hex(), saved)
        self.assertNotIn(invite.code, saved)
        self.assertIn(sha256(secret).hexdigest(), saved)
        self.assertTrue(self.store.options["private"] and self.guard.options["private"])
        self.assertTrue(self.store.options["atomic_writes"] and self.guard.options["atomic_writes"])

    async def test_expiry_and_quotas(self):
        now = 10.0
        self.registry = PairingRegistry(self.store, self.guard, clock=lambda: now)
        await self.registry.async_load()
        invite = await self.invite()
        with self.assertRaisesRegex(PairingError, "limit"):
            await self.invite()
        for index in range(7):
            await self.invite(f"admin{index}")
        with self.assertRaisesRegex(PairingError, "limit"):
            await self.invite("another")
        now += 300
        with self.assertRaisesRegex(PairingError, "Invalid invitation"):
            await self.activate(invite)
        await self.activate(await self.invite())

    async def test_cancel_and_replay(self):
        invite = await self.invite()
        self.assertFalse(await self.registry.async_cancel(invite.invitation_id, administrator_id="other"))
        with self.assertRaisesRegex(PairingError, "Invalid invitation"):
            await self.activate(invite, administrator_id="other")
        self.assertTrue(await self.registry.async_cancel(invite.invitation_id, administrator_id="admin"))
        with self.assertRaisesRegex(PairingError, "Invalid invitation"):
            await self.activate(invite)
        invite = await self.invite()
        results = await asyncio.gather(self.activate(invite), self.activate(invite), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, tuple) for result in results), 1)
        self.assertEqual(sum(isinstance(result, PairingError) for result in results), 1)

    async def test_isolation_and_revocation(self):
        first, first_secret = await self.activate(await self.invite())
        second, second_secret = await self.activate(await self.invite())
        self.assertIsNone(await self.registry.async_authenticate(first.pairing_id, second_secret))
        invite = await self.invite()
        with self.assertRaisesRegex(PairingError, "already paired"):
            await self.activate(invite, installation_id=first.installation_id)
        self.assertTrue(await self.registry.async_revoke(first.pairing_id))
        self.assertFalse(await self.registry.async_revoke(first.pairing_id))
        self.assertNotIn(sha256(first_secret).hexdigest(), self.store.path.read_text("utf-8"))
        self.assertIsNone(await self.registry.async_authenticate(first.pairing_id, first_secret))
        self.assertEqual(await self.registry.async_authenticate(second.pairing_id, second_secret), second)
        replacement, _ = await self.activate(invite, installation_id=first.installation_id)
        self.assertNotEqual(replacement.pairing_id, first.pairing_id)

    async def test_restart_keeps_identity_and_revocation_but_discards_invites(self):
        first, first_secret = await self.activate(await self.invite())
        second, second_secret = await self.activate(await self.invite())
        await self.registry.async_revoke(first.pairing_id)
        invite = await self.invite()
        server_id = self.registry.server_id
        hass, self.registry = await self.open_registry()
        self.assertIs(await PairingRegistry.async_open(hass), self.registry)
        self.assertEqual(self.registry.server_id, server_id)
        self.assertIsNone(await self.registry.async_authenticate(first.pairing_id, first_secret))
        self.assertEqual(await self.registry.async_authenticate(second.pairing_id, second_secret), second)
        with self.assertRaisesRegex(PairingError, "Invalid invitation"):
            await self.activate(invite)

    async def test_backup_rollback_and_missing_guard_fail_closed(self):
        entry, secret = await self.activate(await self.invite())
        old_backup = self.store.path.read_bytes()
        server_id = self.registry.server_id
        await self.registry.async_revoke(entry.pairing_id)
        for remove_guard in (False, True):
            with self.subTest(remove_guard=remove_guard):
                self.store.path.write_bytes(old_backup)
                if remove_guard:
                    self.guard.path.unlink()
                _, self.registry = await self.open_registry()
                self.assertEqual(self.registry.server_id, server_id)
                self.assertIsNone(await self.registry.async_authenticate(entry.pairing_id, secret))
                self.assertEqual((await self.store.async_load())["associations"][0]["state"], "revoked")

    async def test_quarantined_registry_or_guard_does_not_erase_recoverable_associations(self):
        entry, secret = await self.activate(await self.invite())
        for store in (self.store, self.guard):
            with self.subTest(store=store.key):
                original = store.path.read_bytes()
                quarantine = store.path.with_name(store.path.name + ".corrupt.recette")
                store.path.rename(quarantine)
                for _ in range(2):
                    with self.assertRaisesRegex(OSError, "quarantined"):
                        await self.open_registry()
                self.assertFalse(store.path.exists())
                self.assertEqual(quarantine.read_bytes(), original)
                store.path.write_bytes(original)
                _, restored = await self.open_registry()
                self.assertEqual(await restored.async_authenticate(entry.pairing_id, secret), entry)

    async def test_silent_store_failure_is_detected_and_does_not_restore_access(self):
        entry, secret = await self.activate(await self.invite())
        self.store.fail = True
        with self.assertRaisesRegex(PairingError, "persist pairing registry"):
            await self.registry.async_revoke(entry.pairing_id)
        self.assertIsNone(await self.registry.async_authenticate(entry.pairing_id, secret))
        _, self.registry = await self.open_registry()
        self.assertIsNone(await self.registry.async_authenticate(entry.pairing_id, secret))

    async def test_cancelled_revocation_finishes_the_commit(self):
        entry, secret = await self.activate(await self.invite())
        writing = asyncio.Event()
        resume = asyncio.Event()
        save = self.store.async_save

        async def pause_write(data):
            writing.set()
            await resume.wait()
            await save(data)

        with patch.object(self.store, "async_save", pause_write):
            revocation = asyncio.create_task(self.registry.async_revoke(entry.pairing_id))
            await asyncio.wait_for(writing.wait(), timeout=2)
            revocation.cancel()
            resume.set()
            with self.assertRaises(asyncio.CancelledError):
                await revocation
        _, self.registry = await self.open_registry()
        self.assertIsNone(await self.registry.async_authenticate(entry.pairing_id, secret))

    async def test_association_proof_and_permissions_are_bound_to_origin(self):
        invitation = await self.invite()
        entry, secret = await self.activate(invitation)
        foreign = "https://other.example"
        calendar = pairing_module.Consultation.CALENDAR
        self.assertEqual(
            await self.registry.async_check_pairing(entry.pairing_id, secret, origin=invitation.origin),
            pairing_module.PairingStatus.ACTIVE,
        )
        with self.assertRaises(pairing_module.InvalidPairingCredential):
            await self.registry.async_check_pairing(entry.pairing_id, secret, origin=foreign)
        authorize = self.registry.async_authorize_consultation
        self.assertFalse(await authorize(entry.pairing_id, secret, invitation.origin, calendar))
        grant = Permissions(consultation=frozenset({calendar}))
        self.registry._associations[entry.pairing_id] = replace(entry, ha_permissions=grant)
        self.assertFalse(await authorize(entry.pairing_id, secret, invitation.origin, calendar))
        self.registry._associations[entry.pairing_id] = replace(
            entry, ha_permissions=grant, phone_permissions=grant,
        )
        self.assertTrue(await authorize(entry.pairing_id, secret, invitation.origin, calendar))
        self.assertFalse(await authorize(entry.pairing_id, secret, foreign, calendar))
        await self.registry.async_revoke(entry.pairing_id)
        self.assertFalse(await authorize(entry.pairing_id, secret, invitation.origin, calendar))

    def test_origin_validation(self):
        self.assertEqual(validate_origin("https://[2001:0DB8::1]:8123/"), "https://[2001:db8::1]:8123")
        self.assertEqual(validate_origin("https://192.0.2.1:8123"), "https://192.0.2.1:8123")
        for candidate in (
            "http://ha.example", "https://user@ha.example", "https://ha.example/local",
            "https://ha.example?", "https://ha.example#", "https://ha.example:",
            "https://ha.example:0", "https://ha.example\n", "https://[::1].example",
        ):
            with self.subTest(candidate=candidate), self.assertRaises(PairingError):
                validate_origin(candidate)
