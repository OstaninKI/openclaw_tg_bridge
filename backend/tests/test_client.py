"""Unit tests for bridge client logic without real Telethon."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from openclaw_tg_bridge.client import (
    BridgeClient,
    BridgeForbiddenError,
    BridgeRateLimitError,
    BridgeValidationError,
    _normalize_peer,
    build_policy,
)


class FakeFloodWaitError(Exception):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"Flood wait for {seconds}s")
        self.seconds = seconds


class UsernameInvalidError(Exception):
    pass


class TestClientHelpers(unittest.TestCase):
    def test_normalize_peer(self) -> None:
        self.assertEqual(_normalize_peer("me"), "me")
        self.assertEqual(_normalize_peer("@Durov"), "durov")
        self.assertEqual(_normalize_peer("DUROV"), "durov")
        self.assertEqual(_normalize_peer("-1000123"), "-1000123")
        self.assertEqual(_normalize_peer(12345), "12345")

    def test_build_policy_read_is_open_and_write_is_closed_by_default(self) -> None:
        policy = build_policy(
            read_allow_chat_ids=None,
            read_deny_chat_ids=None,
            write_allow_chat_ids=None,
            write_deny_chat_ids=None,
            reply_delay_sec=2,
            reply_delay_max_sec=1,
        )
        self.assertTrue(policy.read_scope.allow_all)
        self.assertFalse(policy.write_scope.allow_all)
        self.assertEqual(policy.write_scope.allow, frozenset())
        self.assertEqual(policy.reply_delay_sec, 2.0)
        self.assertIsNone(policy.reply_delay_max_sec)


class TestBridgeClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.mock_tg = MagicMock()
        self.mock_tg.is_connected = MagicMock(return_value=True)
        self.mock_tg.__call__ = AsyncMock()
        self.mock_tg.connect = AsyncMock()
        self.mock_tg.disconnect = AsyncMock()
        self.mock_tg.get_entity = AsyncMock()
        self.mock_tg.send_message = AsyncMock()
        self.mock_tg.get_dialogs = AsyncMock()
        self.mock_tg.get_messages = AsyncMock()
        self.mock_tg.get_me = AsyncMock(return_value=SimpleNamespace(id=1, username="me"))

    def create_bridge(self, **kwargs: object) -> BridgeClient:
        options: dict[str, object] = {
            "reply_delay_sec": 0,
            "allow_chat_ids": None,
            "deny_chat_ids": None,
            "write_allow_chat_ids": None,
            "write_deny_chat_ids": None,
            "rpc_timeout_sec": 5,
            "flood_wait_max_sleep_sec": 2,
        }
        options.update(kwargs)
        return BridgeClient(self.mock_tg, **options)

    async def test_send_message_rejects_empty_text(self) -> None:
        bridge = self.create_bridge()
        with self.assertRaisesRegex(BridgeValidationError, "empty"):
            await bridge.send_message("me", "   ")
        self.mock_tg.send_message.assert_not_called()

    async def test_send_message_is_denied_by_default(self) -> None:
        bridge = self.create_bridge()
        self.mock_tg.get_entity.return_value = SimpleNamespace(id=42, username="allowed")
        with self.assertRaisesRegex(BridgeForbiddenError, "Writing is not allowed"):
            await bridge.send_message("@allowed", "hello")

    async def test_send_message_uses_resolved_entity_for_write_allowlist(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="AllowedUser")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.send_message.return_value = SimpleNamespace(id=100)

        with patch("openclaw_tg_bridge.client.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = await bridge.send_message("@AllowedUser", "hello")

        self.assertEqual(result["message_id"], 100)
        self.mock_tg.send_message.assert_awaited_once_with(entity, "hello", reply_to=None)
        sleep_mock.assert_awaited()

    async def test_send_message_blocks_when_resolved_entity_matches_write_denylist(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["42"], write_deny_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="AllowedUser")
        self.mock_tg.get_entity.return_value = entity

        with self.assertRaisesRegex(BridgeForbiddenError, "Writing is not allowed"):
            await bridge.send_message("@AllowedUser", "hello")

        self.mock_tg.send_message.assert_not_called()

    async def test_get_dialogs_filters_by_read_scope(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["-1001", "@keepme"], deny_chat_ids=["dropme"])
        self.mock_tg.get_dialogs.return_value = [
            SimpleNamespace(id=-1001, entity=SimpleNamespace(id=1, username="keepme", title="Keep")),
            SimpleNamespace(id=-1002, entity=SimpleNamespace(id=2, username="dropme", title="Drop")),
            SimpleNamespace(id=-1003, entity=SimpleNamespace(id=3, username="other", title="Other")),
        ]

        dialogs = await bridge.get_dialogs(limit=10)

        self.assertEqual(dialogs, [{"id": -1001, "title": "Keep", "username": "keepme"}])

    async def test_send_message_short_flood_wait_retries_once(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.send_message.side_effect = [FakeFloodWaitError(1), SimpleNamespace(id=77)]

        with patch("openclaw_tg_bridge.client.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = await bridge.send_message("42", "hello")

        self.assertEqual(result["message_id"], 77)
        self.assertEqual(self.mock_tg.send_message.await_count, 2)
        sleep_mock.assert_any_await(1)

    async def test_send_message_long_flood_wait_returns_rate_limit(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.send_message.side_effect = FakeFloodWaitError(30)

        with patch("openclaw_tg_bridge.client.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(BridgeRateLimitError) as cm:
                await bridge.send_message("42", "hello")

        self.assertEqual(cm.exception.retry_after, 30)

    async def test_get_messages_maps_invalid_peer_error(self) -> None:
        bridge = self.create_bridge()
        self.mock_tg.get_entity.side_effect = UsernameInvalidError()

        with self.assertRaisesRegex(BridgeValidationError, "Invalid Telegram peer"):
            await bridge.get_messages("@missing")

    async def test_policy_override_is_enforced_on_backend(self) -> None:
        bridge = self.create_bridge()
        entity = SimpleNamespace(id=99, username="limited")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.send_message.return_value = SimpleNamespace(id=88)

        with patch("openclaw_tg_bridge.client.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(BridgeForbiddenError):
                await bridge.send_message(
                    "@limited",
                    "hello",
                    policy_overrides={"write_allow_chat_ids": ["100"]},
                )

            result = await bridge.send_message(
                "@limited",
                "hello",
                policy_overrides={"write_allow_chat_ids": ["99"], "reply_delay_sec": 0},
            )

        self.assertEqual(result["message_id"], 88)

    async def test_get_messages_passes_min_id(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.get_messages.return_value = [
            SimpleNamespace(
                id=51,
                text="delta",
                date="2026-03-14",
                out=False,
                sender_id=7,
                sender=SimpleNamespace(first_name="Alice", last_name=None, username="alice"),
                reply_to=SimpleNamespace(reply_to_top_id=900),
                reply_to_msg_id=50,
            ),
        ]

        messages = await bridge.get_messages("42", limit=10, min_id=50)

        self.assertEqual(messages[0]["id"], 51)
        self.assertEqual(messages[0]["sender_id"], 7)
        self.assertEqual(messages[0]["sender_name"], "Alice")
        self.assertEqual(messages[0]["topic_id"], 900)
        self.assertEqual(messages[0]["reply_to_message_id"], 50)
        self.assertEqual(messages[0]["chat_id"], 42)
        self.assertEqual(messages[0]["chat_username"], "allowed")
        self.mock_tg.get_messages.assert_awaited_once_with(entity, limit=10, min_id=50)

    async def test_get_messages_returns_oldest_first(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.get_messages.return_value = [
            SimpleNamespace(id=52, text="newer", date="2026-03-14", out=False, sender_id=7),
            SimpleNamespace(id=51, text="older", date="2026-03-14", out=False, sender_id=7),
        ]

        messages = await bridge.get_messages("42", limit=10, min_id=50)

        self.assertEqual([message["id"] for message in messages], [51, 52])

    async def test_list_topics_uses_forum_topics_request(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="forumchat", forum=True)
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.__call__.return_value = SimpleNamespace(
            topics=[
                SimpleNamespace(
                    id=12,
                    top_message=900,
                    title="Releases",
                    unread_count=3,
                    pinned=True,
                    closed=False,
                    hidden=False,
                )
            ]
        )
        functions_ns = SimpleNamespace(
            messages=SimpleNamespace(
                GetForumTopicsRequest=lambda **kwargs: {"kind": "topics", **kwargs},
            )
        )

        with patch("openclaw_tg_bridge.client._telethon_functions", return_value=functions_ns):
            topics = await bridge.list_topics("42", limit=10)

        self.assertEqual(
            topics,
            [
                {
                    "id": 12,
                    "topic_id": 900,
                    "title": "Releases",
                    "icon_color": None,
                    "icon_emoji_id": None,
                    "closed": False,
                    "hidden": False,
                    "pinned": True,
                    "unread_count": 3,
                    "unread_mentions_count": None,
                    "unread_reactions_count": None,
                    "from_id": None,
                    "date": None,
                }
            ],
        )
        self.mock_tg.__call__.assert_awaited_once_with(
            {
                "kind": "topics",
                "peer": entity,
                "offset_date": None,
                "offset_id": 0,
                "offset_topic": 0,
                "limit": 10,
                "q": "",
            }
        )

    async def test_get_messages_reads_specific_topic_with_sender_lookup(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="forumchat", forum=True)
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.__call__.return_value = SimpleNamespace(
            messages=[
                SimpleNamespace(
                    id=53,
                    text="newer",
                    date="2026-03-14",
                    out=False,
                    from_id=SimpleNamespace(user_id=7),
                    reply_to=SimpleNamespace(reply_to_top_id=900),
                ),
                SimpleNamespace(
                    id=52,
                    text="older",
                    date="2026-03-14",
                    out=False,
                    from_id=SimpleNamespace(user_id=7),
                ),
            ],
            users=[SimpleNamespace(id=7, first_name="Alice", last_name=None, username="alice")],
            chats=[],
        )
        functions_ns = SimpleNamespace(
            messages=SimpleNamespace(
                GetRepliesRequest=lambda **kwargs: {"kind": "replies", **kwargs},
            )
        )

        with patch("openclaw_tg_bridge.client._telethon_functions", return_value=functions_ns):
            messages = await bridge.get_messages("42", limit=10, min_id=50, topic_id=900)

        self.assertEqual([message["id"] for message in messages], [52, 53])
        self.assertEqual(messages[0]["sender_name"], "Alice")
        self.assertEqual(messages[0]["sender_username"], "alice")
        self.assertEqual(messages[0]["topic_id"], 900)
        self.assertEqual(messages[1]["topic_id"], 900)
        self.mock_tg.__call__.assert_awaited_once_with(
            {
                "kind": "replies",
                "peer": entity,
                "msg_id": 900,
                "offset_id": 0,
                "offset_date": None,
                "add_offset": 0,
                "limit": 10,
                "max_id": 0,
                "min_id": 50,
                "hash": 0,
            }
        )

    async def test_get_messages_filters_by_since_unix_and_paginates(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed")
        self.mock_tg.get_entity.return_value = entity
        since_unix = int(datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc).timestamp())
        self.mock_tg.get_messages.side_effect = [
            [
                SimpleNamespace(id=105, text="newest", date=datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc), out=False, sender_id=7),
                SimpleNamespace(id=104, text="still fresh", date=datetime(2026, 3, 14, 11, 30, tzinfo=timezone.utc), out=False, sender_id=7),
            ],
            [
                SimpleNamespace(id=103, text="inside window", date=datetime(2026, 3, 14, 10, 0, tzinfo=timezone.utc), out=False, sender_id=7),
                SimpleNamespace(id=102, text="too old", date=datetime(2026, 3, 13, 8, 0, tzinfo=timezone.utc), out=False, sender_id=7),
            ],
        ]

        messages = await bridge.get_messages("42", limit=5, since_unix=since_unix)

        self.assertEqual([message["id"] for message in messages], [103, 104, 105])
        self.assertEqual(self.mock_tg.get_messages.await_args_list[0].kwargs, {"limit": 5})
        self.assertEqual(self.mock_tg.get_messages.await_args_list[1].kwargs, {"limit": 3, "offset_id": 104})

    async def test_get_topic_messages_filters_by_since_unix(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="forumchat", forum=True)
        self.mock_tg.get_entity.return_value = entity
        since_unix = int(datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc).timestamp())
        self.mock_tg.__call__.side_effect = [
            SimpleNamespace(
                messages=[
                    SimpleNamespace(
                        id=205,
                        text="fresh",
                        date=datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc),
                        out=False,
                        from_id=SimpleNamespace(user_id=7),
                    ),
                    SimpleNamespace(
                        id=204,
                        text="older but still inside",
                        date=datetime(2026, 3, 14, 10, 0, tzinfo=timezone.utc),
                        out=False,
                        from_id=SimpleNamespace(user_id=7),
                    ),
                ],
                users=[SimpleNamespace(id=7, first_name="Alice", last_name=None, username="alice")],
                chats=[],
            ),
            SimpleNamespace(
                messages=[
                    SimpleNamespace(
                        id=203,
                        text="too old",
                        date=datetime(2026, 3, 13, 8, 0, tzinfo=timezone.utc),
                        out=False,
                        from_id=SimpleNamespace(user_id=7),
                    ),
                ],
                users=[SimpleNamespace(id=7, first_name="Alice", last_name=None, username="alice")],
                chats=[],
            ),
        ]
        functions_ns = SimpleNamespace(
            messages=SimpleNamespace(
                GetRepliesRequest=lambda **kwargs: {"kind": "replies", **kwargs},
            )
        )

        with patch("openclaw_tg_bridge.client._telethon_functions", return_value=functions_ns):
            messages = await bridge.get_messages("42", limit=5, topic_id=900, since_unix=since_unix)

        self.assertEqual([message["id"] for message in messages], [204, 205])
        self.assertEqual(messages[0]["topic_id"], 900)
        self.assertEqual(self.mock_tg.__call__.await_args_list[0].args[0]["offset_id"], 0)
        self.assertEqual(self.mock_tg.__call__.await_args_list[1].args[0]["offset_id"], 204)

    async def test_discover_source_dialogs_returns_serializable_inventory_entries(self) -> None:
        bridge = self.create_bridge()
        self.mock_tg.get_dialogs.return_value = [
            SimpleNamespace(
                id=-1001,
                is_channel=True,
                is_group=False,
                is_user=False,
                entity=SimpleNamespace(id=-1001, username="news", title="News", broadcast=True, forum=False),
            ),
            SimpleNamespace(
                id=55,
                is_channel=False,
                is_group=False,
                is_user=True,
                entity=SimpleNamespace(id=55, username="friend", first_name="Friend"),
            ),
        ]

        entries = await bridge.discover_source_dialogs(limit=100)

        self.assertEqual(entries[0]["peer_id"], -1001)
        self.assertEqual(entries[0]["username"], "news")
        self.assertEqual(entries[0]["type"], "channel")
        self.assertEqual(entries[1]["type"], "user")

    async def test_resolve_peer_identifiers_returns_id_and_username(self) -> None:
        bridge = self.create_bridge()
        self.mock_tg.get_entity.return_value = SimpleNamespace(id=42, username="AllowedUser")

        identifiers = await bridge.resolve_peer_identifiers("@AllowedUser")

        self.assertEqual(identifiers["id"], "42")
        self.assertEqual(identifiers["username"], "alloweduser")

    async def test_get_incoming_direct_messages_skips_outbound_messages(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed", first_name="Allowed")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.get_messages.return_value = [
            SimpleNamespace(id=9, text="outbound", date="2026-03-14", out=True, sender_id=42),
            SimpleNamespace(id=10, text="inbound", date="2026-03-14", out=False, sender_id=42),
        ]

        messages = await bridge.get_incoming_direct_messages("42", min_id=8, limit=10)

        self.assertEqual([message["id"] for message in messages], [10])


if __name__ == "__main__":
    unittest.main()
