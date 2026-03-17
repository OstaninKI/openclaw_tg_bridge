"""Tests for QR authentication state machine (auth_qr.py)."""

import asyncio
import datetime
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from openclaw_tg_bridge.auth_qr import (
    QrAuthContext,
    QrState,
    _refresh_qr,
    run_qr_login_flow,
)


# ---------------------------------------------------------------------------
# Fake Telethon error classes (avoid importing telethon in unit tests)
# ---------------------------------------------------------------------------

class _FakeSessionPasswordNeededError(Exception):
    pass


class _FakePasswordHashInvalidError(Exception):
    pass


# ---------------------------------------------------------------------------
# _refresh_qr
# ---------------------------------------------------------------------------

class TestRefreshQr(unittest.TestCase):
    def _make_qr_login(self, url="tg://login?token=abc", expires_in_sec=25.0):
        qr = MagicMock()
        qr.url = url
        qr.expires = datetime.datetime.now(tz=datetime.timezone.utc) + datetime.timedelta(
            seconds=expires_in_sec
        )
        return qr

    def test_url_string_stored(self):
        ctx = QrAuthContext()
        qr = self._make_qr_login(url="tg://login?token=xyz")
        with patch("openclaw_tg_bridge.auth_qr.generate_qr_png_b64", return_value=""), \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_ascii", return_value=""):
            _refresh_qr(ctx, qr)
        self.assertEqual(ctx.qr_url, "tg://login?token=xyz")

    def test_url_bytes_decoded(self):
        ctx = QrAuthContext()
        qr = self._make_qr_login(url=b"tg://login?token=bytes")
        with patch("openclaw_tg_bridge.auth_qr.generate_qr_png_b64", return_value=""), \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_ascii", return_value=""):
            _refresh_qr(ctx, qr)
        self.assertEqual(ctx.qr_url, "tg://login?token=bytes")

    def test_expiry_from_datetime(self):
        ctx = QrAuthContext()
        qr = self._make_qr_login(expires_in_sec=20.0)
        before = time.monotonic()
        with patch("openclaw_tg_bridge.auth_qr.generate_qr_png_b64", return_value=""), \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_ascii", return_value=""):
            _refresh_qr(ctx, qr)
        after = time.monotonic()
        # expires_at should be ~20s from now
        self.assertGreater(ctx.qr_expires_at, before + 15.0)
        self.assertLess(ctx.qr_expires_at, after + 25.0)

    def test_fallback_on_bad_expires(self):
        ctx = QrAuthContext()
        qr = MagicMock()
        qr.url = "tg://login?token=x"
        qr.expires = "not-a-datetime"  # will raise AttributeError in subtraction
        before = time.monotonic()
        with patch("openclaw_tg_bridge.auth_qr.generate_qr_png_b64", return_value=""), \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_ascii", return_value=""):
            _refresh_qr(ctx, qr)
        after = time.monotonic()
        # fallback is 30 seconds
        self.assertGreater(ctx.qr_expires_at, before + 25.0)
        self.assertLess(ctx.qr_expires_at, after + 35.0)

    def test_png_generation_failure_does_not_raise(self):
        ctx = QrAuthContext()
        qr = self._make_qr_login()
        with patch("openclaw_tg_bridge.auth_qr.generate_qr_png_b64", side_effect=ImportError("no qrcode")), \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_ascii", return_value=""):
            _refresh_qr(ctx, qr)  # should not raise
        self.assertEqual(ctx.qr_png_b64, "")


# ---------------------------------------------------------------------------
# run_qr_login_flow
# ---------------------------------------------------------------------------

