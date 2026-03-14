"""Unit tests for bridge client logic without real Telethon."""

import asyncio
import unittest
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

    def test_build_policy_treats_star_as_unrestricted(self) -> None:
        policy = build_policy(
            allow_chat_ids=["*"],
            deny_chat_ids=["@BadUser"],
            reply_delay_sec=2,
            reply_delay_max_sec=1,
        )
        self.assertIsNone(policy.allow)
        self.assertEqual(policy.deny, frozenset({"baduser"}))
        self.assertEqual(policy.reply_delay_sec, 2.0)
        self.assertIsNone(policy.reply_delay_max_sec)


class TestBridgeClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.mock_tg = MagicMock()
        self.mock_tg.is_connected = MagicMock(return_value=True)
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

    async def test_send_message_uses_resolved_entity_for_allowlist(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="AllowedUser")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.send_message.return_value = SimpleNamespace(id=100)

        with patch("openclaw_tg_bridge.client.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = await bridge.send_message("@AllowedUser", "hello")

        self.assertEqual(result["message_id"], 100)
        self.mock_tg.send_message.assert_awaited_once_with(entity, "hello", reply_to=None)
        sleep_mock.assert_awaited()

    async def test_send_message_blocks_when_resolved_entity_matches_denylist(self) -> None:
        bridge = self.create_bridge(deny_chat_ids=["42"])
        entity = SimpleNamespace(id=42, username="AllowedUser")
        self.mock_tg.get_entity.return_value = entity

        with self.assertRaisesRegex(BridgeForbiddenError, "not allowed"):
            await bridge.send_message("@AllowedUser", "hello")

        self.mock_tg.send_message.assert_not_called()

    async def test_get_dialogs_filters_by_dialog_id_and_username(self) -> None:
        bridge = self.create_bridge(allow_chat_ids=["-1001", "@keepme"], deny_chat_ids=["dropme"])
        self.mock_tg.get_dialogs.return_value = [
            SimpleNamespace(id=-1001, entity=SimpleNamespace(id=1, username="keepme", title="Keep")),
            SimpleNamespace(id=-1002, entity=SimpleNamespace(id=2, username="dropme", title="Drop")),
            SimpleNamespace(id=-1003, entity=SimpleNamespace(id=3, username="other", title="Other")),
        ]

        dialogs = await bridge.get_dialogs(limit=10)

        self.assertEqual(dialogs, [{"id": -1001, "title": "Keep", "username": "keepme"}])

    async def test_send_message_short_flood_wait_retries_once(self) -> None:
        bridge = self.create_bridge()
        entity = SimpleNamespace(id=42, username="allowed")
        self.mock_tg.get_entity.return_value = entity
        self.mock_tg.send_message.side_effect = [FakeFloodWaitError(1), SimpleNamespace(id=77)]

        with patch("openclaw_tg_bridge.client.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = await bridge.send_message("42", "hello")

        self.assertEqual(result["message_id"], 77)
        self.assertEqual(self.mock_tg.send_message.await_count, 2)
        sleep_mock.assert_any_await(1)

    async def test_send_message_long_flood_wait_returns_rate_limit(self) -> None:
        bridge = self.create_bridge()
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
                    policy_overrides={"allow_chat_ids": ["100"]},
                )

            result = await bridge.send_message(
                "@limited",
                "hello",
                policy_overrides={"allow_chat_ids": ["99"], "reply_delay_sec": 0},
            )

        self.assertEqual(result["message_id"], 88)


if __name__ == "__main__":
    unittest.main()
