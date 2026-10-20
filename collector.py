"""Read-only collection of structured WeChat group announcements."""

import asyncio
import json
import sqlite3
import time
from pathlib import Path

try:
    from .sources import HttpNoticeSource
except ImportError:
    from sources import HttpNoticeSource


def normalize_message(row):
    """Keep the fields needed for recognition and contextual summaries."""
    try:
        stamp = int(row.get("createTime", 0))
    except (TypeError, ValueError):
        return None
    mid = str(row.get("id") or "")
    if not mid or stamp <= 0:
        return None
    return {
        "id": mid,
        "createTime": stamp,
        "content": str(row.get("content") or row.get("title") or "[非文本消息]"),
        "senderUsername": str(row.get("senderUsername") or ""),
        "senderDisplayName": str(row.get("senderDisplayName") or ""),
        "atUsernames": row.get("atUsernames") if isinstance(row.get("atUsernames"), list) else [],
        "renderType": str(row.get("renderType") or ""),
    }


def extract_notices(messages, now):
    """Select mature structured mentions and bounded same-sender context."""
    unique = {}
    for row in messages:
        normalized = normalize_message(row)
        if normalized:
            unique[normalized["id"]] = normalized
    rows = sorted(unique.values(), key=lambda row: (row["createTime"], row["id"]))
    result = []
    for index, row in enumerate(rows):
        if "notify@all" not in row["atUsernames"] or row["createTime"] > now - 60:
            continue
        before, after = [], []
        if row["senderUsername"]:
            for candidate in rows[:index]:
                if (
                    candidate["senderUsername"] == row["senderUsername"]
                    and row["createTime"] - candidate["createTime"] <= 300
                ):
                    before.append(candidate)
            for candidate in rows[index + 1 :]:
                if (
                    candidate["senderUsername"] == row["senderUsername"]
                    and candidate["createTime"] - row["createTime"] <= 300
                ):
                    after.append(candidate)
        result.append({**row, "context": before[-3:] + after[:3]})
    return result