class TestRunQrLoginFlow(unittest.IsolatedAsyncioTestCase):
    def _make_client(self):
        client = AsyncMock()
        return client

    def _make_qr_login(self):
        qr_login = AsyncMock()
        qr_login.url = "tg://login?token=test"
        qr_login.expires = datetime.datetime.now(tz=datetime.timezone.utc) + datetime.timedelta(seconds=30)
        return qr_login

    async def test_happy_path(self):
        """QR is scanned immediately, no 2FA → state=DONE."""
        client = self._make_client()
        qr_login = self._make_qr_login()
        qr_login.wait = AsyncMock(return_value=None)  # success
        client.qr_login = AsyncMock(return_value=qr_login)

        ctx = QrAuthContext()

        patch_errors = patch.dict(
            "sys.modules",
            {
                "telethon.errors": MagicMock(
                    SessionPasswordNeededError=_FakeSessionPasswordNeededError,
                    PasswordHashInvalidError=_FakePasswordHashInvalidError,
                )
            },
        )
        with patch_errors, \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_png_b64", return_value=""), \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_ascii", return_value=""):
            await run_qr_login_flow(client, ctx)

        self.assertEqual(ctx.state, QrState.DONE)
        self.assertTrue(ctx._done_event.is_set())

    async def test_timeout_then_rescan(self):
        """First wait() times out (QR expired) → recreate → second wait() succeeds."""
        client = self._make_client()
        qr_login = self._make_qr_login()
        qr_login.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
        qr_login.recreate = AsyncMock()
        client.qr_login = AsyncMock(return_value=qr_login)

        ctx = QrAuthContext()

        patch_errors = patch.dict(
            "sys.modules",
            {
                "telethon.errors": MagicMock(
                    SessionPasswordNeededError=_FakeSessionPasswordNeededError,
                    PasswordHashInvalidError=_FakePasswordHashInvalidError,
                )
            },
        )
        with patch_errors, \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_png_b64", return_value=""), \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_ascii", return_value=""):
            await run_qr_login_flow(client, ctx)

        qr_login.recreate.assert_awaited_once()
        self.assertEqual(ctx.state, QrState.DONE)

    async def test_2fa_flow(self):
        """QR scanned → SessionPasswordNeededError → password submitted → DONE."""
        client = self._make_client()
        qr_login = self._make_qr_login()
        qr_login.wait = AsyncMock(side_effect=_FakeSessionPasswordNeededError())
        client.qr_login = AsyncMock(return_value=qr_login)
        client.sign_in = AsyncMock(return_value=None)

        ctx = QrAuthContext()
        # Pre-fill password queue so flow doesn't block
        await ctx._password_queue.put("correct_pw")

        patch_errors = patch.dict(
            "sys.modules",
            {
                "telethon.errors": MagicMock(
                    SessionPasswordNeededError=_FakeSessionPasswordNeededError,
                    PasswordHashInvalidError=_FakePasswordHashInvalidError,
                )
            },
        )
        with patch_errors, \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_png_b64", return_value=""), \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_ascii", return_value=""):
            await run_qr_login_flow(client, ctx)

        client.sign_in.assert_awaited_once_with(password="correct_pw")
        self.assertEqual(ctx.state, QrState.DONE)
        self.assertIsNone(ctx.error)

    async def test_2fa_retry_on_wrong_password(self):
        """Wrong 2FA password → state stays AWAITING_PASSWORD → retry → DONE."""
        client = self._make_client()
        qr_login = self._make_qr_login()
        qr_login.wait = AsyncMock(side_effect=_FakeSessionPasswordNeededError())
        client.qr_login = AsyncMock(return_value=qr_login)
        # First call raises PasswordHashInvalidError, second succeeds
        client.sign_in = AsyncMock(
            side_effect=[_FakePasswordHashInvalidError(), None]
        )

        ctx = QrAuthContext()
        await ctx._password_queue.put("wrong_pw")

        # We need to inject the second password after the first fails.
        # Use a task that waits briefly then enqueues correct password.
        async def _inject_second_password():
            await asyncio.sleep(0)  # yield to let flow consume first pw
            await asyncio.sleep(0)  # one more yield for retry path
            await ctx._password_queue.put("correct_pw")

        patch_errors = patch.dict(
            "sys.modules",
            {
                "telethon.errors": MagicMock(
                    SessionPasswordNeededError=_FakeSessionPasswordNeededError,
                    PasswordHashInvalidError=_FakePasswordHashInvalidError,
                )
            },
        )
        with patch_errors, \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_png_b64", return_value=""), \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_ascii", return_value=""):
            await asyncio.gather(
                run_qr_login_flow(client, ctx),
                _inject_second_password(),
            )

        self.assertEqual(client.sign_in.await_count, 2)
        self.assertEqual(ctx.state, QrState.DONE)
        self.assertIsNone(ctx.error)

    async def test_error_sets_state_and_reraises(self):
        """Unexpected error in wait() → state=ERROR, error message set, exception re-raised."""
        client = self._make_client()
        qr_login = self._make_qr_login()
        qr_login.wait = AsyncMock(side_effect=RuntimeError("network failure"))
        client.qr_login = AsyncMock(return_value=qr_login)

        ctx = QrAuthContext()

        patch_errors = patch.dict(
            "sys.modules",
            {
                "telethon.errors": MagicMock(
                    SessionPasswordNeededError=_FakeSessionPasswordNeededError,
                    PasswordHashInvalidError=_FakePasswordHashInvalidError,
                )
            },
        )
        with patch_errors, \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_png_b64", return_value=""), \
             patch("openclaw_tg_bridge.auth_qr.generate_qr_ascii", return_value=""):
            with self.assertRaises(RuntimeError):
                await run_qr_login_flow(client, ctx)

        self.assertEqual(ctx.state, QrState.ERROR)
        self.assertIn("network failure", ctx.error)
        self.assertTrue(ctx._done_event.is_set())


if __name__ == "__main__":
    unittest.main()
