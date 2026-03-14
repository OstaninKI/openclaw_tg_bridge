"""Tests for configuration helpers and policy profiles."""

import json
import tempfile
import unittest
from pathlib import Path

from openclaw_tg_bridge.auth import resolve_local_session_file, write_session_string
from openclaw_tg_bridge.config import (
    PolicyStore,
    parse_request_overrides,
    resolve_effective_policy,
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
                "X-OpenClaw-Policy-Profile": "owner",
                "X-OpenClaw-Reply-Delay-Sec": "2.5",
                "x-openclaw-reply-delay-max-sec": "4",
                "X-OpenClaw-Allow-From": "@User,-1001",
                "X-OpenClaw-Deny-From": "spam",
                "X-OpenClaw-Write-To": "@allowed",
                "X-OpenClaw-Deny-Write-To": "-1002",
            }
        )

        self.assertEqual(overrides["policy_profile"], "owner")
        self.assertEqual(overrides["reply_delay_sec"], 2.5)
        self.assertEqual(overrides["reply_delay_max_sec"], 4.0)
        self.assertEqual(overrides["read_allow_chat_ids"], ["@User", "-1001"])
        self.assertEqual(overrides["read_deny_chat_ids"], ["spam"])
        self.assertEqual(overrides["write_allow_chat_ids"], ["@allowed"])
        self.assertEqual(overrides["write_deny_chat_ids"], ["-1002"])

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

    def test_policy_store_and_effective_policy_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            policy_file = Path(temp_dir) / "policy.json"
            policy_file.write_text(
                json.dumps(
                    {
                        "defaults": {
                            "replyDelaySec": 2,
                            "read": {"allow": ["*"], "deny": []},
                            "write": {"allow": [], "deny": []},
                        },
                        "profiles": {
                            "owner": {
                                "read": {"allow": ["me", "-1001"]},
                                "write": {"allow": ["me"]},
                            },
                            "shared": {
                                "read": {"allow": ["-1001", "@news"]},
                                "write": {"allow": []},
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            config = {
                "reply_delay_sec": 1.0,
                "reply_delay_max_sec": None,
                "allow_chat_ids": ["*"],
                "deny_chat_ids": [],
                "write_allow_chat_ids": [],
                "write_deny_chat_ids": [],
                "policy_default_profile": "owner",
            }

            effective = resolve_effective_policy(
                config,
                PolicyStore(str(policy_file)),
                {"read_deny_chat_ids": ["blocked"]},
            )

            self.assertEqual(effective["policy_profile"], "owner")
            self.assertEqual(effective["reply_delay_sec"], 2.0)
            self.assertEqual(effective["read_allow_chat_ids"], ["me", "-1001"])
            self.assertEqual(effective["write_allow_chat_ids"], ["me"])
            self.assertEqual(effective["read_deny_chat_ids"], ["blocked"])

    def test_unknown_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            policy_file = Path(temp_dir) / "policy.json"
            policy_file.write_text(json.dumps({"profiles": {}}), encoding="utf-8")
            config = {
                "reply_delay_sec": 1.0,
                "reply_delay_max_sec": None,
                "allow_chat_ids": ["*"],
                "deny_chat_ids": [],
                "write_allow_chat_ids": [],
                "write_deny_chat_ids": [],
                "policy_default_profile": None,
            }

            with self.assertRaisesRegex(ValueError, "Unknown policy profile"):
                resolve_effective_policy(
                    config,
                    PolicyStore(str(policy_file)),
                    {"policy_profile": "missing"},
                )


if __name__ == "__main__":
    unittest.main()
