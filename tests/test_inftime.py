#!/usr/bin/env python3
"""
Inftime 回归测试 —— 只用标准库 unittest，不引入 pytest。

    python -m unittest discover -s tests -v

这里每一个用例都对应一个**真实踩过的坑**。
规矩（记在项目备忘里）：凡是修过的 bug，必须在这里留一条断言，
否则下次改别的地方会把同一个洞再捅穿一次。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inftime.extract import extract, rule_extract, split_source_prefix   # noqa: E402
from inftime.ics import build_calendar                                     # noqa: E402
from inftime.schema import Event                                           # noqa: E402
from inftime.store import Store                                            # noqa: E402
from inftime.timeparse import merge_spans, parse_when, strip_time_expressions  # noqa: E402

TZ = timezone(timedelta(hours=8))
# 固定基准时间：2026-10-10 星期六 10:00
NOW = datetime(2026, 10, 10, 10, 0, tzinfo=TZ)


class TestTimeParse(unittest.TestCase):
    def p(self, text):
        return parse_when(text, NOW)

    def test_relative_days(self):
        self.assertEqual(self.p("明天下午三点开会").start, datetime(2026, 10, 11, 15, 0, tzinfo=TZ))
        self.assertEqual(self.p("后天上午开会").start, datetime(2026, 10, 12, 9, 0, tzinfo=TZ))

    def test_week_rolls_forward(self):
        """坑：周六说「本周五」，人指的是下一个周五，不是已经过去的那天。"""
        self.assertEqual(self.p("本周五下午两点交材料").start,
                         datetime(2026, 10, 16, 14, 0, tzinfo=TZ))

    def test_next_week_wednesday(self):
        """坑：早期版本按「今天 + delta」算，把下周三算成了 10-21（多了整整一周）。"""
        self.assertEqual(self.p("下周三上午十点面试").start,
                         datetime(2026, 10, 14, 10, 0, tzinfo=TZ))

    def test_half_width_and_meridiem(self):
        self.assertEqual(self.p("晚上7点半吃饭").start, datetime(2026, 10, 10, 19, 30, tzinfo=TZ))
        self.assertEqual(self.p("下午两点半之前到").start,
                         datetime(2026, 10, 10, 14, 30, tzinfo=TZ))

    def test_bare_hour_assumes_afternoon(self):
        """口语里「六点前」在白天场景指 18:00，不是 06:00。"""
        r = self.p("请在六点前领取包裹")
        self.assertEqual(r.start.hour, 18)
        self.assertTrue(r.note, "应记录歧义提示，便于后续向用户确认")

    def test_deadline_marker(self):
        self.assertTrue(self.p("本周内结束这个PPT").is_deadline)
        self.assertTrue(self.p("月底前交报销单").is_deadline)

    def test_month_end(self):
        self.assertEqual(self.p("月底前交报销单").start.date().isoformat(), "2026-10-31")

    def test_time_range(self):
        r = self.p("周五晚上七点到九点有音乐会")
        self.assertEqual(r.start.hour, 19)
        self.assertEqual(r.end.hour, 21)

    def test_no_time(self):
        self.assertFalse(self.p("记得请女朋友吃饭").ok)

    def test_single_char_evening_marker(self):
        """坑：「下周一晚七点」里的「晚」不是「晚上」，早期版本按上午算成了 07:00。"""
        r = self.p("下周一晚七点有一次直播答疑")
        self.assertEqual(r.start, datetime(2026, 10, 12, 19, 0, tzinfo=TZ))

    def test_relative_month_day(self):
        """坑：「本月25日前」和「下个月15号」早期完全不识别，被当成没有时间。"""
        self.assertEqual(self.p("本月25日前提交值班表").start.date().isoformat(), "2026-10-25")
        self.assertEqual(self.p("下个月15号校庆返校").start.date().isoformat(), "2026-11-15")

    def test_bare_day_after_relative_month(self):
        """坑：「下个月的房租记得1号前转过来」——日子和月份隔了半句话，也要能连上。"""
        r = self.p("下个月的房租记得1号前转过来")
        self.assertEqual(r.start.date().isoformat(), "2026-11-01")
        self.assertTrue(r.is_deadline)

    def test_span_merge_does_not_eat_neighbours(self):
        """坑（严重）：按倒序逐个删除 span，删第一个后下标全部错位，
        「周五晚上七点到九点在东方艺术中心」被切成「方艺术中心」——吃掉了「东」。
        修法是先合并区间，再基于原始字符串一次性切除。
        """
        text = "周五晚上七点到九点在东方艺术中心有一场音乐会"
        r = parse_when(text, NOW)
        from inftime.timeparse import collect_spans
        out = strip_time_expressions(text, collect_spans(text, r, NOW))
        for must_keep in ("东方艺术中心", "音乐会"):
            self.assertIn(must_keep, out, f"「{must_keep}」被误删：{out!r}")

    def test_merge_spans_helper(self):
        self.assertEqual(merge_spans([(0, 2), (2, 5), (7, 9)]), [(0, 5), (7, 9)])


class TestExtract(unittest.TestCase):
    def test_title_clean(self):
        ev = rule_extract("请大家在本周五下午两点前把评审材料发我，务必不要拖", now=NOW)
        self.assertNotIn("本周五", ev.title)
        self.assertNotIn("两点", ev.title)
        self.assertNotIn("大家", ev.title)
        self.assertIn("评审材料", ev.title)

    def test_location(self):
        ev = rule_extract("10月20日上午9:30在综合楼305会议室召开年级大会", now=NOW)
        self.assertEqual(ev.location, "综合楼305会议室")
        self.assertNotIn("综合楼", ev.title, "地点不应残留在标题里")

    def test_location_not_swallowing_sentence(self):
        """坑：泛化字符类把「请在六点前凭提货码至同济大学菜鸟驿站」整段当场了地点。"""
        ev = rule_extract("请在六点前凭提货码至同济大学菜鸟驿站领取申通包裹", now=NOW)
        self.assertEqual(ev.location, "同济大学菜鸟驿站")
        self.assertLessEqual(len(ev.location), 12)

    def test_question_needs_clarification(self):
        ev = rule_extract("那我们明天下午去参加比赛？", now=NOW)
        self.assertTrue(ev.needs_clarification)

    def test_no_time_but_action(self):
        ev = rule_extract("记得把这件事做好", now=NOW)
        self.assertEqual(ev.time_granularity, "unknown")
        self.assertTrue(ev.needs_clarification)

    def test_chitchat_filtered(self):
        self.assertEqual(extract("哈哈这个表情包太逗了", now=NOW, use_llm=False), [])

    def test_source_prefix_parsed(self):
        src, who, msg = split_source_prefix("[项目群] 李工: 明天下午两点开会")
        self.assertEqual(src, "项目群")
        self.assertEqual(who, "李工")
        self.assertEqual(msg, "明天下午两点开会")

    def test_source_prefix_not_over_eager(self):
        """「教务处通知：…」里的「教务处通知」是标题不是人名，不能当说话人拆走。"""
        src, who, msg = split_source_prefix("教务处通知：明天下午两点开会")
        self.assertIsNone(who)
        self.assertIn("教务处通知", msg)

    def test_priority_boost_for_deadline(self):
        soon = rule_extract("今天下午五点前必须提交", now=NOW)
        far = rule_extract("下下周五提交", now=NOW)
        self.assertGreater(soon.priority, far.priority)


class TestICS(unittest.TestCase):
    def test_timezone_is_really_utc(self):
        """坑（严重）：`astimezone(None)` 转的是本地时区，却拼了 'Z' 后缀，
        导致所有导入日历的事件整体偏移 8 小时，而且完全无声。
        """
        ev = Event(title="开会", start="2026-10-10T18:00:00+08:00",
                   end="2026-10-10T19:00:00+08:00", time_granularity="minute")
        ics = build_calendar([ev])
        self.assertIn("DTSTART:20261010T100000Z", ics,
                      "18:00+08:00 必须写成 10:00Z")

    def test_all_day_uses_date_value(self):
        ev = Event(title="全天事", start="2026-10-11T09:00:00+08:00",
                   time_granularity="day", all_day=True)
        ics = build_calendar([ev])
        self.assertIn("DTSTART;VALUE=DATE:20261011", ics)

    def test_lines_folded_under_75_bytes(self):
        ev = Event(title="很长的标题" * 20, start="2026-10-10T18:00:00+08:00",
                   raw_text="原文" * 100, time_granularity="minute")
        ics = build_calendar([ev])
        bad = [l for l in ics.split("\r\n") if len(l.encode("utf-8")) > 75]
        self.assertEqual(bad, [], "ICS 行超过 75 字节会导致解析失败")

    def test_escaping(self):
        ev = Event(title="含,逗号;分号\\反斜杠", start="2026-10-10T18:00:00+08:00",
                   time_granularity="minute")
        ics = build_calendar([ev])
        self.assertIn("\\,", ics)
        self.assertIn("\\;", ics)

    def test_uid_is_stable_across_regeneration(self):
        """坑：UID 含随机数 → 重复导入日历，日程翻倍。

        原实现 `UID:{ev.id}-inftime@{uuid4().hex[:8]}.local`。UUID 每次都变，
        日历年历把「同一件事的两个不同 UID」当成**两个事件**。
        于是「改几条 → 重新导出全部 → 再导入」会让已导入的每一条都复制一份，
        而这恰恰是最自然的用法（批量导出比逐条导出省事）。

        与 `Event.fingerprint` 含 title 是同一类错误：
        **凡是进「身份/去重键」的字段，都必须来自确定性来源。**

        契约：同一条消息重放时，产出的 UID 必须逐字相同。
        """
        raw = "10月20日上午9:30在综合楼305会议室召开年级大会"
        mk = lambda: Event(title="参加年级大会", raw_text=raw,
                           start="2026-10-20T09:30:00+08:00",
                           source_channel="wecom", time_granularity="minute")

        def uid_of(ev):
            for line in build_calendar([ev]).split("\r\n"):
                if line.startswith("UID:"):
                    return line[4:]
            raise AssertionError("ICS 里没有 UID 行")

        first, second = uid_of(mk()), uid_of(mk())
        self.assertEqual(
            first, second,
            "同一条日程两次生成的 UID 必须相同，否则日历里会重复",
        )
        # 锁住派生来源：UID 必须由 fingerprint 派生（不是 ev.id，更不是随机数）
        self.assertEqual(first, f"{mk().fingerprint}-inftime@inftime.local")
        self.assertTrue(first.endswith("@inftime.local"))

        # 反向：真正不同的事必须有不同的 UID，否则会被日历合并掉
        other = Event(title="参加年级大会", raw_text="另一个通知",
                      start="2026-10-20T09:30:00+08:00",
                      source_channel="wecom", time_granularity="minute")
        self.assertNotEqual(first, uid_of(other),
                            "不同的事件必须有不同 UID，否则日历会合并")


class TestPush(unittest.TestCase):
    """`inftime push` 把 .ics 交给系统日历，绕开 AppleScript 授权。

    背景（2026-10-10 实测）：这条进程链（WorkBuddy → python → osascript）
    没有可识别的 app 身份，macOS 对 Calendar 的 AppleScript 访问直接返回
    -10004 且**连授权弹窗都不弹**。所以改走 LaunchServices（`open -a Calendar`），
    由 Calendar.app 自己弹导入确认框，代价是用户点一次确认。

    既然 `open` 是「用户可见的外部动作」，就必须有断言守住它：
    **--dry-run 绝不能真的去打开日历。**
    """

    def _store_with_one_event(self, d):
        store = Store(Path(d) / "t.db")
        store.add(Event(title="参加年级大会",
                        raw_text="10月20日上午9:30在综合楼305会议室召开年级大会",
                        start="2026-10-20T09:30:00+08:00",
                        location="综合楼305会议室",
                        source_channel="wecom",
                        time_granularity="minute"))
        return store

    def test_dry_run_never_opens_calendar(self):
        import argparse
        import subprocess as sp
        from unittest import mock

        from inftime import cli

        with tempfile.TemporaryDirectory() as d:
            store = self._store_with_one_event(d)
            out = Path(d) / "日程.ics"
            opened: list = []

            with mock.patch.object(cli, "_store", lambda: store), \
                 mock.patch.object(sp, "run", lambda *a, **k: opened.append(a)):
                rc = cli.cmd_push(argparse.Namespace(
                    out=str(out), alarm=15, to_icloud=False, dry_run=True))

            self.assertEqual(rc, 0)
            self.assertEqual(opened, [], "--dry-run 不该调用任何外部命令")
            self.assertTrue(out.exists(), "即使用 --dry-run 也要把 .ics 生成出来")
            self.assertIn("BEGIN:VCALENDAR", out.read_text(encoding="utf-8"))
            store.close()

    def test_no_events_means_no_calendar_launch(self):
        import argparse
        import subprocess as sp
        from unittest import mock

        from inftime import cli

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "empty.db")     # 空库
            opened: list = []

            with mock.patch.object(cli, "_store", lambda: store), \
                 mock.patch.object(sp, "run", lambda *a, **k: opened.append(a)):
                rc = cli.cmd_push(argparse.Namespace(
                    out=None, alarm=15, to_icloud=False, dry_run=False))

            self.assertEqual(rc, 0)
            self.assertEqual(opened, [], "没有事件时不该弹日历打扰用户")
            store.close()

    def test_real_run_activates_calendar_before_handing_over_file(self):
        """确认框可能被别的窗口挡住 → 必须**先激活日历、再送文件**。

        2026-10-10 实测踩过的坑：只发 `open -a Calendar <ics>` 时，
        确认框不一定跳到最前，用户会以为「没反应」。所以先用
        `open -a Calendar`（不带文件）把日历拉到前台，隔一下再交文件。
        这里锁住顺序，防止以后被「顺手优化」掉。
        """
        import argparse
        import subprocess as sp
        from unittest import mock

        from inftime import cli

        with tempfile.TemporaryDirectory() as d:
            store = self._store_with_one_event(d)
            out = Path(d) / "日程.ics"
            calls: list = []

            def fake_run(*a, **k):
                calls.append(list(a[0]))

            with mock.patch.object(cli, "_store", lambda: store), \
                 mock.patch.object(sp, "run", fake_run), \
                 mock.patch.object(cli.sys, "platform", "darwin"), \
                 mock.patch("time.sleep", lambda *_: None):
                rc = cli.cmd_push(argparse.Namespace(
                    out=str(out), alarm=15, to_icloud=False, dry_run=False))

            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 2, "应当恰好两次 open：先激活、后送文件")
            self.assertEqual(calls[0], ["open", "-a", "Calendar"],
                             "第一步必须是**不带文件**的激活调用")
            self.assertEqual(calls[1][:3], ["open", "-a", "Calendar"],
                             "第二步才是把 .ics 交给日历")
            self.assertTrue(str(calls[1][3]).endswith(".ics"))
            store.close()


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_dedupe(self):
        ev = Event(title="开会", raw_text="明天开会", start="2026-10-11T09:00:00+08:00")
        _, first = self.store.add(ev)
        _, second = self.store.add(ev)
        self.assertTrue(first)
        self.assertFalse(second, "同一条消息重复喂进来不应产生第二条事件")

    def test_status_flow(self):
        ev = Event(title="交表", raw_text="明天交表")
        self.store.add(ev)
        self.store.set_status(ev.id, "confirmed")
        self.assertEqual(self.store.get(ev.id).status, "confirmed")

    def test_id_prefix_lookup(self):
        ev = Event(title="交表", raw_text="明天交表")
        self.store.add(ev)
        self.assertIsNotNone(self.store.by_id_prefix(ev.id[:5]))

    def test_dedupe_survives_llm_title_drift(self):
        """坑：fingerprint 里含 title，会让去重**彻底失效**。

        2026-10-10 实测：同一条群通知连跑 4 次，大模型给出 4 个不同标题
        （「参加年级大会」「按时参加年级大会」「准时出席年级大会」「全员参加年级大会」）。
        原 fingerprint 把 title 算进 basis，于是同一条消息每读一次就是一个新指纹，
        每次都判为新事件 → 企业微信桥每跑一轮，日程库里就多堆一条。

        契约：只要 source_channel / raw_text / start 三样没变，
              标题怎么飘，指纹都必须一样。
        """
        raw = "10月20日上午9:30在综合楼305会议室召开年级大会，全体准时参加"
        start = "2026-10-20T09:30:00+08:00"
        variants = [
            Event(title="参加年级大会", raw_text=raw, start=start, source_channel="wecom"),
            Event(title="按时参加年级大会", raw_text=raw, start=start, source_channel="wecom"),
            Event(title="准时出席年级大会", raw_text=raw, start=start, source_channel="wecom"),
            Event(title="全员参加年级大会", raw_text=raw, start=start, source_channel="wecom"),
        ]
        fps = {e.fingerprint for e in variants}
        self.assertEqual(
            len(fps), 1,
            "标题不同不该改变指纹——title 由模型生成，天然会飘",
        )

        # 反向：真正不同的事必须能被区分开，否则去重会误吃正常事件
        other = Event(title="参加年级大会", raw_text=raw,
                      start="2026-10-21T09:30:00+08:00", source_channel="wecom")
        self.assertNotEqual(
            variants[0].fingerprint, other.fingerprint,
            "时间不同 = 不同的事，fingerprint 必须不同",
        )

        # 落库验证：标题飘的 4 条，最终只应留下 1 条
        for ev in variants:
            self.store.add(ev)
        self.assertEqual(
            len(self.store.list(limit=50)), 1,
            "同一条消息被读多次，只应留下一个事件",
        )


class TestAuditEntry(unittest.TestCase):
    def test_audit_runs_and_reports(self):
        from inftime.audit import render_markdown, run_audit
        text = "\n".join([
            "[年级群] 辅导员: 10月20日上午9:30在综合楼305会议室开会，全体参加",
            "[项目群] 李工: 本周五下午两点前把材料发我",
            "[家庭群] 妈: 周末记得回来吃饭",
            "[水群] 路人: 哈哈哈哈",
        ])
        res = run_audit(text, window_days=30, now=NOW, use_llm=False)
        md = render_markdown(res, now=NOW)
        self.assertGreaterEqual(res.scanned, 4)
        self.assertIn("承诺审计报告", md)
        self.assertIn("完全没有时间", md)


class TestModelNameGuard(unittest.TestCase):
    """坑：模型名写错会潜伏到「充值成功那一刻」才爆。

    2026-10-10 实测：DeepSeek 的 /chat/completions 是**先查余额、后校验模型名**，
    余额为空时无论模型名对不对都返回 402。所以我原先猜的 `deepseek-v4-flash`
    一直没报错，直到差点以为「配好了」。
    对策：写入配置时拿服务端 /models 清单核对，对不上当场拦下。

    ⚠️ 这两条断言必须做注入测试 —— 假的模型名要真的被判 False，
       没验证过的检查等于没有检查。
    """

    def setUp(self):
        from inftime.llm import LLMConfig
        self.cfg = LLMConfig(base_url="https://example.invalid/api",
                             api_key="dummy", model="glm-4-flash")

    def _patch(self, payload=None, status=None):
        """把 urlopen 换掉，既能造正常响应，也能造 HTTP 错误。"""
        import json as _json
        import urllib.error
        import urllib.request as _ur
        from unittest import mock

        class FakeResp:
            def __init__(self, data):
                self._b = _json.dumps(data).encode("utf-8")

            def read(self):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            if status is not None:
                raise urllib.error.HTTPError(req.full_url, status, "boom", {}, None)
            return FakeResp(payload)

        return mock.patch.object(_ur, "urlopen", fake_urlopen)

    def _check(self, cfg):
        import contextlib
        import io
        from inftime.cli import _check_model
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = _check_model(cfg)
        return ok, buf.getvalue()

    def test_model_list_is_parsed(self):
        from inftime.llm import list_models
        with self._patch(payload={"object": "list", "data": [
            {"id": "glm-4-plus"}, {"id": "glm-4-flash"},
        ]}):
            ids, note = list_models(self.cfg)
        self.assertEqual(ids, ["glm-4-flash", "glm-4-plus"])
        self.assertIn("2", note)

    def test_wrong_model_name_is_rejected(self):
        """注入测试：故意用不存在的模型名，检查必须报错。"""
        from dataclasses import replace
        bad = replace(self.cfg, model="glm-9-ultra-fake")
        with self._patch(payload={"data": [{"id": "glm-4-flash"}, {"id": "glm-4-plus"}]}):
            ok, out = self._check(bad)
        self.assertFalse(ok, "清单里没有这个模型，必须判 False")
        self.assertIn("glm-9-ultra-fake", out)

    def test_correct_model_name_passes(self):
        with self._patch(payload={"data": [{"id": "glm-4-flash"}]}):
            ok, _ = self._check(self.cfg)
        self.assertTrue(ok)

    def test_endpoint_without_models_api_does_not_block(self):
        """端点没实现 /models 时不能把用户挡在门外，只能跳过校验。"""
        with self._patch(status=404):
            ok, out = self._check(self.cfg)
        self.assertTrue(ok)
        self.assertIn("跳过", out)


class TestReasoningModelProbe(unittest.TestCase):
    """坑：推理模型会先烧 reasoning token，max_tokens 给小了会返回**空内容**。

    2026-10-10 实测（智谱 glm-5.3-flash）：
        max_tokens=32  -> content=''  finish_reason='length'   ← 32 个 token 全被推理吃掉
        max_tokens=512 -> content='{"ok": true}'  finish_reason='stop'
    危害：探活会显示「HTTP 200 但内容为空」，把人引去怀疑 key。

    这两条断言锁住「探活请求必须给足 token」和「空内容必须判失败」。
    """

    def setUp(self):
        from inftime.llm import LLMConfig
        self.cfg = LLMConfig(base_url="https://example.invalid/api",
                             api_key="dummy", model="glm-5.3-flash")

    def _capture(self, body):
        """替换 urlopen，抓下探活请求的 payload，并按给定 body 返回。"""
        import json as _json
        import urllib.request as _ur
        from unittest import mock

        seen = {}

        class FakeResp:
            def __init__(self, b):
                self._b = _json.dumps(b).encode("utf-8")

            def read(self):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            seen["payload"] = _json.loads(req.data.decode("utf-8"))
            return FakeResp(body)

        return mock.patch.object(_ur, "urlopen", fake_urlopen), seen

    def test_probe_asks_for_enough_tokens(self):
        """探活的 max_tokens 必须给足，否则推理模型会被截断成空内容。"""
        from inftime.llm import PROBE_MAX_TOKENS, probe
        ctx, seen = self._capture({
            "model": "glm-5.3-flash",
            "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 60},
        })
        with ctx:
            ok, _ = probe(self.cfg)
        self.assertTrue(ok)
        self.assertGreaterEqual(
            seen["payload"]["max_tokens"], 128,
            "探活的 max_tokens 太小，推理模型会先把预算烧在 reasoning 上")
        self.assertEqual(seen["payload"]["max_tokens"], PROBE_MAX_TOKENS)

    def test_empty_content_is_a_failure_not_success(self):
        """注入测试：空内容 + finish_reason=length 必须判失败，且要说清原因。"""
        from inftime.llm import probe
        ctx, _ = self._capture({
            "model": "glm-5.3-flash",
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"total_tokens": 32,
                      "completion_tokens_details": {"reasoning_tokens": 32}},
        })
        with ctx:
            ok, msg = probe(self.cfg)
        self.assertFalse(ok, "HTTP 200 但内容为空，绝不能报成功")
        self.assertIn("空内容", msg)
        self.assertIn("reasoning_tokens", msg)


class TestCompareMeasurement(unittest.TestCase):
    """坑：对照实验的「差异率」很容易算成一个没意义的数。

    两个具体陷阱（都在 2026-10-10 发现）：
      1. 把 confidence 算进差异 —— 模型的自评置信度和规则层永远不相等，
         早期版本如实反映后，47 条里几乎每条都「有差异」，数字完全失去意义。
      2. 模型调用失败被算成「没有差异」—— extract() 会静默降级回规则结果，
         两边输出一模一样。若不单独统计，失败越多，「模型越没用」的假结论越强。
    """

    def _ev(self, title, start=None, conf=0.6, gran=None):
        from inftime.schema import Event
        return Event(title=title, start=start, raw_text=title,
                     confidence=conf, time_granularity=gran or "hour")

    def test_confidence_alone_is_not_a_difference(self):
        """只有置信度不同 -> 必须判为「一致」。"""
        from inftime.compare import Item
        a = self._ev("开会", "2026-10-20T09:30:00+08:00", conf=0.60)
        b = self._ev("开会", "2026-10-20T09:30:00+08:00", conf=0.97)
        self.assertEqual(Item(text="x", rule=[a], llm=[b]).kind, "",
                         "置信度是模型自评，不能算成结论差异")

    def test_punctuation_only_is_not_a_difference(self):
        from inftime.compare import Item
        a = self._ev("交材料", "2026-10-20T09:30:00+08:00")
        b = self._ev("交材料。", "2026-10-20T09:30:00+08:00")
        self.assertEqual(Item(text="x", rule=[a], llm=[b]).kind, "")

    def test_title_change_is_a_difference(self):
        from inftime.compare import Item
        a = self._ev("去把这件事做好", "2026-10-20T09:30:00+08:00")
        b = self._ev("完成先前交代的任务", "2026-10-20T09:30:00+08:00")
        self.assertEqual(Item(text="x", rule=[a], llm=[b]).kind, "标题不同")

    def test_count_change_is_a_difference(self):
        from inftime.compare import Item
        a = self._ev("开会", "2026-10-20T09:30:00+08:00")
        self.assertEqual(Item(text="x", rule=[a], llm=[]).kind, "条数不同")

    def test_failed_model_call_is_not_counted_as_agreement(self):
        """注入测试：模型没参与时，绝不能算成「结论一致」。"""
        from inftime.compare import CompareResult, Item
        a = self._ev("开会", "2026-10-20T09:30:00+08:00")
        it = Item(text="x", rule=[a], llm=[a], llm_failed=True)
        self.assertEqual(it.kind, "模型未参与")
        res = CompareResult(model="m", items=[it], llm_calls=1, llm_ok=0)
        self.assertEqual(res.n_same, 0, "调用失败不能计入「一致」")
        self.assertFalse(res.viable, "成功率不足时结论不可采信")

    def test_viable_requires_eighty_percent(self):
        from inftime.compare import CompareResult
        self.assertTrue(CompareResult(model="m", llm_calls=10, llm_ok=8).viable)
        self.assertFalse(CompareResult(model="m", llm_calls=10, llm_ok=7).viable)

    def test_extract_reports_llm_stats(self):
        """extract 必须如实回报模型参与情况，否则 compare 无从判断。"""
        from inftime.extract import extract
        st: dict = {}
        extract("明天下午三点开会", source_channel="t", use_llm=False, stats=st)
        self.assertEqual(st.get("llm_calls"), 0)
        self.assertFalse(st.get("llm_used"))


class TestClipboardCredentialGate(unittest.TestCase):
    """坑（安全）：剪贴板是全机器最脏的数据源，而原来的过滤器挡不住凭据。

    2026-10-10 实测：合成密码、postgres 连接串、xxx.yyy 形式的 API key、
    带空格的银行卡号 —— 全部会通过原来的 _NOISE 过滤，
    然后被 use_llm=True 原样发到大模型厂商服务器。

    这与项目第一条原则（本地优先、数据不出本机）直接冲突。
    断言锁住：命中疑似凭据必须被拦住，且正常消息不受影响。
    """

    def setUp(self):
        from inftime.watch_clipboard import looks_sensitive
        self.looks = looks_sensitive

    def test_credentials_are_blocked(self):
        # 全部为合成样本，不对应任何真实凭据
        bad = {
            "合成密码": "Tq7#mKp2!vZx9",
            "数据库连接串": "postgres://admin:Pa55w0rd@10.0.3.7:5432/prod",
            "API Key（xxx.yyy 形）": "b79ac3502ba34e0881762c6e58f18801.UAhjuiV4E5DiusjK",
            "银行卡号": "6222 0212 3456 7890 123",
        }
        for name, s in bad.items():
            with self.subTest(name):
                self.assertTrue(self.looks(s), f"{name} 必须被拦下，否则会上云")

    def test_keyword_hits_are_blocked(self):
        for s in ("我的密码是 hunter2 记得改",
                  "这是 API_KEY，别外传",
                  "token: abcdefghijklmnop",
                  "身份证 310110199801011234"):
            with self.subTest(s):
                self.assertTrue(self.looks(s))

    def test_normal_messages_pass(self):
        good = [
            "[年级大群] 辅导员: 10月20日上午9:30在综合楼305会议室开会，全体参加",
            "好的，我明天下午给你",
            "本周日全天停水，请提前储水",
            "明天下午三点在东方艺术中心开评审会",
        ]
        for s in good:
            with self.subTest(s):
                self.assertEqual(self.looks(s), "", f"正常消息被误杀：{s}")

    def test_long_english_word_is_not_a_key(self):
        """纯小写长单词不算密钥；必须至少含两类字符才判为 key/token。"""
        self.assertEqual(self.looks("internationalizationconfigurationmanager"), "")

    def test_reason_is_human_readable(self):
        """提示必须说清原因，否则用户会以为工具坏了。"""
        self.assertIn("敏感词", self.looks("password: x"))
        self.assertTrue(self.looks("Tq7#mKp2!vZx9"))


class TestWeComBridge(unittest.TestCase):
    """坑：企微桥必须能区分「真没有消息」和「权限坏了」。

    2026-10-10 实测：机器人「消息」权限过期（errcode 850003）时，
    收件箱脚本返回的是**空列表 + 退出码 0**，桥于是显示
    「（企微里没有新消息）」——**一个失败伪装成了正常空状态**。
    这类伪装比直接报错更费时间，因为你会转头去查别的地方。

    另一个反直觉点：`wecom-cli auth show --status` 返回 `authorized`
    并不能说明机器人消息权限可用，那是 CLI 自身的授权状态，两道是分开的。
    """

    def _fake_run(self, payload):
        import json as _json
        import subprocess as _sp
        from unittest import mock

        class R:
            def __init__(self, out):
                self.stdout = out
                self.stderr = ""
                self.returncode = 0

        return mock.patch.object(_sp, "run",
                                 lambda *a, **k: R(_json.dumps(payload, ensure_ascii=False)))

    def test_business_error_is_surfaced_not_swallowed(self):
        """注入测试：errcode 非 0 必须抛错，不能退化成「没有新消息」。"""
        from inftime.wecom_bridge import BridgeError, preflight
        with self._fake_run({"errcode": 850003,
                             "errmsg": "authorization expired",
                             "help_message": "当前机器人「消息」使用权限已过期"}):
            with self.assertRaises(BridgeError) as ctx:
                preflight()
        self.assertIn("850003", str(ctx.exception))
        self.assertIn("权限已过期", str(ctx.exception))

    def test_normal_empty_result_passes_preflight(self):
        from inftime.wecom_bridge import preflight
        with self._fake_run({"errcode": 0, "chats": []}):
            preflight()                      # 不抛异常

    def test_non_json_output_is_tolerated(self):
        """CLI 偶尔吐纯文本，不能因此炸掉。"""
        import subprocess as _sp
        from unittest import mock
        from inftime.wecom_bridge import preflight

        class R:
            stdout = "authorized"
            stderr = ""
            returncode = 0

        with mock.patch.object(_sp, "run", lambda *a, **k: R()):
            preflight()

    def test_fetch_uses_isolated_state_file(self):
        """坑：桥和主收件箱共用一张「已读表」→ 用户读一次，桥永久漏一条。

        2026-10-10 实测浪费半小时：`wecom-inbox` 技能把 seen 写在
        `~/.workbuddy/skills/wecom-inbox/state/cursor.json`，人和 AI 共用。
        联调时读了一次群消息确认内容，那 3 条就被永久标记已读，
        之后桥无论传 `--hours 155` 都读不回来
        （inbox 的 seen 过滤发生在时间窗口之外，独立生效）。

        人机共表对「轮流看同一个收件箱」是对的，对**常驻监听方**是致命的：
        **用户越勤快，桥越瞎。**

        契约：桥读消息时必须指向自己的状态文件（~/.inftime 下）。
        """
        from unittest import mock
        from inftime import wecom_bridge as wb

        captured = {}

        def fake_run(script, args):
            captured["script"] = Path(script).name
            captured["args"] = list(args)
            return 0, '{"count": 0, "messages": []}', ""

        with mock.patch.object(wb, "_run", fake_run):
            wb.fetch_messages(hours=155, peek=True)

        self.assertEqual(captured["script"], "wecom_inbox.py")
        self.assertIn("--state-file", captured["args"],
                      "桥必须用独立状态文件，否则会被用户的日常阅读致盲")
        idx = captured["args"].index("--state-file")
        self.assertEqual(captured["args"][idx + 1], str(wb.STATE_FILE))
        self.assertIn(".inftime", str(wb.STATE_FILE),
                      "状态文件应落在桥自己的目录下，不能落在技能目录里")

    def test_reply_writes_the_same_state_file(self):
        """回声过滤的两半必须指向同一个状态文件。

        回群时 `wecom_reply.py` 会把正文摘要写进 state；
        读群时 `wecom_inbox.py` 从 state 里读它来挡回声。
        两边写的/读的若不是同一个文件，下一轮桥就会把自己刚回的消息
        当成新群消息读进来 → 抽取 → 再回一条 → **自环**。
        """
        from unittest import mock
        from inftime import wecom_bridge as wb

        captured = {}

        def fake_run(script, args):
            captured["args"] = list(args)
            return 0, "ok", ""

        with mock.patch.object(wb, "_run", fake_run):
            wb.reply_to_group("小泰腾OPC", "已记下：xxx")

        self.assertIn("--state-file", captured["args"])
        idx = captured["args"].index("--state-file")
        self.assertEqual(
            captured["args"][idx + 1], str(wb.STATE_FILE),
            "发送方和读取方必须写/读同一个状态文件，否则回声挡不住",
        )

    def test_render_extracts_from_group_message(self):
        """群消息要能走通整条抽取链路（含来源前缀拼接）。"""
        from inftime.wecom_bridge import render
        msgs = [{"chat_name": "项目组", "sender": "李工",
                 "preview": "本周五下午两点前把材料发我", "send_time": "2026-10-10 10:00"}]
        results, created = render(msgs, store=None, use_llm=False)
        self.assertEqual(created, 0, "store=None 时不应写库")
        self.assertEqual(len(results), 1)
        _, events = results[0]
        self.assertTrue(events, "群通知应该被抽成事件")
        # 断言只锁「有意愿的契约」：抽出了东西、时间算对了、来源标成企微。
        # 不去锁标题的具体措辞 —— 规则层抠掉时间后措辞本就会退化
        # （这句的实际产出是「材料发我」，「把」被当虚词剥掉了），
        # 把退化结果写死进断言，等于把 bug 固化成契约。
        ev = events[0]
        self.assertGreaterEqual(len(ev.title), 2)
        self.assertEqual(ev.start[:16], "2026-10-16T14:00")
        self.assertEqual(ev.source_channel, "wecom")

    def test_render_skips_empty_preview(self):
        from inftime.wecom_bridge import render
        results, _ = render([{"chat_name": "x", "sender": "y", "preview": "   "}],
                            store=None, use_llm=False)
        self.assertEqual(results, [])

    def test_commit_writes_to_store(self):
        from inftime.wecom_bridge import render
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            msgs = [{"chat_name": "项目组", "sender": "李工",
                     "preview": "本周五下午两点前把材料发我"}]
            _, created = render(msgs, store=store, use_llm=False)
            self.assertEqual(created, 1)
            # 同一条消息再来一次，fingerprint 去重必须生效
            _, again = render(msgs, store=store, use_llm=False)
            self.assertEqual(again, 0, "重复消息不应产生第二条")
            store.close()


    def test_reply_text_has_no_sentinel_of_its_own(self):
        """坑：哨兵前缀必须**只有一处**，否则回群会变成「【助手】【助手】…」。

        `wecom_reply.py --group` 会自动在正文最前面补 SENTINEL("【助手】")，
        桥如果再手写一个，群里就会出现双前缀。哨兵只有一个职责——让收件箱
        认出「这条是自己发的」以防自环，重复写毫无价值，还会污染群消息。

        契约：桥产出的回群正文**不以** 【助手】 开头；前缀统一由回复器加。
        """
        from inftime.wecom_bridge import _summary_text

        ev = Event(title="参加年级大会", raw_text="x",
                   start="2026-10-20T09:30:00+08:00")
        for committed in (False, True):
            text = _summary_text([({"chat_name": "群", "sender": "我"}, [ev])], committed)
            self.assertFalse(
                text.startswith("【助手】"),
                f"桥不该自己加哨兵前缀（committed={committed}）：{text!r}",
            )
            self.assertIn("参加年级大会", text)

        # 空结果分支也不能带前缀，否则同样会叠成双份
        empty = _summary_text([], True)
        self.assertFalse(empty.startswith("【助手】"), f"空结果分支也带了前缀：{empty!r}")


class TestBridgeOutputTellsTheTruth(unittest.TestCase):
    """桥的输出必须让人看得出「它到底读到了什么」。

    2026-10-10 用户报「我在企微里说了个日程，感觉好像没反应」。
    排查后发现链路是通的（换默认参数就跑通了），**真正的坑在输出**：
    原实现把「没抽出事件」的消息直接 `continue` 掉，屏幕上只剩一行
    「读到 1 条消息」，于是「读到了但没识别出」和「压根没读到」
    **在输出上一模一样**。用户没法自诊断，只能来问。
    """

    def test_every_message_is_printed_even_without_events(self):
        import contextlib
        import io
        from unittest import mock

        from inftime import wecom_bridge as wb

        msgs = [
            {"chat_name": "小泰腾OPC", "sender": "薛以腾",
             "preview": "本周五下午两点前把材料发我", "send_time": "2026-10-10 13:24"},
            {"chat_name": "小泰腾OPC", "sender": "薛以腾",
             "preview": "哈哈今天天气不错", "send_time": "2026-10-10 13:25"},
        ]

        # ⚠️ --no-llm：单测不打网络。这条测的是**输出**，不是抽取质量。
        argv = ["inftime.wecom_bridge", "--no-llm"]
        buf = io.StringIO()
        with mock.patch.object(wb, "preflight", lambda: None), \
             mock.patch.object(wb, "fetch_messages", lambda hours, peek: msgs), \
             mock.patch.object(sys, "argv", argv), \
             contextlib.redirect_stdout(buf):
            rc = wb.main()

        out = buf.getvalue()
        self.assertEqual(rc, 0)
        for m in msgs:
            self.assertIn(m["preview"], out,
                          f"每条读到的消息都要打出来，否则用户会以为「没反应」：{m['preview']}")
        # 无事件的那条要明确标注，不能只是「沉默」
        self.assertIn("未识别出需要行动的事", out)

    def test_reply_is_silent_when_nothing_was_extracted(self):
        """`--reply` 只在抽到事件时才回群。

        配上定时轮询后，无条件回复 = 机器人**每小时**在群里刷一句
        「没有识别出需要行动的事」，把群变成日志。
        回群的语义是「确认已记下」—— 没记下就没什么可确认的。
        """
        import contextlib
        import io
        from unittest import mock

        from inftime import wecom_bridge as wb

        msgs = [{"chat_name": "小泰腾OPC", "sender": "薛以腾",
                 "preview": "哈哈今天天气不错", "send_time": "2026-10-10 13:25"}]
        sent: list = []

        argv = ["inftime.wecom_bridge", "--reply", "--no-llm"]
        buf = io.StringIO()
        with mock.patch.object(wb, "preflight", lambda: None), \
             mock.patch.object(wb, "fetch_messages", lambda hours, peek: msgs), \
             mock.patch.object(wb, "reply_to_group",
                               lambda group, text: sent.append((group, text))), \
             mock.patch.object(sys, "argv", argv), \
             contextlib.redirect_stdout(buf):
            rc = wb.main()

        self.assertEqual(rc, 0)
        self.assertEqual(sent, [], "没抽到事件时不该往群里发消息")
        self.assertIn("按约定不回群", buf.getvalue())


class TestVerifiedFacts(unittest.TestCase):
    """联网核实到的「精确时间/场馆」怎么存，才不会把去重键搞坏。

    背景动机：消息「10月31号和姐姐去苏州看梁静茹演唱会」只能抽出
    `全天 2026-10-31 @ 苏州`。真实开场时间和场馆得去票务平台查。
    但 **fingerprint 里含 `start`** —— 如果核实后直接改写 start，
    同一条群消息下次再读会算出新指纹 → 判为新事件 → 重复入库。
    所以核实值单独存（`verified`），原始值（`start`）永不改写。
    """

    def _event(self) -> Event:
        return Event(title="和姐姐看梁静茹演唱会",
                     raw_text="10月31号和姐姐去苏州看梁静茹演唱会",
                     start="2026-10-31T09:00:00+08:00",   # 全天占位值
                     all_day=True, time_granularity="day",
                     location="苏州", source_channel="wecom")

    def test_verified_does_not_change_fingerprint(self):
        ev = self._event()
        before = ev.fingerprint
        ev.verified = {"start": "2026-10-31T19:30:00+08:00",
                       "location": "苏州奥体中心体育馆",
                       "source": "大麦网", "at": "2026-10-10T13:40:00+08:00"}
        self.assertEqual(ev.fingerprint, before,
                         "核实信息绝不能影响去重键 —— 否则每核实一次就多一条日程")

    def test_effective_values_prefer_verified(self):
        ev = self._event()
        ev.verified = {"start": "2026-10-31T19:30:00+08:00",
                       "location": "苏州奥体中心体育馆", "source": "大麦网"}
        self.assertEqual(ev.effective_start, "2026-10-31T19:30:00+08:00")
        self.assertEqual(ev.effective_location, "苏州奥体中心体育馆")
        self.assertFalse(ev.effective_all_day,
                         "核实出具体时刻后就不该再显示成全天")
        self.assertEqual(ev.effective_granularity, "minute")
        self.assertEqual(ev.when_label(), "2026-10-31 19:30",
                         "界面必须显示核实后的时间，而不是全天的占位")

    def test_effective_falls_back_when_partial(self):
        """只核实到地点、没核实到时间 → 时间回落到原始值，地点用核实的。"""
        ev = self._event()
        ev.verified = {"location": "苏州奥体中心体育馆", "source": "大麦网"}
        self.assertEqual(ev.effective_start, "2026-10-31T09:00:00+08:00")
        self.assertTrue(ev.effective_all_day, "没核实到时间就保持全天")
        self.assertEqual(ev.effective_location, "苏州奥体中心体育馆")

    def test_ics_uses_verified_time_and_keeps_uid(self):
        from inftime.ics import build_calendar

        ev = self._event()
        fp_before = ev.fingerprint
        ev.verified = {"start": "2026-10-31T19:30:00+08:00",
                       "location": "苏州奥体中心体育馆", "source": "大麦网"}
        text = build_calendar([ev])

        self.assertIn("DTSTART:20261031T113000Z", text,
                      "ICS 必须用核实值（19:30+08:00 = 11:30Z），不能还是全天")
        self.assertIn("LOCATION:苏州奥体中心体育馆", text)
        self.assertIn("BEGIN:VALARM", text, "核实出具体时刻后应该有到点提醒")
        self.assertIn(f"UID:{fp_before}-inftime@inftime.local", text,
                      "UID 不能跟着核实值变 —— 否则日历里会多出一条而不是更新")

    def test_store_roundtrip_and_old_db_migration(self):
        import sqlite3
        from inftime.store import _row_to_event

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"

            # 先手工建一张**没有 verified 列**的旧表，模拟用户已有的库
            con = sqlite3.connect(db)
            con.executescript("""
                CREATE TABLE events (
                    id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, title TEXT,
                    start TEXT, end TEXT, all_day INTEGER, location TEXT,
                    participants TEXT, source_channel TEXT, source_ref TEXT,
                    raw_text TEXT, is_deadline INTEGER, time_granularity TEXT,
                    priority INTEGER, confidence REAL, needs_clarification INTEGER,
                    clarify_question TEXT, status TEXT, extractor TEXT, notes TEXT,
                    created_at TEXT, updated_at TEXT);
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            """)
            con.commit()
            con.close()

            store = Store(db)          # 打开时应自动补列，不能报错
            ev = self._event()
            store.add(ev)
            ev.verified = {"start": "2026-10-31T19:30:00+08:00", "source": "大麦网"}
            got = store.set_verified(ev.id, ev.verified)

            self.assertIsNotNone(got)
            self.assertTrue(got.is_verified)
            self.assertEqual(got.effective_start, "2026-10-31T19:30:00+08:00")
            self.assertEqual(got.effective_location, "苏州",
                             "没核实到地点时回落到原始值")
            # 坏 JSON 不能让整行读不出来
            store.conn.execute("UPDATE events SET verified = ? WHERE id = ?",
                               ("{不是json", ev.id))
            store.conn.commit()
            row = store.conn.execute(
                "SELECT * FROM events WHERE id = ?", (ev.id,)).fetchone()
            self.assertIsNone(_row_to_event(row).verified)
            store.close()

    def test_enrich_apply_requires_source(self):
        """核实值必须带出处 —— 没有出处的「核实」和模型幻觉没有区别。"""
        import argparse
        from contextlib import redirect_stdout
        import io
        from unittest import mock

        from inftime import cli

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            ev = self._event()
            store.add(ev)
            buf = io.StringIO()
            with mock.patch.object(cli, "_store", lambda: store), \
                 redirect_stdout(buf):
                rc = cli.cmd_enrich(argparse.Namespace(
                    list=False, all=False, json=False, apply=ev.id,
                    start="2026-10-31T19:30:00+08:00", end=None,
                    location=None, title=None, source=None, clear=None))
            self.assertEqual(rc, 1, "缺 --source 必须失败")
            self.assertIn("必须带 --source", buf.getvalue())
            # cmd_enrich 自己会 close 连接，所以要**重新开**一个连接来验证
            check = Store(Path(d) / "t.db")
            self.assertFalse(check.get(ev.id).is_verified,
                             "被拒绝的写入不能留下半截数据")
            check.close()
            store.close()

    def test_enrich_list_flags_missing_time(self):
        import argparse
        from contextlib import redirect_stdout
        import io
        from unittest import mock

        from inftime import cli

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.add(self._event())
            buf = io.StringIO()
            with mock.patch.object(cli, "_store", lambda: store), \
                 redirect_stdout(buf):
                rc = cli.cmd_enrich(argparse.Namespace(
                    list=True, all=False, json=False, apply=None, start=None,
                    end=None, location=None, title=None, source=None, clear=None))
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("具体时间", out)
            self.assertIn("建议检索", out)
            self.assertIn("苏州", out)
            store.close()



class TestCalendarShortcut(unittest.TestCase):
    """快捷指令中转：把事件**全自动**写进日历，绕开「每次点确认」。

    这是「无人值守轮询 → 日历」唯一走得通的路：
    `open -a Calendar` 要人点确认（轮询下会堆积），AppleScript 直写是 -10004。
    快捷指令有正规 app 身份，授权一次之后 `shortcuts run` 就全自动。

    ⚠️ 这里测的是**我们这端**（输入编码 + 防呆）。
    快捷指令里那段 AppleScript 的日期构造已用 osascript 单独验证过；
    `tell application "Calendar"` 那半句只能等用户把快捷指令建好后实测。
    """

    def _event(self, **kw) -> Event:
        base = dict(title="梁静茹演唱会",
                    raw_text="10月31号和姐姐去苏州看梁静茹演唱会",
                    start="2026-10-31T19:30:00+08:00",
                    time_granularity="minute", location="苏州奥体中心体育馆",
                    source_channel="wecom")
        base.update(kw)
        return Event(**base)

    def test_build_input_fields(self):
        from inftime.calendar_shortcut import build_input
        s = build_input(self._event(), alarm_minutes=15, calendar="个人")
        self.assertEqual(
            s, "梁静茹演唱会|2026|10|31|19|30|0|60|苏州奥体中心体育馆|个人",
            "10 段、顺序固定 —— 快捷指令里的 AppleScript 按位置取值")

    def test_build_input_all_day(self):
        from inftime.calendar_shortcut import build_input
        ev = self._event(start="2026-10-31T09:00:00+08:00",
                         all_day=True, time_granularity="day")
        s = build_input(ev, calendar="个人")
        self.assertIn("|1|", s, "全天标记必须是 1")
        self.assertEqual(s.split("|")[5], "0", "全天事件时刻是占位，不应传具体分钟")

    def test_build_input_uses_verified_start(self):
        from inftime.calendar_shortcut import build_input
        ev = self._event(start="2026-10-31T09:00:00+08:00",
                         all_day=True, time_granularity="day")
        ev.verified = {"start": "2026-10-31T19:30:00+08:00", "source": "大麦网"}
        s = build_input(ev, calendar="个人")
        self.assertIn("|19|30|0|", s, "必须用核实后的时间，而不是全天的 09:00 占位")

    def test_build_input_escapes_pipe_in_fields(self):
        from inftime.calendar_shortcut import build_input
        ev = self._event(title="a|b", location="c|d")
        s = build_input(ev)
        self.assertNotIn("a|b", s, "分隔符不能出现在字段内容里")
        self.assertIn("a，b", s)

    def test_build_input_rejects_event_without_time(self):
        from inftime.calendar_shortcut import build_input
        with self.assertRaises(ValueError):
            build_input(self._event(start=None, time_granularity="unknown"))

    def test_auto_calendar_aborts_when_shortcut_missing(self):
        """快捷指令没建好就提前失败，并且**不能**打印「已写入」。"""
        import argparse
        from contextlib import redirect_stdout
        import io
        from unittest import mock

        from inftime import cli
        from inftime import calendar_shortcut as cs

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            store.add(self._event())
            buf = io.StringIO()
            with mock.patch.object(cli, "_store", lambda: store), \
                 mock.patch.object(cs, "shortcut_exists", lambda name: False), \
                 redirect_stdout(buf):
                rc = cli.cmd_auto_calendar(argparse.Namespace(
                    shortcut="Inftime记日程", calendar="个人",
                    alarm=15, newer_than=None, all=False))
            self.assertEqual(rc, 1)
            self.assertIn("找不到快捷指令", buf.getvalue())
            self.assertNotIn("✅", buf.getvalue(), "没写进去就不能打 ✅")
            store.close()

    def test_auto_calendar_skips_already_pushed(self):
        """**核心防重复**：已推过的事件绝不能再推。

        快捷指令路线是裸写日历，没有任何去重。不加这道闸的话，
        每小时轮询会把同一条事件在日历里写 N 遍。
        """
        import argparse
        from contextlib import redirect_stdout
        import io
        from unittest import mock

        from inftime import cli
        from inftime import calendar_shortcut as cs

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            ev = self._event()
            store.add(ev)
            store.set_pushed(ev.id)          # 模拟「上一轮已经推过了」

            called: list = []
            buf = io.StringIO()
            with mock.patch.object(cli, "_store", lambda: store), \
                 mock.patch.object(cs, "shortcut_exists", lambda name: True), \
                 mock.patch.object(cs, "push_via_shortcut",
                                   lambda evs, **k: called.append(list(evs))), \
                 redirect_stdout(buf):
                rc = cli.cmd_auto_calendar(argparse.Namespace(
                    shortcut="Inftime记日程", calendar="个人",
                    alarm=15, newer_than=None, all=False))
            self.assertEqual(rc, 0)
            self.assertEqual(called, [], "已推过的不该再推，否则日历堆重复")
            self.assertIn("没有要写进日历", buf.getvalue())
            store.close()

    def test_auto_calendar_marks_pushed_only_on_success(self):
        """只有真写成功的才标记 pushed_at；失败的下一轮还要再试。"""
        import argparse
        from contextlib import redirect_stdout
        import io
        from unittest import mock

        from inftime import cli
        from inftime import calendar_shortcut as cs

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            good = self._event(title="好的", raw_text="成功的那条消息")
            bad = self._event(title="坏的", raw_text="失败的那条消息")
            store.add(good)
            store.add(bad)

            results = [cs.ShortcutResult(good, True, ""),
                       cs.ShortcutResult(bad, False, "快捷指令超时")]
            buf = io.StringIO()
            with mock.patch.object(cli, "_store", lambda: store), \
                 mock.patch.object(cs, "shortcut_exists", lambda name: True), \
                 mock.patch.object(cs, "push_via_shortcut", lambda evs, **k: results), \
                 redirect_stdout(buf):
                rc = cli.cmd_auto_calendar(argparse.Namespace(
                    shortcut="Inftime记日程", calendar="个人",
                    alarm=15, newer_than=None, all=False))
            self.assertEqual(rc, 3, "有失败就不能装作全部成功")
            # cmd_auto_calendar 结束时会关连接，断言前重新开一个
            check = Store(Path(d) / "t.db")
            self.assertIsNotNone(check.get(good.id).pushed_at)
            self.assertIsNone(check.get(bad.id).pushed_at,
                              "失败的不能标记，否则永远没机会重试")
            check.close()
            store.close()



class TestWeComBridgePush(unittest.TestCase):
    """`--push` 一条龙：读群 → 抽取 → 落库 → 进系统日历。

    这里要守住的契约只有一个，而且很关键：
    **只推「本轮新增」的事件，绝不把整个日程库重推一遍。**
    否则每同步一次，日历确认框里就会重新冒出全部历史事件，
    用户点「添加」点到手软 —— 功能越用越烦。
    """

    def test_on_create_fires_only_for_new_events(self):
        from inftime.wecom_bridge import render

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            msgs = [{"chat_name": "项目组", "sender": "李工",
                     "preview": "本周五下午两点前把材料发我"}]

            first: list = []
            render(msgs, store=store, use_llm=False, on_create=first.append)
            self.assertEqual(len(first), 1, "第一次应回调 1 条新事件")
            self.assertEqual(first[0].source_channel, "wecom")

            second: list = []
            _, again = render(msgs, store=store, use_llm=False,
                              on_create=second.append)
            self.assertEqual(again, 0)
            self.assertEqual(second, [], "重复消息（fingerprint 命中）不该回调")
            store.close()

    def test_push_implies_commit_and_pushes_only_new(self):
        from unittest import mock

        from inftime import wecom_bridge as wb

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            msgs = [{"chat_name": "项目组", "sender": "李工",
                     "preview": "本周五下午两点前把材料发我"}]
            pushed: list = []
            seen_peek: list = []

            def fake_fetch(hours, peek):
                seen_peek.append(peek)
                return msgs

            # ⚠️ 必须带 --no-llm：单测不能真去打大模型（慢 + 依赖网络）。
            argv = ["inftime.wecom_bridge", "--push", "--no-llm"]
            with mock.patch.object(wb, "preflight", lambda: None), \
                 mock.patch.object(wb, "fetch_messages", fake_fetch), \
                 mock.patch.object(wb, "Store", lambda: store), \
                 mock.patch.object(wb, "_push_new",
                                   lambda evs, alarm: pushed.append((list(evs), alarm))), \
                 mock.patch.object(sys, "argv", argv):
                rc = wb.main()

            self.assertEqual(rc, 0)
            self.assertEqual(seen_peek, [False],
                             "--push 必须隐含落库：peek=False，否则游标不推进，"
                             "同一条消息会被反复抽出来")
            self.assertEqual(len(pushed), 1, "--push 应该触发一次日历投递")
            evs, alarm = pushed[0]
            self.assertEqual(len(evs), 1, "只应推本轮新增的那 1 条")
            self.assertEqual(alarm, 15)
            store.close()

    def test_push_without_new_events_does_not_open_calendar(self):
        """库里已有同一条时，--push 不该再弹确认框打扰用户。"""
        from unittest import mock

        from inftime import wecom_bridge as wb

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            msgs = [{"chat_name": "项目组", "sender": "李工",
                     "preview": "本周五下午两点前把材料发我"}]
            # 先落一次，模拟「这条之前已经记过了」
            wb.render(msgs, store=store, use_llm=False)

            pushed: list = []
            argv = ["inftime.wecom_bridge", "--push", "--no-llm"]
            with mock.patch.object(wb, "preflight", lambda: None), \
                 mock.patch.object(wb, "fetch_messages",
                                   lambda hours, peek: msgs), \
                 mock.patch.object(wb, "Store", lambda: store), \
                 mock.patch.object(wb, "_push_new",
                                   lambda evs, alarm: pushed.append(evs)), \
                 mock.patch.object(sys, "argv", argv):
                rc = wb.main()

            self.assertEqual(rc, 0)
            self.assertEqual(pushed, [], "没有新增事件时不该弹日历")
            store.close()


class TestMakeShortcut(unittest.TestCase):
    """make_shortcut 生成的 .shortcut 必须可导入且不再依赖「输入方式」菜单。"""

    def test_workflow_structure(self):
        import plistlib

        from inftime import make_shortcut as ms

        wf = ms.build_workflow()
        actions = wf["WFWorkflowActions"]
        self.assertEqual(len(actions), 1, "只该有一个动作：Run Shell Script")
        params = actions[0]["WFWorkflowActionParameters"]
        self.assertEqual(actions[0]["WFWorkflowActionIdentifier"],
                         "is.workflow.actions.runshellscript")
        # 输入方式写死 stdin（shell 天然模式），彻底绕开 Run AppleScript 的菜单
        self.assertEqual(params["InputMode"], "to stdin")
        self.assertEqual(params["Shell"], "/bin/zsh")
        # 「输入」参数必须显式绑定到 ShortcutInput —— 不绑定则导入后收不到任何输入
        binding = params["Input"]
        self.assertEqual(binding["WFSerializationType"], "WFTextTokenAttachment")
        self.assertEqual(binding["Value"]["Type"], "Variable")
        self.assertEqual(binding["Value"]["VariableName"], "ShortcutInput")
        # 结构能被 plistlib 原样往返（即合法 plist）
        blob = plistlib.dumps(wf, fmt=plistlib.FMT_XML)
        self.assertEqual(plistlib.loads(blob), wf)

    def test_shortcut_script_is_thin_shim(self):
        """快捷指令脚本必须是薄壳：只调 write_event.sh，不含任何逻辑。
        （架构决定 2026-10-10：导入是高摩擦操作，快捷指令永不再改；
        逻辑放磁盘文件由 CLI 自动安装。）"""
        from inftime import make_shortcut as ms

        s = ms.build_shell_script()
        self.assertIn("write_event.sh", s)
        self.assertIn("zsh \"$RUNNER\"", s)
        self.assertIn("还没有初始化", s, "执行脚本缺失时要有中文指引")
        # 薄壳里不该出现业务逻辑
        self.assertNotIn("osascript", s)
        self.assertNotIn("Calendar", s)

    def test_logic_script_has_full_pipeline(self):
        """执行脚本（~/.inftime/write_event.sh 的内容）必须含完整管路。"""
        from inftime import make_shortcut as ms

        s = ms.build_logic_script()
        # 来料优先级：参数（文件路径/文本）→ stdin → CLI 约定文件
        self.assertIn('if [ $# -gt 0 ]', s)
        self.assertIn('[ -f "$1" ]', s)
        self.assertIn("INFTIME_PAYLOAD", s)
        self.assertIn("shortcut_input.txt", s)
        # 日历未运行先拉起（坑：tell 未运行的 Calendar 报 -600）
        self.assertIn("pgrep -x Calendar", s)
        self.assertIn("open -a Calendar", s)
        # AppleScript 适配成 argv 解包 + 段数防呆 + heredoc 完整
        self.assertIn("on run argv", s)
        self.assertIn("item 1 of argv", s)
        self.assertNotIn("on run {input, parameters}", s, "旧解包方式必须替换掉")
        self.assertIn("is not 10", s)
        self.assertIn("没有收到输入", s)
        self.assertIn("<<'AS_END'", s)
        self.assertTrue(s.rstrip().endswith("AS_END"))
        # 覆盖警告：防止用户手改后被下次运行静默覆盖
        self.assertIn("覆盖", s)
        # AppleScript 适配成 argv 解包（osascript CLI 与 Shortcuts 解包方式不同）
        self.assertIn("on run argv", s)
        self.assertIn("item 1 of argv", s)
        self.assertNotIn("on run {input, parameters}", s, "旧解包方式必须替换掉")
        # 段数防呆
        self.assertIn("is not 10", s)
        self.assertIn("输入格式不对", s)
        # heredoc 定界符必须存在，否则 osascript 读不到脚本
        self.assertIn("<<'AS_END'", s)
        self.assertTrue(s.rstrip().endswith("AS_END"))


    def test_run_shortcut_uses_payload_file(self):
        """坑（2026-10-10，实测两次）：`shortcuts run` 不支持管道 stdin，
        `-i` 也送不进动作（含显式 ShortcutInput 绑定）。
        run_shortcut 必须走约定文件：写 → run → 用完即删（含异常路径）。
        ⚠️ 测试必须把 INFTIME_HOME 指到临时目录（坑 24），不许碰真实 ~/.inftime。"""
        import os
        import tempfile as tf
        from unittest import mock

        from inftime import calendar_shortcut as cs

        recorded: list = []

        def fake_run(argv, **kwargs):
            recorded.append(argv)
            self.assertTrue(cs.payload_path().exists(),
                            "调用时约定文件必须存在")
            with open(cs.payload_path()) as f:
                self.assertEqual(f.read(), "标题|2026|10|31|19|30|0|60|地点|个人")
            r = mock.Mock()
            r.returncode = 0
            r.stdout = "ok"
            r.stderr = ""
            return r

        with tf.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"INFTIME_HOME": d}):
            with mock.patch.object(cs.subprocess, "run", fake_run):
                ok, detail = cs.run_shortcut("标题|2026|10|31|19|30|0|60|地点|个人",
                                             name="Inftime记日程")

        self.assertTrue(ok)
        self.assertEqual(recorded[0], ["shortcuts", "run", "Inftime记日程"],
                         "不该再依赖 stdin/-i，只 run 名字")
        # 用完即删
        self.assertFalse(cs.payload_path().exists(),
                         "约定文件必须在调用后删除")

    def test_run_shortcut_cleans_payload_on_error(self):
        """即使 shortcuts run 抛异常（如超时），约定文件也不能残留 ——
        否则下次手动运行快捷指令会拿旧数据写错日程。"""
        import os
        import tempfile as tf
        from unittest import mock

        from inftime import calendar_shortcut as cs

        def boom(argv, **kwargs):
            raise cs.subprocess.TimeoutExpired(argv, 60)

        with tf.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"INFTIME_HOME": d}):
            with mock.patch.object(cs.subprocess, "run", boom):
                with self.assertRaises(cs.subprocess.TimeoutExpired):
                    cs.run_shortcut("x|1")
            self.assertFalse(cs.payload_path().exists(),
                             "异常路径也不能残留约定文件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