class NoticeStore:
    """Persist notices and resumable per-group scans in a dedicated database."""

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS notices (
                number INTEGER PRIMARY KEY, group_id TEXT NOT NULL,
                group_name TEXT NOT NULL, message_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL, payload TEXT NOT NULL,
                UNIQUE(group_id, message_id));
            CREATE INDEX IF NOT EXISTS notices_time ON notices(timestamp);
            CREATE TABLE IF NOT EXISTS states (group_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS group_catalog (group_id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS blocked_groups (group_id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS report_receipts (
                target TEXT NOT NULL, group_id TEXT NOT NULL, message_id TEXT NOT NULL,
                reported_at INTEGER NOT NULL, PRIMARY KEY(target,group_id,message_id));
            CREATE TABLE IF NOT EXISTS staging (
                group_id TEXT NOT NULL, message_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(group_id, message_id));
        """)

    def save_notices(self, group_id, group_name, notices):
        with self.db:
            for notice in notices:
                self.db.execute(
                    """INSERT INTO notices
                    (group_id,group_name,message_id,timestamp,payload) VALUES (?,?,?,?,?)
                    ON CONFLICT(group_id,message_id) DO UPDATE SET
                    group_name=excluded.group_name, payload=excluded.payload""",
                    (
                        group_id,
                        group_name,
                        notice["id"],
                        notice["createTime"],
                        json.dumps(notice, ensure_ascii=False),
                    ),
                )

    def switch_source(self, mode, now):
        """Keep business data; discard transport-specific pagination on a switch."""
        state = self.get_state("__source")
        previous = state.get("mode", "external")
        if previous != mode:
            with self.db:
                self.db.execute("DELETE FROM staging")
                self.db.execute(
                    "DELETE FROM states WHERE group_id IN (SELECT group_id FROM group_catalog)"
                )
            state = {"mode": mode, "minimum_timestamp": now}
        elif not state:
            state = {"mode": mode, "minimum_timestamp": 0}
        self.set_state("__source", state)
        return int(state.get("minimum_timestamp", 0))

    def query(self, start, end, limit=500, *, unreported_to=None):
        rows = self.db.execute(
            """SELECT number,group_id,group_name,payload FROM notices
            WHERE timestamp>=? AND timestamp<=?
            AND group_id NOT IN (SELECT group_id FROM blocked_groups)
            AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM report_receipts AS r
                WHERE r.target=? AND r.group_id=notices.group_id AND r.message_id=notices.message_id))
            ORDER BY timestamp DESC,number DESC LIMIT ?""",
            (start, end, unreported_to, unreported_to, limit),
        ).fetchall()
        return [
            {
                **json.loads(payload),
                "number": number,
                "group_id": gid,
                "group_name": name,
            }
            for number, gid, name, payload in rows
        ]

    def count(self, start=0, end=2**62, *, unreported_to=None):
        return self.db.execute(
            "SELECT COUNT(*) FROM notices WHERE timestamp>=? AND timestamp<=? "
            "AND group_id NOT IN (SELECT group_id FROM blocked_groups) "
            "AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM report_receipts AS r "
            "WHERE r.target=? AND r.group_id=notices.group_id AND r.message_id=notices.message_id))",
            (start, end, unreported_to, unreported_to),
        ).fetchone()[0]

    def mark_reported(self, target, rows, now):
        """Record delivery by stable message identity, never by a timestamp alone."""
        with self.db:
            for row in rows:
                self.db.execute(
                    "INSERT OR IGNORE INTO report_receipts VALUES (?,?,?,?)",
                    (target, row["group_id"], row["id"], now),
                )

    def blocked_groups(self):
        """Return persistent exclusions keyed by stable group identifier."""
        return dict(self.db.execute("SELECT group_id,name FROM blocked_groups"))

    def groups(self):
        """Return known group names without fetching private messages."""
        groups = dict(self.db.execute("SELECT group_id,group_name FROM notices GROUP BY group_id"))
        groups.update(self.blocked_groups())
        groups.update(dict(self.db.execute("SELECT group_id,name FROM group_catalog")))
        return groups

    def remember_groups(self, groups):
        with self.db:
            for group in groups:
                self.db.execute(
                    "INSERT OR REPLACE INTO group_catalog VALUES (?,?)",
                    (group["username"], str(group.get("name") or group["username"])),
                )

    def set_blocked(self, group_id, name, blocked):
        """Hide records immediately; retain data and reset partial scans on restore."""
        with self.db:
            if blocked:
                self.db.execute(
                    "INSERT OR REPLACE INTO blocked_groups VALUES (?,?)", (group_id, name)
                )
            else:
                self.db.execute("DELETE FROM blocked_groups WHERE group_id=?", (group_id,))
        if not blocked:
            state = self.get_state(group_id)
            self.set_state(group_id, {"cursor": state.get("cursor", 0), "active": False})

    def get_state(self, group_id):
        row = self.db.execute("SELECT payload FROM states WHERE group_id=?", (group_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_state(self, group_id, state):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO states VALUES (?,?)",
                (group_id, json.dumps(state)),
            )

    def stage(self, group_id, messages):
        with self.db:
            for message in messages:
                row = normalize_message(message)
                if row:
                    self.db.execute(
                        "INSERT OR REPLACE INTO staging VALUES (?,?,?,?)",
                        (group_id, row["id"], row["createTime"], json.dumps(row)),
                    )

    def staged(self, group_id):
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT payload FROM staging WHERE group_id=? ORDER BY timestamp",
                (group_id,),
            )
        ]

    def trim_stage(self, group_id, cutoff):
        with self.db:
            self.db.execute(
                "DELETE FROM staging WHERE group_id=? AND timestamp<?",
                (group_id, cutoff),
            )

    def cleanup(self, cutoff):
        with self.db:
            self.db.execute("DELETE FROM notices WHERE timestamp<?", (cutoff,))
            self.db.execute("DELETE FROM staging WHERE timestamp<?", (cutoff,))
            self.db.execute("DELETE FROM report_receipts WHERE reported_at<?", (cutoff,))

    def close(self):
        self.db.close()


class Collector:
    """Poll the local service without advancing a failed or partial scan."""

    def __init__(
        self,
        store,
        base_url="http://127.0.0.1:10392",
        *,
        account="",
        group_ids=None,
        page_size=100,
        max_pages=10,
        session_limit=2000,
        retention_days=30,
        clock=time.time,
        source=None,
        minimum_timestamp=0,
    ):
        self.source = source if source is not None else HttpNoticeSource(base_url)
        self.store, self.base_url = store, getattr(self.source, "base_url", "")
        self.account = account.strip()
        self.group_ids = set(group_ids or [])
        self.page_size = max(1, min(int(page_size), 500))
        self.max_pages = max(1, min(int(max_pages), 100))
        self.session_limit = max(1, min(int(session_limit), 2000))
        self.retention_days = max(1, min(int(retention_days), 30))
        self.clock = clock
        self.minimum_timestamp = int(minimum_timestamp)
        self.lock = asyncio.Lock()
        self.info = {
            "connected": False,
            "last_scan": 0,
            "last_success": 0,
            "groups": 0,
            "failed_groups": 0,
            "backlog": 0,
            "coverage_incomplete": False,
            "error": "尚未采集",
        }

    async def _get(self, path, **params):
        return await self.source.get(path, **params)

    def _check_source(self, data):
        if data.get("source") != "realtime" or data.get("sourceFallback"):
            raise RuntimeError("实时数据源不可用，已暂停推进游标")
        if data.get("account") != self.account:
            raise RuntimeError("本地接口账号与绑定账号不一致")

    async def poll_once(self):
        async with self.lock:
            now = int(self.clock())
            self.info.update(last_scan=now, failed_groups=0, backlog=0)
            try:
                self.store.cleanup(now - self.retention_days * 86400)
                accounts = await self._get("/api/chat/accounts")
                pinned = self.store.get_state("__account").get("account")
                selected = self.account or pinned or accounts.get("default_account")
                if not selected or selected not in accounts.get("accounts", []):
                    raise RuntimeError("绑定的微信账号不可用")
                if pinned and pinned != selected:
                    raise RuntimeError("账号变更需使用独立数据目录，避免混合记录")
                self.account = selected
                self.store.set_state("__account", {"account": selected})
                data = await self._get(
                    "/api/chat/sessions",
                    account=selected,
                    source="realtime",
                    limit=self.session_limit,
                    include_hidden="true",
                    preview="none",
                )
                self._check_source(data)
                sessions = data.get("sessions")
                if not isinstance(sessions, list):
                    raise RuntimeError("会话列表格式异常")
                self.info["coverage_incomplete"] = len(sessions) >= self.session_limit
                groups = [
                    s
                    for s in sessions
                    if s.get("isGroup") is True
                    and str(s.get("username", "")).endswith("@chatroom")
                    and (not self.group_ids or s["username"] in self.group_ids)
                ]
                self.store.remember_groups(groups)
                self.info["groups"] = len(groups)
                if self.group_ids - {g["username"] for g in groups}:
                    self.info["coverage_incomplete"] = True
                self.info.update(connected=True, error="")
                for group in groups:
                    if group["username"] in self.store.blocked_groups():
                        continue
                    try:
                        await self._poll_group(group, now)
                    except Exception as error:
                        self.info["failed_groups"] += 1
                        self.info["error"] = type(error).__name__ + "：该群采集未完成，游标已保留"
                if (
                    not self.info["failed_groups"]
                    and not self.info["backlog"]
                    and not self.info["coverage_incomplete"]
                ):
                    self.info["last_success"] = now
                return True
            except Exception as error:
                self.info.update(
                    connected=False,
                    error=type(error).__name__ + "：本地数据服务不可用或账号不匹配",
                )
                return False

    async def _poll_group(self, group, now):
        gid = group["username"]
        state = self.store.get_state(gid)
        cursor = int(state.get("cursor", 0))
        if not state.get("active"):
            state = {
                "cursor": cursor,
                "active": True,
                "offset": 0,
                "started": now,
                "floor": max(
                    now - self.retention_days * 86400,
                    cursor - 600 if cursor else now - 86400,
                ),
                "cutoff": max(
                    now - self.retention_days * 86400,
                    cursor - 900 if cursor else now - 86700,
                ),
            }
        for _ in range(self.max_pages):
            page = await self._get(
                "/api/chat/messages",
                account=self.account,
                username=gid,
                source="realtime",
                order="desc",
                limit=self.page_size,
                offset=state["offset"],
            )
            if gid in self.store.blocked_groups():
                return
            self._check_source(page)
            rows = page.get("messages")
            if not isinstance(rows, list) or "hasMore" not in page:
                raise RuntimeError("消息分页格式异常")
            normalized = [normalize_message(row) for row in rows]
            if any(row is None for row in normalized):
                raise RuntimeError("消息缺少 ID 或时间，不能安全推进游标")
            if rows and any(
                normalized[i]["createTime"] < normalized[i + 1]["createTime"]
                for i in range(len(normalized) - 1)
            ):
                raise RuntimeError("消息顺序异常")
            if not rows and page["hasMore"]:
                raise RuntimeError("分页无进展")
            self.store.stage(gid, [r for r in rows if r["createTime"] >= state["cutoff"]])
            finished = not page["hasMore"] or bool(
                normalized and normalized[-1]["createTime"] < state["cutoff"]
            )
            if finished:
                notices = [
                    n
                    for n in extract_notices(self.store.staged(gid), now)
                    if n["createTime"] >= max(state["floor"], self.minimum_timestamp)
                ]
                self.store.save_notices(gid, str(group.get("name") or gid), notices)
                self.store.set_state(gid, {"cursor": state["started"], "active": False})
                self.store.trim_stage(gid, now - 900)
                return
            state["offset"] += len(rows)
            self.store.set_state(gid, state)
        self.info["backlog"] += 1

    def status(self):
        return {
            **self.info,
            "notices": self.store.count(),
            "blocked_groups": len(self.store.blocked_groups()),
        }

    async def close(self):
        await self.source.close()
