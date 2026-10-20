import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from builtin_fixtures import RAW_KEY, encrypt, sqlite_bytes, wal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_wechat_notice_digest.builtin.worker import ReaderRuntime
from astrbot_plugin_wechat_notice_digest.collector import Collector, NoticeStore


def message_sql(contents):
    table = "Msg_" + hashlib.md5(b"g@chatroom").hexdigest()
    rows = ";".join(
        f"INSERT INTO {table} VALUES({i},{i},{timestamp},'交材料{i}',1,'<msgsource><atuserlist>notify@all</atuserlist></msgsource>')"
        for i, timestamp in contents
    )
    return (
        f"CREATE TABLE Name2Id(user_name); INSERT INTO Name2Id VALUES('sender'); CREATE TABLE {table}(local_id,server_id,create_time,message_content,real_sender_id,source);"
        + rows
        + ";"
    )


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_wal_to_notice_with_blocking_and_restart_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "a/db_storage"
            (storage / "contact").mkdir(parents=True)
            (storage / "message").mkdir()
            contact = sqlite_bytes(
                "CREATE TABLE contact(username,nick_name,remark); INSERT INTO contact VALUES('g@chatroom','班级群','');"
            )
            (storage / "contact/contact.db").write_bytes(encrypt(contact))
            initial = encrypt(sqlite_bytes(message_sql([(1, 900)])))
            updated = encrypt(sqlite_bytes(message_sql([(1, 900), (2, 910)])))
            message = storage / "message/message_0.db"
            message.write_bytes(initial)
            frames = [
                (
                    i // 4096 + 1,
                    len(updated) // 4096 if i + 4096 == len(updated) else 0,
                    updated[i : i + 4096],
                )
                for i in range(0, len(updated), 4096)
            ]
            Path(str(message) + "-wal").write_bytes(wal(frames))
            runtime = ReaderRuntime(
                root / "plugin", storage, "a", live_check=lambda force=False: True
            )
            runtime.credentials.save("a", RAW_KEY)

            class Source:
                mode = "builtin"

                async def get(self, path, **params):
                    return runtime.dispatch("get", {"path": path, **params})

                async def close(self):
                    runtime.close()

            store = NoticeStore(root / "notices.db")
            collector = Collector(store, source=Source(), clock=lambda: 1000)
            try:
                await collector.poll_once()
                self.assertEqual(store.count(), 2)
                self.assertEqual(store.query(0, 1000)[0]["group_name"], "班级群")
                records = store.query(0, 1000)
                store.mark_reported("test-private", records, 1000)
                await collector.poll_once()
                self.assertEqual(store.count(unreported_to="test-private"), 0)
                store.set_blocked("g@chatroom", "班级群", True)
                await collector.poll_once()
                self.assertEqual(store.count(), 0)
            finally:
                await collector.close()
                store.close()

    async def test_source_switch_keeps_receipts_and_blocked_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            store = NoticeStore(Path(directory) / "n.db")
            try:
                store.remember_groups([{"username": "g@chatroom", "name": "群"}])
                store.set_state("g@chatroom", {"active": True, "offset": 100, "cursor": 900})
                store.save_notices(
                    "g@chatroom",
                    "群",
                    [{"id": "old", "createTime": 900, "content": "旧通知", "context": []}],
                )
                store.mark_reported("test-private", store.query(0, 1000), 1000)
                store.set_blocked("g@chatroom", "群", True)
                cutoff = store.switch_source("builtin", 1100)
                self.assertEqual(cutoff, 1100)
                self.assertEqual(store.get_state("g@chatroom"), {})
                self.assertIn("g@chatroom", store.blocked_groups())
                self.assertEqual(store.switch_source("builtin", 1200), 1100)
                store.set_blocked("g@chatroom", "群", False)
                self.assertEqual(store.count(unreported_to="test-private"), 0)
            finally:
                store.close()

    async def test_unchanged_snapshot_not_redecrypted_and_unknown_schema_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "a/db_storage"
            storage.mkdir(parents=True)
            database = storage / "test.db"
            database.write_bytes(encrypt(sqlite_bytes()))
            runtime = ReaderRuntime(
                root / "plugin", storage, "a", live_check=lambda force=False: True
            )
            runtime.key = RAW_KEY
            try:
                with patch(
                    "astrbot_plugin_wechat_notice_digest.builtin.worker.decrypt_snapshot",
                    wraps=__import__(
                        "astrbot_plugin_wechat_notice_digest.builtin.snapshot",
                        fromlist=["decrypt_snapshot"],
                    ).decrypt_snapshot,
                ) as decrypt:
                    first = runtime.connection(database)
                    self.assertIs(first, runtime.connection(database))
                    self.assertEqual(decrypt.call_count, 1)
                runtime.credentials.save("a", RAW_KEY)
                with self.assertRaises(ValueError):
                    runtime.dispatch("get", {"path": "/api/chat/sessions", "account": "other"})
            finally:
                runtime.close()
