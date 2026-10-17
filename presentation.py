"""Validate model fields and render concise plain-text WeChat cards."""

import json
import re
import time
from datetime import datetime, timedelta, timezone

CHINA = timezone(timedelta(hours=8))


def deadline_value(value):
    """Parse explicit ISO dates; a date without a time expires at day end."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)?", value
    ):
        raise ValueError("Invalid deadline date")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if len(value) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed.replace(tzinfo=CHINA) if parsed.tzinfo is None else parsed.astimezone(CHINA)


def priority(item, now):
    """Order actionable deadlines before unknown dates and overdue tasks."""
    due = deadline_value(item.get("deadline_iso"))
    urgent = item.get("urgency") == "urgent"
    if due is None:
        return (2 if urgent else 4, 0, 0), "🟠 需尽快处理" if urgent else ""
    current = datetime.fromtimestamp(now, CHINA)
    days = (due.date() - current.date()).days
    if due.timestamp() < now:
        return (5, due.timestamp(), 0), "已过期"
    rank = 0 if days == 0 else 1 if days <= 3 else 3
    label = "🔴 今天截止" if rank == 0 else "🟠 3天内截止" if rank == 1 else ""
    if urgent and rank == 3:
        label = "🟠 需尽快处理"
    return (rank, due.timestamp(), 0 if urgent else 1), label


def parse_items(text):
    """Accept structured items only, rather than forwarding model prose."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        result = json.loads(text)
    except (ValueError, TypeError) as error:
        raise ValueError("模型未返回结构化通知") from error
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list) or not items or len(items) > 200:
        raise ValueError("通知条目格式异常")
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("sources"), list)
            or not item["sources"]
            or not all(isinstance(s, str) for s in item["sources"])
            or not isinstance(item.get("action"), str)
            or not item["action"].strip()
            or not isinstance(item.get("deadline", "未注明"), str)
            or not isinstance(item.get("materials", []), list)
            or not all(isinstance(m, str) and m.strip() for m in item.get("materials", []))
            or item.get("urgency", "normal") not in ("normal", "urgent")
        ):
            raise ValueError("通知字段格式异常")
        deadline_value(item.get("deadline_iso"))
    return items


def render_items(items, rows, blocked=None, *, return_sources=False, now=None):
    """Resolve source names from local records and consolidate identical cards."""
    now = time.time() if now is None else now
    blocked = set(blocked or [])
    sources = {f"N{row['number']}": row for row in rows if row["group_id"] not in blocked}
    excluded = {f"N{row['number']}" for row in rows if row["group_id"] in blocked}
    cards = {}
    covered = set()
    mixed_omitted = False
    for item in items:
        if excluded.intersection(item.get("sources", [])):
            mixed_omitted = mixed_omitted or bool(sources.keys() & set(item.get("sources", [])))
            continue
        references = [s for s in item.get("sources", []) if s in sources]
        if not references:
            continue
        action = " ".join(item["action"].split())
        deadline = " ".join((item.get("deadline") or "未注明").split())
        if len(action) > 160:
            action = action[:160] + "…（查看原文）"
        if len(deadline) > 80:
            deadline = deadline[:80] + "…（查看原文）"
        materials = tuple(dict.fromkeys(" ".join(m.split()) for m in item.get("materials", [])))
        due = item.get("deadline_iso") or ""
        key = (action, deadline, materials, due)
        card = cards.setdefault(key, {"names": [], "item": dict(item)})
        if item.get("urgency") == "urgent":
            card["item"]["urgency"] = "urgent"
        names = card["names"]
        for reference in references:
            covered.add(reference)
            name = " ".join(sources[reference]["group_name"].split())[:120]
            if name not in names:
                names.append(name)
    if not cards:
        if not sources and blocked:
            return (
                ("通知所属群已屏蔽，本次不展示。", [])
                if return_sources
                else "通知所属群已屏蔽，本次不展示。"
            )
        if mixed_omitted:
            return (
                ("屏蔽设置已变更，混合条目未能提炼，请重新发送 /通知汇总。", [])
                if return_sources
                else "屏蔽设置已变更，混合条目未能提炼，请重新发送 /通知汇总。"
            )
        raise ValueError("模型未引用有效通知来源")
    rendered = []
    ordered = sorted(cards.items(), key=lambda entry: priority(entry[1]["item"], now)[0])
    overdue_heading = False
    for index, ((action, deadline, materials, _), card) in enumerate(ordered, 1):
        names = card["names"]
        rank, label = priority(card["item"], now)
        if rank[0] == 5 and not overdue_heading:
            rendered.append("—— 已过期事项（请确认是否仍需补办）——")
            overdue_heading = True
        number = chr(0x245F + index) if index <= 20 else str(index)
        suffix = f" {label}" if label else ""
        text = f"{number}【{'、'.join(names)}】{suffix}\n要做：{action}\n截止：{deadline}"
        if materials:
            text += "\n材料：" + "、".join(materials)
        rendered.append(text)
    missing = len(sources.keys() - covered)
    if missing:
        rendered.append(f"另有 {missing} 条未能提炼，请重试汇总或在微信中查看原文。")
    text = "\n\n".join(rendered)
    return (
        (text, [row for key, row in sources.items() if key in covered]) if return_sources else text
    )
