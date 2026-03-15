"""Tests for source inventory integration in the HTTP layer."""

import unittest
from unittest.mock import AsyncMock, patch

from openclaw_tg_bridge.client import BridgeValidationError
from openclaw_tg_bridge.state import SourceInventoryStore

try:
    from openclaw_tg_bridge.server import (
        AllowedDmSender,
        _apply_source_discovery,
        _recover_dm_events,
        _source_entry_matches_policy,
    )
except ModuleNotFoundError as exc:
    if exc.name != "fastapi":
        raise
    AllowedDmSender = None
    _apply_source_discovery = None
    _recover_dm_events = None
    _source_entry_matches_policy = None


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


if __name__ == "__main__":
    unittest.main()
