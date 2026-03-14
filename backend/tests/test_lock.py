"""Tests for backend process lock."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openclaw_tg_bridge.lock import ProcessLock, ProcessLockError


class TestProcessLock(unittest.TestCase):
    def test_acquire_and_release_creates_and_removes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "bridge.lock"
            lock = ProcessLock(lock_path)

            lock.acquire()
            self.assertTrue(lock_path.exists())

            lock.release()
            self.assertFalse(lock_path.exists())

    def test_stale_pid_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "bridge.lock"
            lock_path.write_text("999999\n", encoding="utf-8")
            lock = ProcessLock(lock_path)

            with patch("openclaw_tg_bridge.lock._pid_alive", return_value=False):
                lock.acquire()

            self.assertTrue(lock_path.exists())
            lock.release()

    def test_live_pid_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "bridge.lock"
            lock_path.write_text("12345\n", encoding="utf-8")
            lock = ProcessLock(lock_path)

            with patch("openclaw_tg_bridge.lock._pid_alive", return_value=True):
                with self.assertRaises(ProcessLockError):
                    lock.acquire()


if __name__ == "__main__":
    unittest.main()
