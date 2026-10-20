import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_wechat_notice_digest.collector import Collector, NoticeStore
from astrbot_plugin_wechat_notice_digest.sources import HttpNoticeSource


class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_injected_source_uses_existing_collector_without_http(self):
        with tempfile.TemporaryDirectory() as directory:
            store = NoticeStore(Path(directory) / "n.db")
            source = AsyncMock()
            source.mode = "builtin"
            source.get.side_effect = [
                {"status": "success", "accounts": ["a"], "default_account": "a"},
                {
                    "status": "success",
                    "account": "a",
                    "source": "realtime",
                    "sessions": [{"username": "g@chatroom", "name": "群", "isGroup": True}],
                },
                {
                    "status": "success",
                    "account": "a",
                    "source": "realtime",
                    "messages": [
                        {
                            "id": "1",
                            "createTime": 900,
                            "content": "交表",
                            "senderUsername": "s",
                            "atUsernames": ["notify@all"],
                        }
                    ],
                    "hasMore": False,
                },
            ]
            collector = Collector(store, source=source, clock=lambda: 1000)
            await collector.poll_once()
            self.assertEqual(store.count(), 1)
            self.assertEqual(collector.status()["failed_groups"], 0)
            await collector.close()
            source.close.assert_awaited_once()
            store.close()

    async def test_http_source_rejects_non_loopback_and_embedded_secrets(self):
        for url in ["https://example.com", "http://u:p@127.0.0.1", "http://127.0.0.1/path"]:
            with self.assertRaises(ValueError):
                HttpNoticeSource(url)
