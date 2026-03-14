"""Tests for source inventory integration in the HTTP layer."""

import unittest
from unittest.mock import AsyncMock, patch

from openclaw_tg_bridge.state import SourceInventoryStore

try:
    from openclaw_tg_bridge.server import _apply_source_discovery, _source_entry_matches_policy
except ModuleNotFoundError as exc:
    if exc.name != "fastapi":
        raise
    _apply_source_discovery = None
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


if __name__ == "__main__":
    unittest.main()
