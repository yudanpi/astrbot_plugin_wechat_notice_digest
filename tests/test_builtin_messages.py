import hashlib
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_wechat_notice_digest.builtin.messages import list_groups, normalize_record, read_messages


class MessageTests(unittest.TestCase):
    def test_contacts_and_safe_identifiers(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE contact(username, nick_name, remark)")
        connection.executemany("INSERT INTO contact VALUES(?,?,?)", [("g@chatroom", "班级", "群备注"), ("friend", "好友", "")])
        self.assertEqual(list_groups(connection), [{"username": "g@chatroom", "name": "群备注", "isGroup": True}])
        with self.assertRaises(ValueError):
            read_messages(connection, "x'; DROP TABLE contact;--", 10, 0)
        connection.close()

    def test_structural_mentions_sender_and_stable_ids(self):
        row = {"local_id": 1, "server_id": 123, "create_time": 100, "message_content": "wxid_a:\n交表", "sender_username": "wxid_a", "source": "<msgsource><atuserlist><![CDATA[notify@all]]></atuserlist></msgsource>"}
        message = normalize_record(row, account="a", shard="message_0", table="table")
        self.assertEqual(message["id"], "123")
        self.assertEqual(message["atUsernames"], ["notify@all"])
        self.assertEqual(message["content"], "交表")
        row.update(source="", message_content="@全体成员 <atuserlist>notify@all</atuserlist>", server_id=0)
        first = normalize_record(row, account="a", shard="message_0", table="table")
        second = normalize_record(row, account="a", shard="message_0", table="table")
        self.assertEqual(first["atUsernames"], [])
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["id"], normalize_record(row, account="b", shard="message_0", table="table")["id"])

    def test_compressed_body_and_schema_validation(self):
        import zstandard
        row = {"local_id": 1, "create_time": 100, "message_content": zstandard.ZstdCompressor().compress("你好".encode()), "source": ""}
        self.assertEqual(normalize_record(row, account="a", shard="s", table="t")["content"], "你好")
        connection = sqlite3.connect(":memory:")
        with self.assertRaises(ValueError):
            list_groups(connection)
        connection.close()

    def test_equal_timestamps_and_offset(self):
        con = sqlite3.connect(":memory:")
        name = "Msg_" + hashlib.md5(b"g@chatroom").hexdigest()
        con.execute("CREATE TABLE Name2Id(user_name)")
        con.execute("INSERT INTO Name2Id VALUES('sender')")
        con.execute(f'CREATE TABLE "{name}"(local_id, server_id, create_time, message_content, real_sender_id, source)')
        con.executemany(f'INSERT INTO "{name}" VALUES(?,?,?,?,?,?)', [(i, i, 100, str(i), 1, "") for i in range(1, 5)])
        result = read_messages(con, "g@chatroom", 2, 1)
        self.assertEqual([row["local_id"] for row in result["messages"]], [3, 2])
        self.assertTrue(result["hasMore"])
        con.close()
