import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_wechat_notice_digest.collector import NoticeStore
from astrbot_plugin_wechat_notice_digest.main import WeChatNoticeDigest


class Event:
    unified_msg_origin = "weixin_oc:FriendMessage:admin"

    def __init__(self, admin=True, private=True):
        self.admin, self.private, self.stopped = admin, private, False

    def is_admin(self):
        return self.admin

    def is_private_chat(self):
        return self.private

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


async def outputs(generator):
    return [text async for text in generator]


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.context = SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="model"),
            llm_generate=AsyncMock(
                return_value=SimpleNamespace(
                    completion_text='{"items":[{"sources":["N1"],"action":"提交材料","deadline":"未注明"}]}'
                )
            ),
        )
        with patch(
            "astrbot_plugin_wechat_notice_digest.main.StarTools.get_data_dir",
            return_value=Path(self.tmp.name),
        ):
            self.plugin = WeChatNoticeDigest(self.context, {})
        self.plugin.store = NoticeStore(Path(self.tmp.name) / "test.db")
        self.plugin.collector = SimpleNamespace(
            status=lambda: {
                "connected": True,
                "last_success": int(time.time()),
                "last_scan": int(time.time()),
                "groups": 2,
                "failed_groups": 0,
                "backlog": 0,
                "coverage_incomplete": False,
                "notices": 1,
                "error": "",
            },
            close=AsyncMock(),
        )
        self.plugin.store.save_notices(
            "g@chatroom",
            "学习群",
            [
                {
                    "id": "1",
                    "createTime": int(time.time()) - 100,
                    "content": "提交材料",
                    "senderUsername": "a",
                    "context": [],
                }
            ],
        )

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.tmp.cleanup()

    async def test_permission_and_private_only(self):
        for event in [Event(private=False), Event(admin=False, private=False)]:
            for handler in [
                self.plugin.digest,
                self.plugin.originals,
                self.plugin.status_command,
            ]:
                text = "".join(await outputs(handler(event)))
                self.assertIn("仅限私聊", text)
                self.assertNotIn("提交材料", text)
        self.context.llm_generate.assert_not_awaited()

    async def test_non_admin_can_use_all_three_commands_in_private(self):
        for handler in [self.plugin.digest, self.plugin.originals, self.plugin.status_command]:
            result = "".join(await outputs(handler(Event(admin=False))))
            self.assertNotIn("仅限管理员", result)
            self.assertNotIn("仅限私聊", result)
        self.context.llm_generate.assert_awaited_once()

    async def test_days_validation(self):
        for days in ["0", "31", "abc"]:
            result = await outputs(self.plugin.digest(Event(), days))
            self.assertIn("1–30", "".join(result))
        self.context.llm_generate.assert_not_awaited()

    async def test_block_commands_hide_old_notice_and_unblock_restores(self):
        result = "".join(await outputs(self.plugin.block_group(Event(admin=False), "学习群")))
        self.assertIn("已屏蔽", result)
        self.assertEqual(self.plugin.store.count(), 0)
        result = "".join(await outputs(self.plugin.originals(Event())))
        self.assertNotIn("提交材料", result)
        result = "".join(await outputs(self.plugin.block_list(Event())))
        self.assertIn("学习群", result)
        await outputs(self.plugin.unblock_group(Event(), "学习群"))
        self.assertEqual(self.plugin.store.count(), 1)

    async def test_duplicate_group_names_require_identifier(self):
        self.plugin.store.remember_groups(
            [
                {"username": "a@chatroom", "name": "同名 群"},
                {"username": "b@chatroom", "name": "同名 群"},
            ]
        )
        result = "".join(await outputs(self.plugin.block_group(Event(), "同名 群")))
        self.assertIn("同名", result)
        self.assertEqual(self.plugin.store.blocked_groups(), {})
        await outputs(self.plugin.block_group(Event(), "a@chatroom"))
        self.assertEqual(list(self.plugin.store.blocked_groups()), ["a@chatroom"])
        result = "".join(await outputs(self.plugin.block_group(Event(private=False), "b@chatroom")))
        self.assertIn("仅限私聊", result)
        self.assertNotIn("b@chatroom", self.plugin.store.blocked_groups())

    async def test_block_while_model_is_running_hides_result(self):
        async def respond(**kwargs):
            self.plugin.store.set_blocked("g@chatroom", "学习群", True)
            return SimpleNamespace(
                completion_text='{"items":[{"sources":["N1"],"action":"提交材料","deadline":"未注明"}]}'
            )

        self.context.llm_generate.side_effect = respond
        text = "".join(await outputs(self.plugin.digest(Event())))
        self.assertNotIn("提交材料", text)
        self.assertIn("已屏蔽", text)

    async def test_block_before_model_request_does_not_send_blocked_data(self):
        async def provider(umo):
            self.plugin.store.set_blocked("g@chatroom", "学习群", True)
            return "model"

        self.context.get_current_chat_provider_id.side_effect = provider
        text = "".join(await outputs(self.plugin.digest(Event())))
        self.context.llm_generate.assert_not_awaited()
        self.assertNotIn("提交材料", text)

    async def test_original_parts_stop_after_block_settings_change(self):
        self.plugin.store.save_notices(
            "g@chatroom",
            "学习群",
            [
                {
                    "id": "long",
                    "createTime": int(time.time()) - 80,
                    "content": "long private notification " * 120,
                    "context": [],
                }
            ],
        )
        generator = self.plugin.originals(Event())
        first = await anext(generator)
        self.assertTrue(first)
        self.plugin.store.set_blocked("g@chatroom", "学习群", True)
        remaining = "".join(await outputs(generator))
        self.assertNotIn("private notification", remaining)
        self.assertNotIn("提交材料", remaining)

    async def test_model_failure_falls_back_to_originals(self):
        self.context.llm_generate.side_effect = RuntimeError("secret detail")
        result = "".join(await outputs(self.plugin.digest(Event())))
        self.assertIn("总结未完成", result)
        self.assertIn("提交材料", result)
        self.assertNotIn("secret detail", result)

    async def test_missing_provider_falls_back(self):
        self.context.get_current_chat_provider_id.return_value = ""
        result = "".join(await outputs(self.plugin.digest(Event())))
        self.assertIn("总结未完成", result)
        self.assertIn("提交材料", result)

    async def test_prompt_grounded_and_no_tools(self):
        result = "".join(await outputs(self.plugin.digest(Event())))
        kwargs = self.context.llm_generate.await_args.kwargs
        self.assertIn("学习群", kwargs["prompt"])
        self.assertIn("截止", kwargs["system_prompt"])
        self.assertIn("指令", kwargs["system_prompt"])
        self.assertNotIn("tools", kwargs)
        self.assertIn("【学习群】", result)
        self.assertIn("要做：提交材料", result)
        self.assertIn("截止：未注明", result)
        self.assertNotIn("来源索引", result)

    async def test_character_limits_and_visible_omissions(self):
        self.plugin.max_notices = 1
        self.plugin.store.save_notices(
            "g@chatroom",
            "学习群",
            [
                {
                    "id": "2",
                    "createTime": int(time.time()) - 90,
                    "content": "X" * 30000,
                    "senderUsername": "a",
                    "context": [],
                }
            ],
        )
        self.context.llm_generate.return_value = SimpleNamespace(completion_text="Y" * 10000)
        result = await outputs(self.plugin.digest(Event()))
        self.assertTrue(all(len(part) <= 1800 for part in result))
        prompt = self.context.llm_generate.await_args.kwargs["prompt"]
        self.assertLessEqual(len(prompt), 12000)
        self.assertIn("未纳入", "".join(result))
        self.assertIn("截断", "".join(result))

    async def test_shutdown_cancels_task(self):
        self.plugin.task = asyncio.create_task(asyncio.sleep(1000))
        await asyncio.sleep(0)
        task = self.plugin.task
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.plugin.collector.close.assert_awaited()

    async def test_originals_excludes_expired_notices_without_a_successful_poll(self):
        self.plugin.store.save_notices(
            "old@chatroom",
            "old",
            [
                {
                    "id": "old",
                    "createTime": int(time.time()) - 31 * 86400,
                    "content": "expired secret",
                    "senderUsername": "a",
                    "context": [],
                }
            ],
        )
        result = "".join(await outputs(self.plugin.originals(Event())))
        self.assertNotIn("expired secret", result)

    async def test_exact_cross_group_duplicates_are_grouped_before_batching(self):
        self.plugin.store.save_notices(
            "other@chatroom",
            "其他群",
            [
                {
                    "id": "other",
                    "createTime": int(time.time()) - 100,
                    "content": "提交材料",
                    "senderUsername": "b",
                    "context": [],
                }
            ],
        )
        await outputs(self.plugin.digest(Event()))
        prompt = self.context.llm_generate.await_args.kwargs["prompt"]
        self.assertEqual(prompt.count("提交材料"), 1)
        self.assertIn("N1", prompt)
        self.assertIn("N2", prompt)

    async def test_fallback_has_own_ten_record_query_when_model_cap_is_one(self):
        self.plugin.max_notices = 1
        for i in range(11):
            self.plugin.store.save_notices(
                "g@chatroom",
                "学习群",
                [
                    {
                        "id": f"extra{i}",
                        "createTime": int(time.time()) - 80 + i,
                        "content": f"材料{i}",
                        "context": [],
                    }
                ],
            )
        self.context.llm_generate.side_effect = RuntimeError("unavailable")
        result = "".join(await outputs(self.plugin.digest(Event())))
        self.assertEqual(result.count("[N"), 10)
        self.assertIn("另有 2 条", result)

    async def test_scheduled_report_uses_pending_rows_and_tracks_sources(self):
        self.plugin.reporter = SimpleNamespace(target=Event.unified_msg_origin)
        rows = self.plugin.store.query(0, int(time.time()))
        report = await self.plugin._build_daily_report(rows, len(rows))
        self.assertIn("每日早报", "".join(report["parts"]))
        self.assertEqual(report["rows"], rows)

    async def test_scheduled_report_waits_for_complete_collection(self):
        self.plugin.reporter = SimpleNamespace(target=Event.unified_msg_origin)
        self.plugin.collector.status = lambda: {"connected": False}
        rows = self.plugin.store.query(0, int(time.time()))
        self.assertIsNone(await self.plugin._build_daily_report(rows, len(rows)))
        self.context.llm_generate.assert_not_awaited()
