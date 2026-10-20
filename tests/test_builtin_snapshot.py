import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from builtin_fixtures import RAW_KEY, encrypt, sqlite_bytes, wal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_wechat_notice_digest.builtin.snapshot import (
    SnapshotChangedError,
    SnapshotIntegrityError,
    decrypt_snapshot,
    read_snapshot,
)


class SnapshotTests(unittest.TestCase):
    def test_wal_mode_database_header_can_be_queried_in_memory(self):
        raw = bytearray(sqlite_bytes())
        raw[18:20] = b"\2\2"
        plaintext = decrypt_snapshot(encrypt(raw), RAW_KEY)
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(plaintext)
            self.assertEqual(connection.execute("SELECT value FROM sample").fetchone(), ("hello",))
        finally:
            connection.close()

    def test_decrypts_queryable_database_and_rejects_wrong_key_or_corruption(self):
        encrypted = encrypt(sqlite_bytes())
        plaintext = decrypt_snapshot(encrypted, RAW_KEY)
        connection = sqlite3.connect(":memory:")
        connection.deserialize(plaintext)
        self.assertEqual(connection.execute("SELECT value FROM sample").fetchone(), ("hello",))
        connection.close()
        for data, key in [(encrypted, b"x" * 32), (encrypted[:-1] + b"x", RAW_KEY)]:
            with self.assertRaises(SnapshotIntegrityError):
                decrypt_snapshot(data, key)

    def test_only_committed_wal_frames_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.db"
            path.write_bytes(b"a" * 4096 + b"b" * 4096)
            Path(str(path) + "-wal").write_bytes(wal([(2, 2, b"c" * 4096), (1, 0, b"d" * 4096)]))
            self.assertEqual(read_snapshot(path), b"a" * 4096 + b"c" * 4096)
            data = bytearray(Path(str(path) + "-wal").read_bytes())
            data[60] ^= 1
            Path(str(path) + "-wal").write_bytes(data)
            with self.assertRaises(SnapshotIntegrityError):
                read_snapshot(path)

    def test_changing_pair_retries_and_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.db"
            path.write_bytes(b"a" * 4096)
            with patch(
                "astrbot_plugin_wechat_notice_digest.builtin.snapshot._metadata",
                side_effect=range(12),
            ) as metadata:
                with self.assertRaises(SnapshotChangedError):
                    read_snapshot(path)
                self.assertEqual(metadata.call_count, 12)
