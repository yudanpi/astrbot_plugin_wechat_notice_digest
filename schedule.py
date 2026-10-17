"""Persistent daily delivery with Beijing time and a resumable text outbox."""

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

CHINA = timezone(timedelta(hours=8))
STATE_KEY = "__daily_report"


def next_daily_run(now, report_time):
    """Return the next future wall-clock occurrence in Asia/Shanghai."""
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", report_time):
        raise ValueError("汇报时间必须是 HH:MM")
    hour, minute = map(int, report_time.split(":"))
    current = datetime.fromtimestamp(now, CHINA)
    due = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due <= current:
        due += timedelta(days=1)
    return int(due.timestamp())


class DailyReporter:
    """Keep delivery progress separate from collection and manual summaries."""

    def __init__(self, store, config, builder, sender, *, clock=time.time):
        self.store, self.builder, self.sender, self.clock = store, builder, sender, clock
        self.enabled = config.get("daily_report_enabled") is True
        self.report_time = str(config.get("daily_report_time", "07:00"))
        self.target = str(config.get("daily_report_target", "")).strip()
        self.limit = max(1, min(int(config.get("max_notices", 40)), 100))
        self.lock = asyncio.Lock()
        self.error = ""
        if self.enabled:
            try:
                next_daily_run(int(clock()), self.report_time)
                fields = self.target.split(":", 2)
                if (
                    len(fields) != 3
                    or fields[1] != "FriendMessage"
                    or not fields[0]
                    or not fields[2]
                ):
                    raise ValueError("请配置私聊 UMO 作为汇报目标")
            except ValueError as error:
                self.error = str(error)
                self.enabled = False

    async def tick(self):
        if not self.enabled or self.lock.locked():
            return
        async with self.lock:
            now = int(self.clock())
            identity = [self.target, self.report_time]
            state = self.store.get_state(STATE_KEY)
            if state.get("identity") != identity:
                due = next_daily_run(now, self.report_time)
                state = {
                    "identity": identity,
                    "next_due": due,
                    "eligible_since": due - 86400,
                    "last_result": "等待首次汇报",
                    "last_sent": 0,
                }
                self.store.set_state(STATE_KEY, state)
                return
            if now < state["next_due"] or now < state.get("retry_after", 0):
                return
            try:
                job = state.get("job")
                if job and (
                    set(job["blocked"]) != set(self.store.blocked_groups())
                    or any(row["createTime"] < now - 30 * 86400 for row in job["rows"])
                ):
                    state.pop("job", None)
                    state.update(
                        last_result="屏蔽设置或保留期变化，已取消待发内容",
                        retry_after=0,
                        next_due=next_daily_run(now, self.report_time),
                    )
                    self.store.set_state(STATE_KEY, state)
                    return
                if not job:
                    start = max(state["eligible_since"], now - 30 * 86400)
                    total = self.store.count(start, now, unreported_to=self.target)
                    if not total:
                        state.update(
                            next_due=next_daily_run(now, self.report_time),
                            retry_after=0,
                            last_result="无新增通知，未发送",
                        )
                        self.store.set_state(STATE_KEY, state)
                        return
                    rows = self.store.query(start, now, self.limit, unreported_to=self.target)
                    report = await self.builder(rows, total)
                    if not report or not report.get("rows") or not report.get("parts"):
                        state.update(last_result="等待采集完成或有效汇总", retry_after=now + 60)
                        self.store.set_state(STATE_KEY, state)
                        return
                    requested = {(r["group_id"], r["id"]) for r in rows}
                    if any((r["group_id"], r["id"]) not in requested for r in report["rows"]):
                        raise ValueError("Report included an unexpected notification")
                    if any(r["group_id"] in self.store.blocked_groups() for r in report["rows"]):
                        state.update(last_result="屏蔽设置变化，稍后重新整理", retry_after=now + 60)
                        self.store.set_state(STATE_KEY, state)
                        return
                    job = {
                        "parts": report["parts"],
                        "next_part": 0,
                        "rows": [
                            {k: r[k] for k in ("group_id", "id", "createTime")}
                            for r in report["rows"]
                        ],
                        "blocked": sorted(self.store.blocked_groups()),
                    }
                    state["job"] = job
                    self.store.set_state(STATE_KEY, state)
                while job["next_part"] < len(job["parts"]):
                    if set(job["blocked"]) != set(self.store.blocked_groups()):
                        state.pop("job", None)
                        state.update(
                            last_result="屏蔽设置已变化，后续发送已停止",
                            retry_after=0,
                            next_due=next_daily_run(now, self.report_time),
                        )
                        self.store.set_state(STATE_KEY, state)
                        return
                    sent = await self.sender(self.target, job["parts"][job["next_part"]])
                    if sent is not True:
                        raise RuntimeError("Private delivery failed")
                    job["next_part"] += 1
                    self.store.set_state(STATE_KEY, state)
                self.store.mark_reported(self.target, job["rows"], now)
                count = len(job["rows"])
                state.pop("job", None)
                state.update(
                    last_sent=now,
                    next_due=next_daily_run(now, self.report_time),
                    retry_after=0,
                    last_result=f"已汇报 {count} 条通知",
                )
                self.store.set_state(STATE_KEY, state)
            except Exception as error:
                state["failures"] = state.get("failures", 0) + 1
                state.update(
                    retry_after=now + 900,
                    last_result=f"发送或生成失败（{type(error).__name__}），15分钟后重试；可先私聊机器人刷新会话",
                )
                self.store.set_state(STATE_KEY, state)

    def status(self):
        state = self.store.get_state(STATE_KEY)
        return {
            "enabled": self.enabled,
            "time": self.report_time,
            "next_due": state.get("next_due", 0),
            "last_sent": state.get("last_sent", 0),
            "result": self.error or state.get("last_result", "尚未初始化"),
            "pending": bool(state.get("job")),
        }


class ScheduledReportEvent:
    """Adapt scheduled generation to the same private digest renderer."""

    scheduled_report = True

    def __init__(self, target, rows, total):
        self.unified_msg_origin = target
        self.report_rows, self.report_total = rows, total
        self.reported_rows = []

    def is_private_chat(self):
        return True

    def stop_event(self):
        pass

    def plain_result(self, text):
        return text
