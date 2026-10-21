"""
中文时间表达解析。

这是 Inftime 的地基，也是 2018 年那份 BP 里技术章节（6.3.1）最重的一块：
「今天早上」「上午 8 点 15」「昨天一早」「本周内」「下周三下午」——
人类说话的散乱性，是「消息 → 日程」这条链路里最容易崩的一环。

本模块只用标准库实现，**不依赖任何大模型**。
理由：这是确定性任务，规则比模型更可预测、更快、更省，而且离线可用。
（LLM 只在语义层做增强，不负责算时间——那是最容易出幻觉的地方。）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

# ---------------------------------------------------------------- 基础工具

CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2, "三": 3, "叁": 3,
            "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7, "柒": 7,
            "八": 8, "捌": 8, "九": 9, "玖": 9}

# 时段词 -> (起始小时, 结束小时)，None 表示跨零点
MERIDIEM = {
    "凌晨": (0, 5), "清晨": (5, 7), "一早": (6, 9), "早上": (6, 11), "早晨": (6, 11),
    "上午": (8, 12), "中午": (11, 13), "正午": (12, 12), "晌午": (11, 13),
    "下午": (13, 18), "午后": (13, 17), "傍晚": (17, 19), "晚上": (18, 23),
    "夜里": (20, 23), "夜间": (20, 23), "半夜": (23, 2),
}

WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

REL_DAY = {
    "大前天": -3, "前天": -2, "昨天": -1, "昨日": -1, "昨晚": -1,
    "今": 0, "今天": 0, "今日": 0, "本日": 0, "当天": 0, "今晚": 0, "今早": 0, "今晨": 0,
    "明日": 1, "明天": 1, "明": 1, "明晚": 1, "明早": 1,
    "后日": 2, "后天": 2, "大后天": 3,
}

# 「七点到九点」这类区间
_TIME_RANGE = re.compile(
    r"(\d{1,2})\s*[点時时](?:\s*(\d{1,2})\s*分?)?\s*(?:到|至|[-~～])\s*"
    r"(\d{1,2})\s*[点時时](?:\s*(\d{1,2})\s*分?)?"
)

DEADLINE_MARKERS = ("之前", "以前", "前必须", "前完成", "前交", "前提交", "截止", "deadline",
                    "DDL", "最晚", "不得晚于", "不得超过", "截至", "前")

# 全角 -> 半角
_FW = {ord(c): ord(c) - 0xFEE0 for c in "０１２３４５６７８９"}


def _cn2int(s: str) -> Optional[int]:
    """中文数字 / 阿拉伯数字 -> int。支持 1~99 的常见写法。"""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = CN_DIGIT.get(head, 1) if head else 1
        ones = CN_DIGIT.get(tail, 0) if tail else 0
        return tens * 10 + ones
    if len(s) == 1 and s in CN_DIGIT:
        return CN_DIGIT[s]
    return None


def normalize(text: str) -> str:
    """全角转半角；把紧邻时间单位的汉字数字转成阿拉伯数字。"""
    t = text.translate(_FW)
    t = t.replace("：", ":").replace("．", ".").replace("　", " ")

    # 只在与时间单位相邻时做汉字数字替换，避免把「一班」「三句话」误伤
    unit = "点时半刻分秒号月周年天日"
    def repl(m: re.Match) -> str:
        val = _cn2int(m.group(1))
        return f"{val}{m.group(2)}" if val is not None else m.group(0)

    t = re.sub(rf"([零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十]{{1,3}})([{unit}])", repl, t)
    return t


# ---------------------------------------------------------------- 结果对象

@dataclass
class WhenResult:
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    granularity: str = "unknown"     # day / half_day / hour / minute
    all_day: bool = False
    is_deadline: bool = False
    matched: str = ""
    span: tuple[int, int] = (0, 0)
    deadline_matched: str = ""
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.start is not None


# ---------------------------------------------------------------- 主解析

_DATE_ABS = re.compile(
    r"(?:(\d{4})\s*[年\-/])?\s*(\d{1,2})\s*[月\-/]\s*(\d{1,2})\s*[日号]?"
)
_TIME_HM = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})(?!\d)")
_TIME_CN = re.compile(r"(\d{1,2})\s*[点時时]\s*(?:(\d{1,2})\s*分?|(半)|(一刻)|(三刻))?")
_WEEK = re.compile(r"(上上|上|本|这|下下|下)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])")
_PERIOD_END = re.compile(r"(本|这|下|下下)?\s*(周|星期|礼拜)(?:内|末|底|之前|前)")
_MONTH_END = re.compile(r"(本|这|下)?\s*月(?:底|末|内|之前|前)")
# 「本月25日前」「下个月15号」「下个月的房租记得1号前转过来」
_REL_MONTH_DAY = re.compile(
    r"(下下|下|本|这)?\s*(?:个)?\s*月[^\d，。；！？]{0,10}?(\d{1,2})\s*[号日]"
)
_DAYS_LATER = re.compile(r"(\d{1,2})\s*(?:个)?\s*(天|日|周|星期|个?月|小时|钟头)\s*(?:后|以后|之后)")
_IN_MIN = re.compile(r"(\d{1,3})\s*(?:分钟|min)\s*(?:内|以内|之内|后)")


def parse_when(text: str, now: Optional[datetime] = None) -> WhenResult:
    """从一段中文里解析出时间。找不到就返回 granularity='unknown'。"""
    now = now or datetime.now().astimezone()
    tz = now.tzinfo
    t = normalize(text)
    res = WhenResult()

    # ---------- 0. 截止语气 ----------
    for m in DEADLINE_MARKERS:
        if m in t:
            res.is_deadline = True
            res.deadline_matched = m
            break

    # ---------- 1. 相对时长：3 天后 / 2 小时后 / 30 分钟内 ----------
    m = _IN_MIN.search(t)
    if m:
        n = int(m.group(1))
        res.start = now + timedelta(minutes=n)
        res.granularity = "minute"
        res.matched, res.span = m.group(0), m.span()
        return res

    m = _DAYS_LATER.search(t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit in ("天", "日", "周", "星期") or "月" in unit:
            days = n if unit in ("天", "日") else (n * 7 if unit in ("周", "星期") else n * 30)
            base = (now + timedelta(days=days)).replace(
                hour=9, minute=0, second=0, microsecond=0)
            res.start = base
            res.granularity = "day"
            res.all_day = True
        else:
            res.start = (now + timedelta(hours=n)).replace(second=0, microsecond=0)
            res.granularity = "hour"
        res.matched, res.span = m.group(0), m.span()
        return res

    # ---------- 2. 月/周/日 跨度（本周内 / 月底 / 下周末）----------
    m = _PERIOD_END.search(t)
    if m is not None and m.group(2):
        prefix = m.group(1) or "本"
        base = _week_start(now, prefix)
        res.start = (base + timedelta(days=6)).replace(hour=23, minute=59, second=0, microsecond=0)
        if m.group(0).endswith("末"):
            res.start = (base + timedelta(days=5)).replace(hour=23, minute=59)
        res.granularity = "day"
        res.all_day = True
        res.is_deadline = True
        res.matched, res.span = m.group(0), m.span()
        return res

    m = _MONTH_END.search(t)
    if m is not None:
        prefix = m.group(1) or "本"
        y, mo = now.year, now.month
        if prefix == "下":
            mo += 1
            if mo == 13:
                y, mo = y + 1, 1
        last = _last_day_of_month(y, mo)
        res.start = datetime(y, mo, last, 23, 59, tzinfo=tz)
        res.granularity = "day"
        res.all_day = True
        res.is_deadline = True
        res.matched, res.span = m.group(0), m.span()
        return res

    # ---------- 3. 绝对日期 ----------
    date_part: Optional[datetime] = None
    m = _DATE_ABS.search(t)
    if m:
        try:
            y = int(m.group(1)) if m.group(1) else now.year
            mo, d = int(m.group(2)), int(m.group(3))
            if not m.group(1) and (mo, d) < (now.month, now.day):
                y += 1  # 「1月3日」在 10 月说出来，指的是明年
            date_part = datetime(y, mo, d, tzinfo=tz)
            res.matched, res.span = m.group(0), m.span()
        except ValueError:
            date_part = None

    # ---------- 4. 星期 ----------
    if date_part is None:
        m = _REL_MONTH_DAY.search(t)
        if m:
            prefix, day = m.group(1) or "本", int(m.group(2))
            y, mo = now.year, now.month
            if prefix in ("下", "下下"):
                mo += 2 if prefix == "下下" else 1
            while mo > 12:
                mo -= 12
                y += 1
            if day <= _last_day_of_month(y, mo):
                try:
                    date_part = datetime(y, mo, day, tzinfo=tz)
                    res.matched, res.span = m.group(0), m.span()
                except ValueError:
                    date_part = None

    if date_part is None:
        m = _WEEK.search(t)
        if m:
            prefix = m.group(1) or ""
            wd = WEEKDAY_CN.get(m.group(2))
            if wd is not None:
                # 以「周一」为锚点计算，而不是以今天为锚点
                monday = datetime(now.year, now.month, now.day, tzinfo=tz) - timedelta(
                    days=now.weekday()
                )
                if prefix in ("下", "下下"):
                    monday += timedelta(weeks=2 if prefix == "下下" else 1)
                elif prefix in ("上", "上上"):
                    monday -= timedelta(weeks=2 if prefix == "上上" else 1)
                date_part = monday + timedelta(days=wd)
                # 「本周五」在周六说出来，人指的其实是下一个周五。
                # Inftime 是前瞻性工具，一律向前取，不返回已过去的日子。
                if prefix in ("", "本", "这") and date_part.date() < now.date():
                    date_part += timedelta(days=7)
                res.matched, res.span = m.group(0), m.span()

    # ---------- 5. 相对日 ----------
    if date_part is None:
        # 长词优先，避免「今天」被「今」抢先
        for word in sorted(REL_DAY, key=len, reverse=True):
            if word in t:
                idx = t.index(word)
                date_part = datetime(now.year, now.month, now.day, tzinfo=tz) + timedelta(
                    days=REL_DAY[word]
                )
                res.matched, res.span = word, (idx, idx + len(word))
                break

    # ---------- 6. 时刻 ----------
    hour: Optional[int] = None
    minute = 0
    time_gran = None
    time_span = None
    time_from_cn = False
    mm = _TIME_HM.search(t)
    if mm:
        h, mi = int(mm.group(1)), int(mm.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            hour, minute = h, mi
            time_gran = "minute"
            time_span = mm.span()
    if hour is None:
        mm = _TIME_CN.search(t)
        if mm:
            time_from_cn = True
            h = int(mm.group(1))
            if mm.group(3):          # X点半
                minute, time_gran = 30, "minute"
            elif mm.group(4):        # X点一刻
                minute, time_gran = 15, "minute"
            elif mm.group(5):        # X点三刻
                minute, time_gran = 45, "minute"
            elif mm.group(2):
                minute, time_gran = int(mm.group(2)), "minute"
            else:
                minute, time_gran = 0, "hour"
            hour = h
            time_span = mm.span()

    # ---------- 7. 时段词 ----------
    meridiem = None
    for word in sorted(MERIDIEM, key=len, reverse=True):
        if word in t:
            meridiem = word
            break
    # 单字「晚 / 早」紧贴在时刻前面时也算时段词：「下周一晚七点」= 19:00
    if meridiem is None and time_span and time_span[0] > 0:
        prev = t[time_span[0] - 1]
        if prev in "晚夜":
            meridiem = "晚上"
        elif prev in "早晨":
            meridiem = "早上"

    # ---------- 8. 合并 ----------
    if date_part is not None and hour is None and meridiem:
        lo, hi = MERIDIEM[meridiem]
        hour = 9 if lo <= 9 <= hi else lo
        minute = 0
        time_gran = "half_day"

    if date_part is not None and hour is None:
        res.start = date_part.replace(hour=9, minute=0, second=0, microsecond=0)
        res.all_day = True
        res.granularity = "day"
        if not res.matched:
            res.matched = ""
        return res

    if date_part is None and hour is not None:
        # 只有时刻没有日期：今天或明天（已过则顺延）
        date_part = datetime(now.year, now.month, now.day, tzinfo=tz)

    if date_part is not None and hour is not None:
        # 12 小时制修正
        if meridiem and hour <= 12:
            lo, _ = MERIDIEM[meridiem]
            if meridiem in ("下午", "午后", "傍晚", "晚上", "夜里", "夜间") and hour < 12:
                hour += 12
            elif meridiem in ("凌晨", "半夜") and hour == 12:
                hour = 0
            elif meridiem in ("上午", "早上", "早晨", "清晨") and hour == 12:
                hour = 0
        elif not meridiem and time_from_cn and 1 <= hour <= 6:
            # 口语里「三点」「六点」在白天场景几乎都指下午。
            # 这是启发式，会在事件上标 needs_clarification，让用户一句话确认。
            hour += 12
            res.note = f"「{hour - 12}点」未指明上下午，已按下午处理（可一句话纠正）"
        elif not meridiem and time_from_cn and 7 <= hour <= 11:
            res.note = f"「{hour}点」未指明上下午，已按上午处理（可一句话纠正）"

        try:
            start = date_part.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
        except ValueError:
            return res

        if start < now and date_part.date() == now.date() and res.matched == "":
            start += timedelta(days=1)   # 只有时刻且已过 -> 明天

        res.start = start
        # 区间结束时间：「七点到九点」
        rm = _TIME_RANGE.search(t)
        if rm:
            end_h = int(rm.group(3))
            end_m = int(rm.group(4)) if rm.group(4) else 0
            if meridiem and end_h <= 12 and meridiem in ("下午", "午后", "傍晚", "晚上", "夜里"):
                if end_h < 12:
                    end_h += 12
            elif not meridiem and 1 <= end_h <= 6:
                end_h += 12
            try:
                res.end = start.replace(hour=end_h % 24, minute=end_m)
                if res.end <= start:
                    res.end = None
            except ValueError:
                res.end = None
        if res.end is None and time_gran == "minute":
            res.end = start + timedelta(hours=1)
        res.granularity = time_gran or "hour"
        res.all_day = False

        if time_span:
            res.span = time_span
        return res

    return res


# ---------------------------------------------------------------- 辅助

def _week_start(now: datetime, prefix: str) -> datetime:
    """返回该前缀对应周的周一 00:00。"""
    monday = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo) - timedelta(
        days=now.weekday()
    )
    if prefix in ("下", "下下"):
        monday += timedelta(weeks=2 if prefix == "下下" else 1)
    return monday


def _last_day_of_month(y: int, m: int) -> int:
    if m == 12:
        nxt = datetime(y + 1, 1, 1)
    else:
        nxt = datetime(y, m + 1, 1)
    return (nxt - timedelta(days=1)).day


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(s for s in spans if s[1] > s[0]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def strip_time_expressions(text: str, results: list[WhenResult]) -> str:
    """把命中的时间表达从原文里抠掉，剩下的更接近「事情本身」。

    坑（已踩过）：区间必须**先合并、再基于原始字符串一次性切除**。
    早期版本按倒序逐个删除，删第一个之后后面所有下标都错位，
    「周五晚上七点到九点在东方艺术中心」被切成了「方艺术中心」——
    把「东」这个字吃掉了。
    """
    t = normalize(text)
    spans = merge_spans([r.span for r in results if r.matched and r.span != (0, 0)])
    if not spans:
        return t.strip()
    parts: list[str] = []
    prev = 0
    for a, b in spans:
        if 0 <= a < b <= len(t):
            parts.append(t[prev:a])
            prev = b
    parts.append(t[prev:])
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def collect_spans(text: str, extra: Optional[WhenResult] = None,
                  now: Optional[datetime] = None) -> list[WhenResult]:
    """收集一段文本里全部时间表达的 span，供标题清洗使用。"""
    out = scan_all(text, now)
    if extra is not None and extra.matched and extra.span != (0, 0):
        out.append(extra)
    t = normalize(text)
    # 时段词本身也算，否则「本周五下午」里的「下午」会残留
    for word in MERIDIEM:
        i = t.find(word)
        if i >= 0:
            out.append(WhenResult(matched=word, span=(i, i + len(word))))
    # 把「两点前」的「前」也包进来，否则标题里会留下孤零零一个「前」
    for r in out:
        r.span = _extend_over_modifiers(t, r.span)
    return out


# 时间表达后面紧跟的修饰词，应当与时间一起被抠掉
_TRAIL_MODS = ("之前", "之后", "以前", "以后", "以内", "之内", "左右", "前后",
               "前", "后", "内")


def _extend_over_modifiers(t: str, span: tuple[int, int]) -> tuple[int, int]:
    a, b = span
    # 向右吞掉「前 / 之后 / 以内」这类修饰
    while b < len(t):
        for m in _TRAIL_MODS:
            if t.startswith(m, b):
                b += len(m)
                break
        else:
            break
    # 向左吞掉紧贴的单字时段词：「下周一晚7点」里的「晚」要跟着一起走，
    # 否则标题里会留下孤零零一个「晚」。
    if a > 0 and t[a - 1] in "晚早夜晨":
        a -= 1
    return (a, b)


def scan_all(text: str, now: Optional[datetime] = None) -> list[WhenResult]:
    """扫描一段文本中的全部时间表达（用于审计与调试）。"""
    now = now or datetime.now().astimezone()
    out: list[WhenResult] = []
    t = normalize(text)
    # 简化处理：对每个正则独立扫描，收集命中
    for rx, gran, allday in (
        (_DATE_ABS, "day", True),
        (_TIME_RANGE, "minute", False),
        (_TIME_HM, "minute", False),
        (_TIME_CN, "hour", False),
        (_WEEK, "day", True),
        (_PERIOD_END, "day", True),
        (_MONTH_END, "day", True),
        (_DAYS_LATER, "day", False),
        (_IN_MIN, "minute", False),
    ):
        for m in rx.finditer(t):
            r = WhenResult(matched=m.group(0), span=m.span())
            one = parse_when(m.group(0), now)
            r.start, r.granularity, r.all_day = one.start, one.granularity, one.all_day
            out.append(r)
    for word, off in REL_DAY.items():
        if word in t:
            i = t.index(word)
            r = WhenResult(matched=word, span=(i, i + len(word)), granularity="day", all_day=True)
            r.start = datetime(now.year, now.month, now.day, 9, 0, tzinfo=now.tzinfo) + timedelta(days=off)
            out.append(r)
    out.sort(key=lambda r: r.span[0])
    return out
