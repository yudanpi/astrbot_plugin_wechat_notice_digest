import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_wechat_notice_digest.builtin.worker import ReaderRuntime


class LivenessTests(unittest.TestCase):
    def test_device_suffix_is_not_a_different_logical_account(self):
        from astrbot_plugin_wechat_notice_digest.builtin.source import BuiltinNoticeSource

        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / "wxid_demo_1234/db_storage"
            storage.mkdir(parents=True)
            source = BuiltinNoticeSource(Path(directory) / "plugin", storage, "wxid_demo_1234")
            self.assertEqual(source.account, "wxid_demo")
            runtime = ReaderRuntime(
                Path(directory) / "plugin",
                storage,
                "wxid_demo_1234",
                live_check=lambda force=False: True,
            )
            runtime.credentials.save("wxid_demo_1234", b"x" * 32)
            try:
                self.assertEqual(
                    runtime.dispatch("get", {"path": "/api/chat/accounts"})["accounts"],
                    ["wxid_demo"],
                )
            finally:
                runtime.close()

    def test_saved_key_is_not_live_when_wechat_closed_or_other_account_logged_in(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / "a/db_storage"
            storage.mkdir(parents=True)
            runtime = ReaderRuntime(
                Path(directory) / "plugin", storage, "a", live_check=lambda force=False: False
            )
            runtime.credentials.save("a", b"x" * 32)
            try:
                self.assertEqual(runtime.status()["state"], "offline")
                with self.assertRaises(RuntimeError):
                    runtime.dispatch("get", {"path": "/api/chat/accounts"})
            finally:
                runtime.close()

    def test_live_binding_can_recover_without_new_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / "a/db_storage"
            storage.mkdir(parents=True)
            active = [False]
            runtime = ReaderRuntime(
                Path(directory) / "plugin", storage, "a", live_check=lambda force=False: active[0]
            )
            runtime.credentials.save("a", b"x" * 32)
            try:
                self.assertEqual(runtime.status()["state"], "offline")
                active[0] = True
                self.assertEqual(
                    runtime.dispatch("get", {"path": "/api/chat/accounts"})["accounts"], ["a"]
                )
            finally:
                runtime.close()
