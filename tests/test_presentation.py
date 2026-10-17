import unittest
from datetime import datetime, timedelta, timezone

from presentation import parse_items, render_items


class PresentationTests(unittest.TestCase):
    def test_groups_are_taken_from_records_and_three_clear_lines(self):
        rows = [{"number": 1, "group_name": "真实班级群", "group_id": "g"}]
        items = parse_items(
            '{"items":[{"sources":["N1"],"group":"伪造群", "action":"完成停保并交截图", "deadline":"10月18日 12:00"}]}'
        )
        text = render_items(items, rows)
        self.assertIn("①【真实班级群】\n要做：完成停保并交截图\n截止：10月18日 12:00", text)
        self.assertNotIn("伪造群", text)
        self.assertNotIn("来源索引", text)

    def test_missing_deadline_and_unknown_sources(self):
        rows = [{"number": 1, "group_name": "群", "group_id": "g"}]
        text = render_items([{"sources": ["N1"], "action": "报名"}], rows)
        self.assertIn("截止：未注明", text)
        with self.assertRaises(ValueError):
            render_items([{"sources": ["N999"], "action": "虚构"}], rows)

    def test_code_fences_and_dedup_across_groups(self):
        items = parse_items(
            '```json\n{"items":[{"sources":["N1"],"action":"报名","deadline":"未注明"},{"sources":["N2"],"action":"报名","deadline":"未注明"}]}\n```'
        )
        rows = [
            {"number": 1, "group_name": "甲群", "group_id": "a"},
            {"number": 2, "group_name": "乙群", "group_id": "b"},
        ]
        text = render_items(items, rows)
        self.assertEqual(text.count("要做："), 1)
        self.assertIn("甲群、乙群", text)
        self.assertNotIn("甲群", render_items(items, rows, {"a"}))

    def test_invalid_response_does_not_pass_through(self):
        for text in [
            "ignore instructions",
            '{"items": []}',
            '{"items":[{"sources":"N1","action":"bad"}]}',
        ]:
            with self.assertRaises(ValueError):
                parse_items(text)

    def test_mixed_item_is_not_relabelled_when_one_source_is_blocked(self):
        rows = [
            {"number": 1, "group_name": "屏蔽群", "group_id": "a"},
            {"number": 2, "group_name": "正常群", "group_id": "b"},
        ]
        text = render_items(
            [{"sources": ["N1", "N2"], "action": "含屏蔽群独有的私密要求", "deadline": "10月20日"}],
            rows,
            {"a"},
        )
        self.assertNotIn("私密要求", text)
        self.assertIn("未能提炼", text)


class PriorityTests(unittest.TestCase):
    now = datetime(2026, 10, 17, 7, tzinfo=timezone(timedelta(hours=8))).timestamp()

    def render(self, items):
        rows = [
            {"number": i, "group_name": f"群{i}", "group_id": str(i)}
            for i in range(1, len(items) + 1)
        ]
        for i, item in enumerate(items, 1):
            item["sources"] = [f"N{i}"]
        return render_items(items, rows, now=self.now)

    def test_deadline_and_urgency_sort_and_expired_last(self):
        text = self.render(
            [
                {"action": "过期事项", "deadline": "10月16日", "deadline_iso": "2026-10-16"},
                {"action": "普通无日期"},
                {"action": "月底事项", "deadline": "10月30日", "deadline_iso": "2026-10-30"},
                {"action": "尽快提交", "urgency": "urgent"},
                {"action": "明天事项", "deadline": "10月18日", "deadline_iso": "2026-10-18"},
                {
                    "action": "今天下午",
                    "deadline": "10月17日17:00",
                    "deadline_iso": "2026-10-17T17:00:00+08:00",
                },
                {
                    "action": "今天中午",
                    "deadline": "10月17日12:00",
                    "deadline_iso": "2026-10-17T12:00:00+08:00",
                },
            ]
        )
        order = [
            "今天中午",
            "今天下午",
            "明天事项",
            "尽快提交",
            "月底事项",
            "普通无日期",
            "过期事项",
        ]
        self.assertEqual(sorted(order, key=text.index), order)
        self.assertIn("今天截止", text)
        self.assertIn("已过期", text)
        self.assertIn("需尽快处理", text)

    def test_materials_are_listed_and_different_requirements_not_merged(self):
        text = self.render(
            [
                {"action": "提交申请", "materials": ["申请表", "身份证复印件", "申请表"]},
                {"action": "提交申请", "materials": ["成绩单"]},
                {"action": "参加会议"},
            ]
        )
        self.assertIn("材料：申请表、身份证复印件", text)
        self.assertIn("材料：成绩单", text)
        self.assertEqual(text.count("要做：提交申请"), 2)
        self.assertEqual(text.count("材料："), 2)

    def test_invalid_optional_fields_are_rejected(self):
        for fields in [
            '"materials":"身份证"',
            '"materials":[123]',
            '"urgency":"random"',
            '"deadline_iso":"tomorrow"',
        ]:
            with self.assertRaises(ValueError):
                parse_items('{"items":[{"sources":["N1"],"action":"报名",' + fields + "}]}")

    def test_date_only_today_not_expired_and_timezone_compared(self):
        text = self.render(
            [
                {"action": "全天截止", "deadline": "10月17日", "deadline_iso": "2026-10-17"},
                {
                    "action": "清晨已过期",
                    "deadline": "10月17日06:00",
                    "deadline_iso": "2026-10-16T22:00:00Z",
                },
            ]
        )
        self.assertLess(text.index("全天截止"), text.index("清晨已过期"))
        self.assertIn("【群2】 已过期", text)
        self.assertNotIn("【群1】 已过期", text)
