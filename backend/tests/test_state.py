"""Tests for JSON-backed state stores."""

import tempfile
import unittest
from pathlib import Path

from openclaw_tg_bridge.state import DmCursorStore, SourceInventoryStore


class TestSourceInventoryStore(unittest.TestCase):
    def test_in_memory_store_keeps_cache_without_path(self) -> None:
        store = SourceInventoryStore(None)

        data = store.replace_dialogs(
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

        self.assertEqual(data["meta"]["dialog_count"], 1)
        self.assertEqual(store.load()["dialogs"][0]["peer_key"], "-1001")

    def test_allowed_identifiers_include_id_and_username_and_exclude_users(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SourceInventoryStore(Path(temp_dir) / "sources.json")
            store.replace_dialogs(
                [
                    {
                        "peer_id": -1001,
                        "peer_key": "-1001",
                        "username": "news",
                        "title": "News",
                        "type": "channel",
                    },
                    {
                        "peer_id": -1002,
                        "peer_key": "-1002",
                        "username": "workchat",
                        "title": "Work Chat",
                        "type": "supergroup",
                    },
                    {
                        "peer_id": 55,
                        "peer_key": "55",
                        "username": "friend",
                        "title": "Friend",
                        "type": "user",
                    },
                ]
            )

            identifiers = store.allowed_identifiers()
            dialogs = store.list_dialogs(exclude_usernames=["workchat"])

            self.assertEqual(identifiers, ["-1001", "news", "-1002", "workchat"])
            self.assertEqual([dialog["peer_key"] for dialog in dialogs], ["-1001"])


class TestDmCursorStore(unittest.TestCase):
    def test_ack_persists_highest_cursor_only(self) -> None:
        store = DmCursorStore(None)

        self.assertEqual(store.get_cursor("dm_inbox", "123"), 0)
        self.assertEqual(store.ack("dm_inbox", "123", 9), 9)
        self.assertEqual(store.ack("dm_inbox", "123", 7), 9)
        self.assertEqual(store.get_consumer_cursors("dm_inbox"), {"123": 9})


if __name__ == "__main__":
    unittest.main()
