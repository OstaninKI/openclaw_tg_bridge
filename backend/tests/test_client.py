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
        self.mock_tg.send_read_acknowledge = AsyncMock()
        self.mock_tg.send_message = AsyncMock()
        self.mock_tg.send_file = AsyncMock()
        self.mock_tg.edit_message = AsyncMock()
        self.mock_tg.delete_messages = AsyncMock()
        self.mock_tg.forward_messages = AsyncMock()
        self.mock_tg.get_dialogs = AsyncMock()
        self.mock_tg.get_messages = AsyncMock()
        self.mock_tg.get_me = AsyncMock(return_value=SimpleNamespace(id=1, username="me"))
        self.mock_tg.download_media = AsyncMock()
        self.mock_tg.get_participants = AsyncMock()
        self.mock_tg.delete_dialog = AsyncMock()
        self.mock_tg.get_input_entity = AsyncMock()

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

    async def test_send_message_uses_observed_entity_for_numeric_peer(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["1470044"])
        observed = SimpleNamespace(id=1470044, username="alloweduser", access_hash=12345)
        bridge.observe_peer_entity(observed, peer="1470044")
        self.mock_tg.send_message.return_value = SimpleNamespace(id=101)

        with patch("openclaw_tg_bridge.client.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = await bridge.send_message("1470044", "hello")

        self.assertEqual(result["message_id"], 101)
        self.mock_tg.get_entity.assert_not_awaited()
        self.mock_tg.send_message.assert_awaited_once_with(observed, "hello", reply_to=None)
        sleep_mock.assert_awaited()

    async def test_mark_read_uses_observed_entity(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["1470044"])
        observed = SimpleNamespace(id=1470044, username="alloweduser", access_hash=12345)
        bridge.observe_peer_entity(observed, peer="1470044")

        result = await bridge.mark_read(
            "1470044",
            max_message_id=55,
            policy_overrides={"write_allow_chat_ids": ["1470044"]},
        )

        self.assertTrue(result["ok"])
        self.mock_tg.get_entity.assert_not_awaited()
        self.mock_tg.send_read_acknowledge.assert_awaited_once_with(observed, max_id=55)

    async def test_mark_read_requires_interaction_scope(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["1470044"])
        observed = SimpleNamespace(id=1470044, username="alloweduser", access_hash=12345)
        bridge.observe_peer_entity(observed, peer="1470044")

        with self.assertRaisesRegex(BridgeForbiddenError, "Interacting is not allowed"):
            await bridge.mark_read("1470044", max_message_id=55)

        self.mock_tg.send_read_acknowledge.assert_not_awaited()

    async def test_send_typing_uses_write_scope_and_input_peer(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["1470044"])
        observed = SimpleNamespace(id=1470044, username="alloweduser", access_hash=12345)
        input_entity = SimpleNamespace(id=1470044)
        bridge.observe_peer_entity(observed, peer="1470044")
        self.mock_tg.get_input_entity.return_value = input_entity
        functions_ns = SimpleNamespace(
            messages=SimpleNamespace(SetTypingRequest=lambda **kwargs: {"kind": "typing", **kwargs})
        )
        types_ns = SimpleNamespace(SendMessageTypingAction=lambda: {"kind": "typing_action"})

        with patch("openclaw_tg_bridge.client._telethon_functions", return_value=functions_ns), patch(
            "openclaw_tg_bridge.client._telethon_types", return_value=types_ns
        ):
            result = await bridge.send_typing("1470044")

        self.assertTrue(result["ok"])
        self.mock_tg.get_entity.assert_not_awaited()
        self.mock_tg.get_input_entity.assert_awaited_once_with(observed)
        self.mock_tg.__call__.assert_awaited_once_with(
            {
                "kind": "typing",
                "peer": input_entity,
                "action": {"kind": "typing_action"},
            }
        )

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

    async def test_send_file_uses_write_scope(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["me", "42"])
        entity = SimpleNamespace(id=42, username="allowed")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.send_file.return_value = SimpleNamespace(id=333)

        with patch("openclaw_tg_bridge.client.asyncio.sleep", new=AsyncMock()), patch(
            "openclaw_tg_bridge.client.Path.exists", return_value=True
        ):
            result = await bridge.send_file("42", "/tmp/file.txt", caption="hello")

        self.assertEqual(result["message_id"], 333)
        self.mock_tg.send_file.assert_awaited_once_with(
            entity,
            "/tmp/file.txt",
            caption="hello",
            reply_to=None,
        )

    async def test_send_file_requires_self_write_access(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["42"])

        with patch("openclaw_tg_bridge.client.Path.exists", return_value=True):
            with self.assertRaisesRegex(BridgeForbiddenError, "backend-host files"):
                await bridge.send_file("42", "/tmp/file.txt")

    async def test_get_blocked_users_requires_self_write_access(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["42"])

        with self.assertRaisesRegex(BridgeForbiddenError, "listing blocked users"):
            await bridge.get_blocked_users()

    async def test_leave_chat_requires_self_write_access(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["42"])
        self.mock_tg.get_entity.return_value = SimpleNamespace(id=42, title="Ops")

        with self.assertRaisesRegex(BridgeForbiddenError, "leaving chats"):
            await bridge.leave_chat("42")

    async def test_get_message_includes_media_and_geo_metadata(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.get_messages.return_value = SimpleNamespace(
            id=71,
            text="photo from field",
            date=datetime(2026, 3, 14, 10, 0, tzinfo=timezone.utc),
            out=False,
            sender_id=7,
            sender=SimpleNamespace(first_name="Alice", last_name=None, username="alice"),
            media=SimpleNamespace(
                geo=SimpleNamespace(lat=35.1, long=33.4),
                title="Office",
                address="Cyprus",
            ),
            file=SimpleNamespace(name="photo.jpg", size=1234, mime_type="image/jpeg"),
            entities=[],
        )

        message = await bridge.get_message("42", 71)

        self.assertEqual(message["id"], 71)
        self.assertTrue(message["has_media"])
        self.assertEqual(message["file_name"], "photo.jpg")
        self.assertEqual(message["latitude"], 35.1)
        self.assertEqual(message["longitude"], 33.4)

    async def test_get_message_includes_contact_metadata(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.get_messages.return_value = SimpleNamespace(
            id=72,
            text="",
            date=datetime(2026, 3, 14, 10, 0, tzinfo=timezone.utc),
            out=False,
            sender_id=7,
            media=SimpleNamespace(
                phone_number="+12025550123",
                first_name="Alice",
                last_name="Example",
                user_id=7001,
                vcard="BEGIN:VCARD",
            ),
            entities=[],
        )

        message = await bridge.get_message("42", 72)

        self.assertTrue(message["has_media"])
        self.assertEqual(message["contact_phone"], "+12025550123")
        self.assertEqual(message["contact_first_name"], "Alice")
        self.assertEqual(message["contact_last_name"], "Example")
        self.assertEqual(message["contact_user_id"], 7001)
        self.assertEqual(message["contact_vcard"], "BEGIN:VCARD")

    async def test_download_media_fetches_message_once_and_serializes_it(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"], write_allow_chat_ids=["me", "42"])
        entity = SimpleNamespace(id=42, username="allowed")
        tg_message = SimpleNamespace(
            id=71,
            text="photo from field",
            date=datetime(2026, 3, 14, 10, 0, tzinfo=timezone.utc),
            out=False,
            sender_id=7,
            media=SimpleNamespace(),
            file=SimpleNamespace(name="photo.jpg", size=1234, mime_type="image/jpeg"),
        )
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.get_messages.return_value = tg_message
        self.mock_tg.download_media.return_value = "/tmp/photo.jpg"

        result = await bridge.download_media("42", 71, output_path="/tmp/out.bin")

        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "/tmp/photo.jpg")
        self.assertEqual(result["message"]["id"], 71)
        self.mock_tg.get_messages.assert_awaited_once_with(entity, ids=71)
        self.mock_tg.download_media.assert_awaited_once_with(tg_message, file="/tmp/out.bin")

    async def test_download_media_for_inbox_uses_read_scope_only(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"], write_allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed")
        tg_message = SimpleNamespace(
            id=81,
            media=SimpleNamespace(),
        )
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.get_messages.return_value = tg_message
        self.mock_tg.download_media.return_value = "/tmp/inbox/81_photo.jpg"

        downloaded = await bridge.download_media_for_inbox("42", 81, output_path="/tmp/inbox/81_photo.jpg")

        self.assertEqual(downloaded, "/tmp/inbox/81_photo.jpg")
        self.mock_tg.get_messages.assert_awaited_once_with(entity, ids=81)
        self.mock_tg.download_media.assert_awaited_once_with(tg_message, file="/tmp/inbox/81_photo.jpg")

    async def test_download_media_for_inbox_returns_none_for_non_media_message(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed")
        tg_message = SimpleNamespace(
            id=82,
            media=None,
        )
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.get_messages.return_value = tg_message

        downloaded = await bridge.download_media_for_inbox("42", 82, output_path="/tmp/inbox/82.bin")

        self.assertIsNone(downloaded)
        self.mock_tg.download_media.assert_not_awaited()

    async def test_search_messages_serializes_results(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.get_messages.return_value = [
            SimpleNamespace(
                id=91,
                text="release note",
                date=datetime(2026, 3, 14, 10, 0, tzinfo=timezone.utc),
                out=False,
                sender_id=7,
                sender=SimpleNamespace(first_name="Alice", last_name=None, username="alice"),
            )
        ]

        messages = await bridge.search_messages("42", "release", limit=5)

        self.assertEqual(messages[0]["id"], 91)
        self.assertEqual(messages[0]["sender_name"], "Alice")
        self.mock_tg.get_messages.assert_awaited_once_with(entity, limit=5, search="release", from_user=None)

    async def test_get_participants_uses_scoped_read(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.get_participants.return_value = [
            SimpleNamespace(id=7, username="alice", first_name="Alice", last_name=None, bot=False)
        ]

        participants = await bridge.get_participants("42", limit=50, offset=10)

        self.assertEqual(participants, [{"id": 7, "username": "alice", "title": "Alice", "bot": False}])
        self.mock_tg.get_participants.assert_awaited_once_with(entity, limit=50, offset=10)

    async def test_get_admins_supports_basic_groups(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, title="Ops")
        self.mock_tg.get_entity.return_value = entity
        functions_ns = SimpleNamespace(
            messages=SimpleNamespace(GetFullChatRequest=lambda **kwargs: {"kind": "full_chat", **kwargs})
        )
        self.mock_tg.__call__.return_value = SimpleNamespace(
            full_chat=SimpleNamespace(
                participants=SimpleNamespace(
                    participants=[
                        type("ChatParticipantAdmin", (), {"user_id": 7})(),
                        type("ChatParticipantCreator", (), {"user_id": 8})(),
                        type("ChatParticipant", (), {"user_id": 9})(),
                    ]
                )
            ),
            users=[
                SimpleNamespace(id=7, username="alice", first_name="Alice", last_name=None, bot=False),
                SimpleNamespace(id=8, username="owner", first_name="Owner", last_name=None, bot=False),
                SimpleNamespace(id=9, username="member", first_name="Member", last_name=None, bot=False),
            ],
        )

        with patch("openclaw_tg_bridge.client._telethon_functions", return_value=functions_ns):
            admins = await bridge.get_admins("42", limit=10)

        self.assertEqual(
            admins,
            [
                {"id": 7, "username": "alice", "title": "Alice", "bot": False},
                {"id": 8, "username": "owner", "title": "Owner", "bot": False},
            ],
        )

    async def test_list_contacts_filters_by_read_scope(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"], write_allow_chat_ids=["me"])
        functions_ns = SimpleNamespace(
            contacts=SimpleNamespace(GetContactsRequest=lambda **kwargs: {"kind": "contacts", **kwargs})
        )
        self.mock_tg.__call__.return_value = SimpleNamespace(
            users=[
                SimpleNamespace(id=42, username="allowed", first_name="Allowed", phone="123"),
                SimpleNamespace(id=99, username="blocked", first_name="Blocked", phone="456"),
            ]
        )

        with patch("openclaw_tg_bridge.client._telethon_functions", return_value=functions_ns):
            contacts = await bridge.list_contacts()

        self.assertEqual(contacts, [{"id": 42, "username": "allowed", "title": "Allowed", "phone": "123"}])

    async def test_list_contacts_requires_self_write_access(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])

        with self.assertRaisesRegex(BridgeForbiddenError, "listing contacts"):
            await bridge.list_contacts()

    async def test_add_contact_requires_self_write_access(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["42"])

        with self.assertRaisesRegex(BridgeForbiddenError, "creating contacts"):
            await bridge.add_contact("+123", "Alice")

    async def test_create_group_uses_me_and_user_write_scope(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["me", "42", "43"])
        self.mock_tg.get_entity.side_effect = [
            SimpleNamespace(id=42, username="alice"),
            SimpleNamespace(id=43, username="bob"),
        ]
        functions_ns = SimpleNamespace(
            messages=SimpleNamespace(CreateChatRequest=lambda **kwargs: {"kind": "create_chat", **kwargs})
        )
        self.mock_tg.__call__.return_value = SimpleNamespace(chats=[SimpleNamespace(id=555, title="Ops")])

        with patch("openclaw_tg_bridge.client._telethon_functions", return_value=functions_ns):
            result = await bridge.create_group("Ops", ["42", "43"])

        self.assertEqual(result["chat_id"], 555)
        self.mock_tg.__call__.assert_awaited_once()

    async def test_promote_admin_supports_basic_groups(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["42", "7"])
        self.mock_tg.get_entity.side_effect = [
            SimpleNamespace(id=42, title="Ops"),
            SimpleNamespace(id=7, username="alice"),
        ]
        functions_ns = SimpleNamespace(
            messages=SimpleNamespace(EditChatAdminRequest=lambda **kwargs: {"kind": "edit_chat_admin", **kwargs})
        )

        with patch("openclaw_tg_bridge.client._telethon_functions", return_value=functions_ns):
            result = await bridge.promote_admin("42", "7", title="Moderator")

        self.assertTrue(result["ok"])
        self.mock_tg.__call__.assert_awaited_once_with(
            {"kind": "edit_chat_admin", "chat_id": 42, "user_id": SimpleNamespace(id=7, username="alice"), "is_admin": True}
        )

    async def test_demote_admin_supports_basic_groups(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["42", "7"])
        self.mock_tg.get_entity.side_effect = [
            SimpleNamespace(id=42, title="Ops"),
            SimpleNamespace(id=7, username="alice"),
        ]
        functions_ns = SimpleNamespace(
            messages=SimpleNamespace(EditChatAdminRequest=lambda **kwargs: {"kind": "edit_chat_admin", **kwargs})
        )

        with patch("openclaw_tg_bridge.client._telethon_functions", return_value=functions_ns):
            result = await bridge.demote_admin("42", "7")

        self.assertTrue(result["ok"])
        self.mock_tg.__call__.assert_awaited_once_with(
            {"kind": "edit_chat_admin", "chat_id": 42, "user_id": SimpleNamespace(id=7, username="alice"), "is_admin": False}
        )

    async def test_ban_user_rejects_basic_groups(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["42", "7"])
        self.mock_tg.get_entity.side_effect = [
            SimpleNamespace(id=42, title="Ops"),
            SimpleNamespace(id=7, username="alice"),
        ]

        with self.assertRaisesRegex(BridgeValidationError, "channels and supergroups"):
            await bridge.ban_user("42", "7")

    async def test_get_banned_users_rejects_basic_groups(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        self.mock_tg.get_entity.return_value = SimpleNamespace(id=42, title="Ops")

        with self.assertRaisesRegex(BridgeValidationError, "channels and supergroups"):
            await bridge.get_banned_users("42")

    async def test_get_banned_users_uses_empty_query_filter(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        self.mock_tg.get_entity.return_value = SimpleNamespace(id=42, title="Ops", megagroup=True)
        self.mock_tg.get_participants.return_value = [
            SimpleNamespace(id=7, username="alice", first_name="Alice", last_name=None)
        ]
        types_ns = SimpleNamespace(ChannelParticipantsKicked=lambda **kwargs: {"kind": "kicked", **kwargs})

        with patch("openclaw_tg_bridge.client._telethon_types", return_value=types_ns):
            users = await bridge.get_banned_users("42", limit=10, offset=2)

        self.assertEqual(users[0]["id"], 7)
        self.mock_tg.get_participants.assert_awaited_once_with(
            self.mock_tg.get_entity.return_value,
            limit=10,
            offset=2,
            filter={"kind": "kicked", "q": ""},
        )

    async def test_send_reaction_uses_input_peer(self) -> None:
        bridge = self.create_bridge(write_allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed")
        input_entity = SimpleNamespace(id=42)
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.get_input_entity.return_value = input_entity
        functions_ns = SimpleNamespace(
            messages=SimpleNamespace(SendReactionRequest=lambda **kwargs: {"kind": "reaction", **kwargs})
        )
        types_ns = SimpleNamespace(ReactionEmoji=lambda emoticon: {"emoji": emoticon})

        with patch("openclaw_tg_bridge.client.asyncio.sleep", new=AsyncMock()), patch(
            "openclaw_tg_bridge.client._telethon_functions", return_value=functions_ns
        ), patch("openclaw_tg_bridge.client._telethon_types", return_value=types_ns):
            result = await bridge.send_reaction("42", 77, "🔥", big=True)

        self.assertTrue(result["ok"])
        self.mock_tg.get_input_entity.assert_awaited_once_with(entity)

    async def test_get_message_reactions_returns_emoji_counts(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="allowed")
        input_entity = SimpleNamespace(id=42)
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.get_input_entity.return_value = input_entity
        functions_ns = SimpleNamespace(
            messages=SimpleNamespace(GetMessageReactionsListRequest=lambda **kwargs: {"kind": "reaction_list", **kwargs})
        )
        self.mock_tg.__call__.return_value = SimpleNamespace(
            reactions=[
                SimpleNamespace(reaction=SimpleNamespace(emoticon="🔥")),
                SimpleNamespace(reaction=SimpleNamespace(emoticon="👍")),
                SimpleNamespace(reaction=SimpleNamespace(emoticon="🔥")),
            ]
        )

        with patch("openclaw_tg_bridge.client._telethon_functions", return_value=functions_ns):
            reactions = await bridge.get_message_reactions("42", 77, limit=20)

        self.assertEqual(reactions, [{"count": 2, "emoji": "🔥"}, {"count": 1, "emoji": "👍"}])

    async def test_get_chat_loads_full_channel_info(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="channel", title="Channel", broadcast=True, megagroup=False)
        self.mock_tg.get_entity.return_value = entity
        functions_ns = SimpleNamespace(
            channels=SimpleNamespace(GetFullChannelRequest=lambda **kwargs: {"kind": "full_channel", **kwargs})
        )
        self.mock_tg.__call__.return_value = SimpleNamespace(
            full_chat=SimpleNamespace(about="About text", participants_count=17)
        )

        with patch("openclaw_tg_bridge.client._telethon_functions", return_value=functions_ns):
            chat = await bridge.get_chat("42")

        self.assertEqual(chat["participants_count"], 17)
        self.assertEqual(chat["about"], "About text")

    async def test_get_recent_actions_serializes_events(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="channel", title="Channel", broadcast=True)
        self.mock_tg.get_entity.return_value = entity
        functions_ns = SimpleNamespace(
            channels=SimpleNamespace(GetAdminLogRequest=lambda **kwargs: {"kind": "admin_log", **kwargs})
        )
        self.mock_tg.__call__.return_value = SimpleNamespace(
            events=[
                SimpleNamespace(
                    id=1,
                    date=datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc),
                    user_id=7,
                    action=SimpleNamespace(),
                )
            ]
        )

        with patch("openclaw_tg_bridge.client._telethon_functions", return_value=functions_ns):
            events = await bridge.get_recent_actions("42", limit=10)

        self.assertEqual(events[0]["id"], 1)
        self.assertEqual(events[0]["user_id"], 7)

    async def test_get_recent_actions_rejects_basic_groups(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        self.mock_tg.get_entity.return_value = SimpleNamespace(id=42, title="Ops")

        with self.assertRaisesRegex(BridgeValidationError, "channels and supergroups"):
            await bridge.get_recent_actions("42", limit=10)


if __name__ == "__main__":
    unittest.main()
