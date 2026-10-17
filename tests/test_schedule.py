import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from test_collector import message

from collector import NoticeStore, extract_notices
from schedule import DailyReporter, next_daily_run

CN = timezone(timedelta(hours=8))


def stamp(day, hour, minute=0):
    return int(datetime(2026, 10, day, hour, minute, tzinfo=CN).timestamp())


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NoticeStore(Path(self.tmp.name) / "schedule.db")
        self.now = stamp(17, 6, 59)
        self.target = "bot:FriendMessage:test-user"
        self.sender = AsyncMock(return_value=True)
        self.builder = AsyncMock(
            side_effect=lambda rows, total: {"parts": ["早报第一段", "早报第二段"], "rows": rows}
        )
        self.config = {
            "daily_report_enabled": True,
            "daily_report_time": "07:00",
            "daily_report_target": self.target,
        }
        self.reporter = DailyReporter(
            self.store, self.config, self.builder, self.sender, clock=lambda: self.now
        )

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, mid="1", group="g@chatroom"):
        row = extract_notices([message(mid, self.now - 120, all_at=True)], self.now)[0]
        self.store.save_notices(group, group, [row])

    async def test_seven_am_in_beijing_only_and_restart_dedup(self):
        self.add()
        await self.reporter.tick()
        self.sender.assert_not_awaited()
        self.now = stamp(17, 7)
        await self.reporter.tick()
        self.assertEqual(self.sender.await_count, 2)
        self.assertEqual(self.store.count(0, self.now, unreported_to=self.target), 0)
        reporter = DailyReporter(
            self.store, self.config, self.builder, self.sender, clock=lambda: self.now
        )
        await reporter.tick()
        self.assertEqual(self.sender.await_count, 2)
        self.assertEqual(reporter.status()["next_due"], stamp(18, 7))

    async def test_empty_day_is_quiet_and_missed_run_catches_up(self):
        await self.reporter.tick()
        self.now = stamp(17, 8)
        await self.reporter.tick()
        self.sender.assert_not_awaited()
        self.builder.assert_not_awaited()
        self.assertEqual(self.reporter.status()["next_due"], stamp(18, 7))

    async def test_send_failure_resumes_pending_parts_after_restart(self):
        self.add()
        await self.reporter.tick()
        self.now = stamp(17, 7)
        self.sender.side_effect = [True, RuntimeError("expired token")]
        await self.reporter.tick()
        self.assertEqual(self.store.count(0, self.now, unreported_to=self.target), 1)
        self.now += 901
        sender = AsyncMock(return_value=True)
        reporter = DailyReporter(
            self.store, self.config, self.builder, sender, clock=lambda: self.now
        )
        await reporter.tick()
        sender.assert_awaited_once_with(self.target, "早报第二段")
        self.assertEqual(self.builder.await_count, 1)
        self.assertEqual(self.store.count(0, self.now, unreported_to=self.target), 0)

    async def test_blocked_records_are_not_reported(self):
        self.add()
        self.store.set_blocked("g@chatroom", "g", True)
        await self.reporter.tick()
        self.now = stamp(17, 7)
        await self.reporter.tick()
        self.sender.assert_not_awaited()

    async def test_disabled_and_group_target_never_send(self):
        self.add()
        for config in [{}, {**self.config, "daily_report_target": "bot:GroupMessage:group"}]:
            reporter = DailyReporter(
                self.store, config, self.builder, self.sender, clock=lambda: self.now
            )
            await reporter.tick()
        self.sender.assert_not_awaited()

    async def test_block_between_parts_cancels_remaining(self):
        self.add()
        await self.reporter.tick()
        self.now = stamp(17, 7)

        async def send(target, text):
            self.store.set_blocked("g@chatroom", "g", True)
            return True

        self.sender.side_effect = send
        await self.reporter.tick()
        self.sender.assert_awaited_once()

    async def test_late_collected_record_uses_identity_not_timestamp_watermark(self):
        self.add()
        await self.reporter.tick()
        self.now = stamp(17, 7)
        await self.reporter.tick()
        self.store.save_notices(
            "g@chatroom",
            "g",
            extract_notices([message("late", stamp(17, 6), all_at=True)], self.now),
        )
        self.now = stamp(18, 7)
        await self.reporter.tick()
        self.assertEqual(self.sender.await_count, 4)
        self.assertEqual(self.store.count(0, self.now, unreported_to=self.target), 0)

    async def test_partial_summary_does_not_mark_unseen_records_delivered(self):
        self.add("1")
        self.add("2")
        self.builder.side_effect = lambda rows, total: {"parts": ["只总结一条"], "rows": rows[:1]}
        await self.reporter.tick()
        self.now = stamp(17, 7)
        await self.reporter.tick()
        self.assertEqual(self.store.count(0, self.now, unreported_to=self.target), 1)

    def test_first_enable_after_seven_starts_tomorrow(self):
        self.assertEqual(next_daily_run(stamp(17, 16), "07:00"), stamp(18, 7))


if __name__ == "__main__":
    unittest.main()
