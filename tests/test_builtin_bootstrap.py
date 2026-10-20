import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from builtin_fixtures import RAW_KEY, encrypt, sqlite_bytes
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_wechat_notice_digest.builtin.bootstrap import BootstrapController


class BootstrapTests(unittest.TestCase):
    def test_explicit_start_validates_selected_database_and_saves_once(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / "account" / "db_storage"
            (storage / "message").mkdir(parents=True)
            (storage / "message/message_0.db").write_bytes(encrypt(sqlite_bytes()))
            capture = Mock(return_value=RAW_KEY)
            credentials = Mock()
            controller = BootstrapController(storage, "account", credentials, capture=capture)
            capture.assert_not_called()
            controller.start()
            controller.wait(3)
            self.assertEqual(controller.status()["state"], "ready")
            credentials.save.assert_called_once_with("account", RAW_KEY)
            self.assertNotIn(RAW_KEY.hex(), str(controller.status()))

    def test_cancel_and_timeout_cannot_save_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / "a/db_storage"
            storage.mkdir(parents=True)
            credentials = Mock()
            def capture(storage, cancel, timeout):
                cancel.wait(timeout)
                raise TimeoutError("secret-should-not-escape")
            controller = BootstrapController(storage, "a", credentials, capture=capture)
            controller.start(timeout=1)
            controller.cancel()
            controller.wait(2)
            credentials.save.assert_not_called()
            self.assertNotIn("secret-should-not-escape", str(controller.status()))

    def test_wrong_account_and_bad_key_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / "a/db_storage"
            (storage / "message").mkdir(parents=True)
            (storage / "message/message_0.db").write_bytes(encrypt(sqlite_bytes()))
            with self.assertRaises(ValueError):
                BootstrapController(storage, "b", Mock())
            credentials = Mock()
            controller = BootstrapController(storage, "a", credentials, capture=lambda *args: b"x" * 32)
            controller.start()
            controller.wait(3)
            self.assertEqual(controller.status()["state"], "error")
            credentials.save.assert_not_called()
