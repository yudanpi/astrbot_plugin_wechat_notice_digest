import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from test_collector import message

from collector import Collector, NoticeStore


class PollingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NoticeStore(Path(self.tmp.name) / "n.db")
        self.rows = {"g@chatroom": [message(i, 800 + i, all_at=True) for i in range(8, 0, -1)]}
        self.fail = False
        self.fallback = False
        self.requests = []
        self.extra = []
        app = web.Application()
        app.router.add_get("/api/chat/accounts", self.accounts)
        app.router.add_get("/api/chat/sessions", self.sessions)
        app.router.add_get("/api/chat/messages", self.messages)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.collector = Collector(
            self.store,
            f"http://127.0.0.1:{port}",
            page_size=3,
            max_pages=1,
            clock=lambda: 1000,
        )

    async def asyncTearDown(self):
        await self.collector.close()
        self.store.close()
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def accounts(self, request):
        return web.json_response(
            {"status": "success", "default_account": "self", "accounts": ["self"]}
        )

    async def sessions(self, request):
        return web.json_response(
            {
                "status": "success",
                "account": "self",
                "source": "realtime",
                "sessions": [{"username": gid, "name": gid, "isGroup": True} for gid in self.rows]
                + [{"username": "private", "isGroup": False}]
                + self.extra,
            }
        )

    async def messages(self, request):
        gid = request.query["username"]
        self.requests.append(gid)
        if self.fail and gid == "g@chatroom":
            raise web.HTTPServiceUnavailable()
        offset, limit = int(request.query["offset"]), int(request.query["limit"])
        rows = self.rows[gid]
        return web.json_response(
            {
                "status": "success",
                "account": "self",
                "source": "decrypted" if self.fallback else "realtime",
                "messages": rows[offset : offset + limit],
                "hasMore": offset + limit < len(rows),
            }
        )

    async def test_resume_restart_and_dedup(self):
        await self.collector.poll_once()
        self.assertEqual(self.store.get_state("g@chatroom").get("cursor", 0), 0)
        self.assertEqual(self.collector.status()["backlog"], 1)
        await self.collector.close()
        self.collector = Collector(
            self.store,
            self.collector.base_url,
            page_size=3,
            max_pages=2,
            clock=lambda: 1000,
        )
        await self.collector.poll_once()
        self.assertEqual(self.store.count(), 8)
        self.assertEqual(self.store.get_state("g@chatroom")["cursor"], 1000)
        for _ in range(3):
            await self.collector.poll_once()
        self.assertEqual(self.store.count(), 8)
        self.assertNotIn("private", self.requests)

    async def test_single_group_failure_does_not_advance_or_block_others(self):
        self.rows["ok@chatroom"] = [message(99, 850, all_at=True)]
        self.fail = True
        await self.collector.poll_once()
        self.assertEqual(self.store.get_state("g@chatroom").get("offset", 0), 0)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.collector.status()["failed_groups"], 1)

    async def test_fallback_not_claimed_as_realtime(self):
        self.fallback = True
        await self.collector.poll_once()
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.get_state("g@chatroom").get("cursor", 0), 0)
        self.assertEqual(self.collector.status()["failed_groups"], 1)

    async def test_unavailable_service_retains_previous_notices(self):
        self.collector.max_pages = 5
        await self.collector.poll_once()
        await self.runner.cleanup()
        result = await self.collector.poll_once()
        self.assertFalse(result)
        self.assertEqual(self.store.count(), 8)
        self.assertFalse(self.collector.status()["connected"])

    async def test_new_messages_during_paginated_scan_are_not_lost(self):
        await self.collector.poll_once()
        self.rows["g@chatroom"].insert(0, message(9, 850, all_at=True))
        for _ in range(8):
            await self.collector.poll_once()
        self.assertEqual(self.store.count(), 9)

    async def test_blocked_group_is_not_fetched_and_catalog_keeps_it(self):
        self.store.set_blocked("g@chatroom", "g", True)
        await self.collector.poll_once()
        self.assertNotIn("g@chatroom", self.requests)
        self.assertIn("g@chatroom", self.store.groups())
        self.assertEqual(self.collector.status()["blocked_groups"], 1)

    async def test_group_list_limit_reports_incomplete_coverage(self):
        self.collector.session_limit = 2
        await self.collector.poll_once()
        self.assertTrue(self.collector.status()["coverage_incomplete"])

    async def test_retention_cleanup_even_when_service_is_offline(self):
        from collector import extract_notices

        self.store.save_notices(
            "g@chatroom", "g", extract_notices([message(1, 800, all_at=True)], 1000)
        )
        self.collector.clock = lambda: 40 * 86400
        await self.runner.cleanup()
        await self.collector.poll_once()
        self.assertEqual(self.store.count(), 0)


if __name__ == "__main__":
    unittest.main()
