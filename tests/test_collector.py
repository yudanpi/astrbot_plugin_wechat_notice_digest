import tempfile
import unittest
from pathlib import Path

from collector import NoticeStore, extract_notices


def message(mid, timestamp, text="notice", sender="alice", all_at=False):
    return {
        "id": str(mid),
        "createTime": timestamp,
        "content": text,
        "senderUsername": sender,
        "atUsernames": ["notify@all"] if all_at else [],
    }


class RecognitionTests(unittest.TestCase):
    def test_structured_marker_only_and_maturity(self):
        rows = [
            message(1, 900, "@所有人"),
            message(2, 910, all_at=True),
            message(3, 980, all_at=True),
        ]
        self.assertEqual([n["id"] for n in extract_notices(rows, 1000)], ["2"])

    def test_context_sender_time_and_limits(self):
        rows = [message(i, 600 + i * 10) for i in range(1, 7)]
        rows += [
            message(7, 700, "@所有人", all_at=True),
            message(8, 710, "attachment"),
            message(9, 720, "other", sender="bob"),
            message(10, 1001, "too late"),
        ]
        notice = extract_notices(rows, 1200)[0]
        self.assertEqual([m["id"] for m in notice["context"]], ["4", "5", "6", "8"])

    def test_missing_sender_does_not_merge_unknown_people(self):
        rows = [message(1, 800, sender=""), message(2, 810, sender="", all_at=True)]
        self.assertEqual(extract_notices(rows, 1000)[0]["context"], [])

    def test_missing_id_or_timestamp_is_not_saved(self):
        self.assertEqual(extract_notices([{"atUsernames": ["notify@all"]}], 1000), [])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NoticeStore(Path(self.tmp.name) / "notices.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_dedup_across_groups_and_update_context(self):
        n = extract_notices([message(1, 800, all_at=True)], 1000)[0]
        self.store.save_notices("g1", "one", [n])
        self.store.save_notices("g2", "two", [n])
        n["context"] = [message(2, 810, "extra")]
        self.store.save_notices("g1", "one", [n])
        rows = self.store.query(0, 1000)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            next(r for r in rows if r["group_id"] == "g1")["context"][0]["content"],
            "extra",
        )

    def test_block_hides_old_rows_persists_and_unblock_restores(self):
        n = extract_notices([message(1, 800, all_at=True)], 1000)[0]
        self.store.save_notices("g@chatroom", "测试 群", [n])
        self.store.remember_groups([{"username": "g@chatroom", "name": "测试 群"}])
        self.store.set_blocked("g@chatroom", "测试 群", True)
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.query(0, 1000), [])
        self.store.close()
        self.store = NoticeStore(Path(self.tmp.name) / "notices.db")
        self.assertIn("g@chatroom", self.store.blocked_groups())
        self.store.set_blocked("g@chatroom", "测试 群", False)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.groups()["g@chatroom"], "测试 群")

    def test_state_persists_and_cleanup(self):
        self.store.set_state("g", {"cursor": 123, "offset": 40})
        self.store.close()
        self.store = NoticeStore(Path(self.tmp.name) / "notices.db")
        self.assertEqual(self.store.get_state("g")["offset"], 40)
        n = extract_notices([message(1, 800, all_at=True)], 1000)[0]
        self.store.save_notices("g", "one", [n])
        self.store.cleanup(900)
        self.assertEqual(self.store.query(0, 1000), [])


if __name__ == "__main__":
    unittest.main()
