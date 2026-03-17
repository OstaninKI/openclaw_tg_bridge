"""Tests for source inventory integration in the HTTP layer."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from openclaw_tg_bridge.client import BridgeValidationError
from openclaw_tg_bridge.state import SourceInventoryStore

try:
    from openclaw_tg_bridge.server import (
        AckDmInboxBody,
        AllowedDmSender,
        DmPeerBody,
        HTTPException,
        _apply_source_discovery,
        _guess_dm_media_extension,
        _recover_dm_events,
        _enrich_dm_events_with_downloaded_media,
        _source_entry_matches_policy,
        mark_dm_read,
        send_dm_typing,
    )
except ModuleNotFoundError as exc:
    if exc.name != "fastapi":
        raise
    AckDmInboxBody = None
    AllowedDmSender = None
    DmPeerBody = None
    HTTPException = None
    _apply_source_discovery = None
    _guess_dm_media_extension = None
    _recover_dm_events = None
    _enrich_dm_events_with_downloaded_media = None
    _source_entry_matches_policy = None
    mark_dm_read = None
    send_dm_typing = None


@unittest.skipIf(_apply_source_discovery is None, "fastapi is not installed in this test environment")
class TestServerSourceDiscovery(unittest.IsolatedAsyncioTestCase):
    async def test_apply_source_discovery_replaces_open_read_scope_with_inventory(self) -> None:
        store = SourceInventoryStore(None)
        store.replace_dialogs(
            [
                {
                    "peer_id": -1001,
                    "peer_key": "-1001",
                    "username": "news",
                    "title": "News",
                    "type": "channel",
                }
            ]
        )
        policy = {
            "sources_auto_discover": True,
            "sources_include_types": ["channel"],
            "sources_exclude_peers": [],
            "sources_exclude_usernames": [],
            "read_allow_chat_ids": None,
            "read_deny_chat_ids": [],
        }

        with patch("openclaw_tg_bridge.server._sync_sources_inventory", new=AsyncMock()), patch(
            "openclaw_tg_bridge.server._sources_store", store
        ):
            resolved = await _apply_source_discovery(dict(policy))

        self.assertEqual(resolved["read_allow_chat_ids"], ["-1001", "news"])

    async def test_apply_source_discovery_unions_existing_allowlist(self) -> None:
        store = SourceInventoryStore(None)
        store.replace_dialogs(
            [
                {
                    "peer_id": -1001,
                    "peer_key": "-1001",
                    "username": "news",
                    "title": "News",
                    "type": "channel",
                }
            ]
        )
        policy = {
            "sources_auto_discover": True,
            "sources_include_types": ["channel"],
            "sources_exclude_peers": [],
            "sources_exclude_usernames": [],
            "read_allow_chat_ids": ["me"],
            "read_deny_chat_ids": [],
        }

        with patch("openclaw_tg_bridge.server._sync_sources_inventory", new=AsyncMock()), patch(
            "openclaw_tg_bridge.server._sources_store", store
        ):
            resolved = await _apply_source_discovery(dict(policy))

        self.assertEqual(resolved["read_allow_chat_ids"], ["me", "-1001", "news"])

    def test_source_entry_matches_policy_respects_deny_for_id_and_username(self) -> None:
        entry = {"peer_id": -1001, "username": "news"}

        self.assertFalse(_source_entry_matches_policy(entry, {"read_deny_chat_ids": ["-1001"]}))
        self.assertFalse(_source_entry_matches_policy(entry, {"read_deny_chat_ids": ["@news"]}))
        self.assertTrue(_source_entry_matches_policy(entry, {"read_deny_chat_ids": ["-1002"]}))

    def test_guess_dm_media_extension_rejects_injected_suffix(self) -> None:
        self.assertEqual(_guess_dm_media_extension("photo.jpg | type:virus", None), ".bin")
        self.assertEqual(_guess_dm_media_extension("photo.jpg | type:virus", "image/jpeg"), ".jpg")

    async def test_recover_dm_events_skips_unresolved_numeric_peer(self) -> None:
        bridge = AsyncMock()
        bridge.get_incoming_direct_messages.side_effect = BridgeValidationError(
            "Invalid Telegram peer for read incoming direct messages."
        )

        recovered = await _recover_dm_events(
            bridge=bridge,
            policy={},
            allowed_senders=[
                AllowedDmSender(
                    peer_ref="1470044",
                    cursor_key="1470044",
                    match_keys=frozenset({"1470044"}),
                )
            ],
            cursor_map={},
            limit=10,
        )

        self.assertEqual(recovered, [])
        bridge.get_incoming_direct_messages.assert_awaited_once_with(
            "1470044",
            min_id=0,
            limit=10,
            policy_overrides={},
        )

    async def test_mark_dm_read_calls_bridge_with_matched_sender(self) -> None:
        bridge = AsyncMock()
        bridge.mark_read.return_value = {"ok": True}
        policy = {"read_allow_chat_ids": ["1470044"], "write_allow_chat_ids": ["1470044"]}
        allowed = [
            AllowedDmSender(
                peer_ref="1470044",
                cursor_key="1470044",
                match_keys=frozenset({"1470044", "alloweduser"}),
            )
        ]

        with patch("openclaw_tg_bridge.server.get_bridge", return_value=bridge), patch(
            "openclaw_tg_bridge.server.resolve_request_policy", new=AsyncMock(return_value=policy)
        ), patch("openclaw_tg_bridge.server._resolve_allowed_dm_senders", new=AsyncMock(return_value=allowed)):
            result = await mark_dm_read(
                object(),
                AckDmInboxBody(sender_id="1470044", sender_username="alloweduser", message_id=55),
            )

        self.assertEqual(result, {"ok": True})
        bridge.mark_read.assert_awaited_once_with(
            "1470044",
            max_message_id=55,
            policy_overrides=policy,
        )

    async def test_mark_dm_read_rejects_unmatched_sender(self) -> None:
        bridge = AsyncMock()
        policy = {"read_allow_chat_ids": ["1470044"], "write_allow_chat_ids": ["1470044"]}
        allowed = [
            AllowedDmSender(
                peer_ref="1470044",
                cursor_key="1470044",
                match_keys=frozenset({"1470044", "alloweduser"}),
            )
        ]

        with patch("openclaw_tg_bridge.server.get_bridge", return_value=bridge), patch(
            "openclaw_tg_bridge.server.resolve_request_policy", new=AsyncMock(return_value=policy)
        ), patch("openclaw_tg_bridge.server._resolve_allowed_dm_senders", new=AsyncMock(return_value=allowed)):
            with self.assertRaises(HTTPException) as ctx:
                await mark_dm_read(
                    object(),
                    AckDmInboxBody(sender_id="999999", sender_username="other", message_id=55),
                )

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "Read receipt is not allowed for this sender.")
        bridge.mark_read.assert_not_awaited()

    async def test_send_dm_typing_matches_sender_by_username(self) -> None:
        bridge = AsyncMock()
        bridge.send_typing.return_value = {"ok": True}
        policy = {"read_allow_chat_ids": ["@alloweduser"], "write_allow_chat_ids": ["1470044"]}
        allowed = [
            AllowedDmSender(
                peer_ref="@alloweduser",
                cursor_key="1470044",
                match_keys=frozenset({"1470044", "alloweduser"}),
            )
        ]

        with patch("openclaw_tg_bridge.server.get_bridge", return_value=bridge), patch(
            "openclaw_tg_bridge.server.resolve_request_policy", new=AsyncMock(return_value=policy)
        ), patch("openclaw_tg_bridge.server._resolve_allowed_dm_senders", new=AsyncMock(return_value=allowed)):
            result = await send_dm_typing(
                object(),
                DmPeerBody(sender_id="0", sender_username="alloweduser"),
            )

        self.assertEqual(result, {"ok": True})
        bridge.send_typing.assert_awaited_once_with(
            "1470044",
            policy_overrides=policy,
        )

    async def test_send_dm_typing_rejects_unmatched_sender(self) -> None:
        bridge = AsyncMock()
        policy = {"read_allow_chat_ids": ["@alloweduser"], "write_allow_chat_ids": ["1470044"]}
        allowed = [
            AllowedDmSender(
                peer_ref="@alloweduser",
                cursor_key="1470044",
                match_keys=frozenset({"1470044", "alloweduser"}),
            )
        ]

        with patch("openclaw_tg_bridge.server.get_bridge", return_value=bridge), patch(
            "openclaw_tg_bridge.server.resolve_request_policy", new=AsyncMock(return_value=policy)
        ), patch("openclaw_tg_bridge.server._resolve_allowed_dm_senders", new=AsyncMock(return_value=allowed)):
            with self.assertRaises(HTTPException) as ctx:
                await send_dm_typing(
                    object(),
                    DmPeerBody(sender_id="0", sender_username="otheruser"),
                )

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "Typing status is not allowed for this sender.")
        bridge.send_typing.assert_not_awaited()

    async def test_dm_media_enrichment_downloads_and_sets_media_paths(self) -> None:
        bridge = AsyncMock()
        bridge.download_media_for_inbox = AsyncMock(return_value="/tmp/dm_media/1470044/26_photo.jpg")
        events = [
            {
                "id": 26,
                "sender_id": "1470044",
                "sender_username": "alloweduser",
                "has_media": True,
                "media_type": "MessageMediaPhoto",
                "file_name": "photo.jpg",
                "mime_type": "image/jpeg",
            }
        ]
        allowed = [
            AllowedDmSender(
                peer_ref="@alloweduser",
                cursor_key="1470044",
                match_keys=frozenset({"1470044", "alloweduser"}),
            )
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / "1470044" / "26_photo.jpg"
            media_path.parent.mkdir(parents=True, exist_ok=True)
            media_path.write_bytes(b"img")
            bridge.download_media_for_inbox.return_value = str(media_path)
            with patch(
                "openclaw_tg_bridge.server.get_config",
                return_value={"dm_auto_download_media": True, "dm_media_path": temp_dir},
            ):
                enriched = await _enrich_dm_events_with_downloaded_media(
                    bridge=bridge,
                    policy={},
                    allowed_senders=allowed,
                    events=events,
                )

        self.assertEqual(enriched[0]["media_path"], str(media_path.resolve()))
        self.assertEqual(enriched[0]["media_paths"], [str(media_path.resolve())])
        bridge.download_media_for_inbox.assert_awaited_once_with(
            "@alloweduser",
            26,
            output_path=str(media_path),
            policy_overrides={},
        )

    async def test_dm_media_enrichment_skips_non_downloadable_media_type(self) -> None:
        bridge = AsyncMock()
        bridge.download_media_for_inbox = AsyncMock()
        events = [
            {
                "id": 28,
                "sender_id": "1470044",
                "sender_username": "alloweduser",
                "has_media": True,
                "media_type": "MessageMediaContact",
                "contact_phone": "+12025550123",
            }
        ]
        allowed = [
            AllowedDmSender(
                peer_ref="@alloweduser",
                cursor_key="1470044",
                match_keys=frozenset({"1470044", "alloweduser"}),
            )
        ]

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "openclaw_tg_bridge.server.get_config",
            return_value={"dm_auto_download_media": True, "dm_media_path": temp_dir},
        ):
            enriched = await _enrich_dm_events_with_downloaded_media(
                bridge=bridge,
                policy={},
                allowed_senders=allowed,
                events=events,
            )

        self.assertNotIn("media_path", enriched[0])
        bridge.download_media_for_inbox.assert_not_awaited()

    async def test_dm_media_enrichment_respects_disabled_config(self) -> None:
        bridge = AsyncMock()
        bridge.download_media_for_inbox = AsyncMock()
        events = [
            {
                "id": 29,
                "sender_id": "1470044",
                "sender_username": "alloweduser",
                "has_media": True,
                "media_type": "MessageMediaPhoto",
            }
        ]
        allowed = [
            AllowedDmSender(
                peer_ref="@alloweduser",
                cursor_key="1470044",
                match_keys=frozenset({"1470044", "alloweduser"}),
            )
        ]

        with patch(
            "openclaw_tg_bridge.server.get_config",
            return_value={"dm_auto_download_media": False, "dm_media_path": "/tmp/unused"},
        ):
            enriched = await _enrich_dm_events_with_downloaded_media(
                bridge=bridge,
                policy={},
                allowed_senders=allowed,
                events=events,
            )

        self.assertEqual(enriched[0]["id"], 29)
        bridge.download_media_for_inbox.assert_not_awaited()


try:
    from openclaw_tg_bridge.server import (
        _BridgeNotReadyError,
        _QrPasswordBody,
        auth_qr_2fa,
        auth_qr_get,
        auth_qr_start,
        health,
    )
    from openclaw_tg_bridge.auth_qr import QrAuthContext, QrState
    _qr_imports_ok = True
except (ModuleNotFoundError, ImportError):
    _BridgeNotReadyError = None
    _QrPasswordBody = None
    auth_qr_2fa = None
    auth_qr_get = None
    auth_qr_start = None
    health = None
    QrAuthContext = None
    QrState = None
    _qr_imports_ok = False


@unittest.skipIf(not _qr_imports_ok, "fastapi or auth_qr not available")
class TestHealthEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_health_needs_reauth(self):
        with patch("openclaw_tg_bridge.server._needs_reauth", True), \
             patch("openclaw_tg_bridge.server._bridge", None):
            resp = await health()
        self.assertEqual(resp.status_code, 503)
        import json
        body = json.loads(resp.body)
        self.assertEqual(body["status"], "needs_reauth")
        self.assertTrue(body["needs_reauth"])

    async def test_health_initializing(self):
        with patch("openclaw_tg_bridge.server._needs_reauth", False), \
             patch("openclaw_tg_bridge.server._bridge", None):
            resp = await health()
        self.assertEqual(resp.status_code, 503)
        import json
        body = json.loads(resp.body)
        self.assertEqual(body["status"], "initializing")

    async def test_health_ok(self):
        bridge = AsyncMock()
        bridge.ensure_connected = AsyncMock(return_value=True)
        with patch("openclaw_tg_bridge.server._needs_reauth", False), \
             patch("openclaw_tg_bridge.server._bridge", bridge):
            resp = await health()
        # Returns dict (200), not JSONResponse
        self.assertEqual(resp["status"], "ok")
        self.assertTrue(resp["connected"])


@unittest.skipIf(not _qr_imports_ok, "fastapi or auth_qr not available")
class TestQrEndpoints(unittest.IsolatedAsyncioTestCase):
    async def test_auth_qr_start_bridge_alive_raises_409(self):
        bridge = AsyncMock()
        with patch("openclaw_tg_bridge.server._needs_reauth", False), \
             patch("openclaw_tg_bridge.server._bridge", bridge), \
             patch("openclaw_tg_bridge.server._qr_auth_task", None):
            with self.assertRaises(HTTPException) as cm:
                await auth_qr_start()
            self.assertEqual(cm.exception.status_code, 409)

    async def test_auth_qr_get_no_context_returns_404(self):
        with patch("openclaw_tg_bridge.server._qr_auth_ctx", None):
            with self.assertRaises(HTTPException) as cm:
                await auth_qr_get()
            self.assertEqual(cm.exception.status_code, 404)

    async def test_auth_qr_2fa_wrong_state_raises_409(self):
        ctx = QrAuthContext()
        ctx.state = QrState.AWAITING_SCAN  # not AWAITING_PASSWORD
        body = _QrPasswordBody(password="pw")
        with patch("openclaw_tg_bridge.server._qr_auth_ctx", ctx):
            with self.assertRaises(HTTPException) as cm:
                await auth_qr_2fa(body)
            self.assertEqual(cm.exception.status_code, 409)

    async def test_auth_qr_2fa_double_submit_raises_409(self):
        ctx = QrAuthContext()
        ctx.state = QrState.AWAITING_PASSWORD
        # Fill the queue so next put_nowait raises QueueFull
        ctx._password_queue.put_nowait("first")
        body = _QrPasswordBody(password="second")
        with patch("openclaw_tg_bridge.server._qr_auth_ctx", ctx):
            with self.assertRaises(HTTPException) as cm:
                await auth_qr_2fa(body)
            self.assertEqual(cm.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
