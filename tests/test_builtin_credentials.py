import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_wechat_notice_digest.builtin.credentials import CredentialStore


class CredentialTests(unittest.TestCase):
    def test_windows_dpapi_roundtrip_and_account_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(Path(directory) / "private")
            raw = b"synthetic-key-no-live-credentials!"[:32]
            store.save("test_account", raw)
            self.assertEqual(store.load("test_account"), raw)
            self.assertNotIn(raw.hex(), store.path.read_text())
            with self.assertRaises(RuntimeError):
                store.load("different_account")

    def test_protection_failure_never_writes_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(Path(directory) / "private")
            with patch("astrbot_plugin_wechat_notice_digest.builtin.credentials._protect", side_effect=OSError("sensitive")):
                with self.assertRaisesRegex(RuntimeError, "Credential protection failed"):
                    store.save("a", b"x" * 32)
            self.assertFalse(store.path.exists())

    def test_tampered_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(Path(directory) / "private")
            store.save("a", b"x" * 32)
            store.path.write_text(json.dumps({"blob": base64.b64encode(b"bad").decode()}))
            with self.assertRaisesRegex(RuntimeError, "Credential unavailable"):
                store.load("a")
