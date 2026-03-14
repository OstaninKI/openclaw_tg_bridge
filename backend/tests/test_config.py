"""Tests for configuration helpers."""

import tempfile
import unittest
from pathlib import Path

from openclaw_tg_bridge.auth import resolve_local_session_file, write_session_string
from openclaw_tg_bridge.config import (
    parse_request_overrides,
    resolve_session_path,
)


class TestConfigHelpers(unittest.TestCase):
    def test_resolve_session_path_adds_suffix(self) -> None:
        resolved = resolve_session_path("~/telegram/openclaw_user")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.suffix, ".session")

    def test_parse_request_overrides_is_case_insensitive(self) -> None:
        overrides = parse_request_overrides(
            {
                "X-OpenClaw-Reply-Delay-Sec": "2.5",
                "x-openclaw-reply-delay-max-sec": "4",
                "X-OpenClaw-Allow-From": "@User,-1001",
                "X-OpenClaw-Deny-From": "spam",
            }
        )

        self.assertEqual(overrides["reply_delay_sec"], 2.5)
        self.assertEqual(overrides["reply_delay_max_sec"], 4.0)
        self.assertEqual(overrides["allow_chat_ids"], ["@User", "-1001"])
        self.assertEqual(overrides["deny_chat_ids"], ["spam"])

    def test_parse_request_overrides_rejects_invalid_numbers(self) -> None:
        with self.assertRaisesRegex(ValueError, "reply delay"):
            parse_request_overrides({"X-OpenClaw-Reply-Delay-Sec": "oops"})

    def test_resolve_local_session_file_creates_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_file = resolve_local_session_file(f"{temp_dir}/nested/openclaw_user")
            self.assertTrue(session_file.parent.exists())
            self.assertEqual(session_file.suffix, ".session")

    def test_write_session_string_writes_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "session.txt"
            write_session_string(str(output), "secret")
            self.assertEqual(output.read_text(encoding="utf-8"), "secret\n")


if __name__ == "__main__":
    unittest.main()
