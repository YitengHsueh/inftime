"""
ICS 生成（RFC 5545）—— 手写，零依赖。

为什么不用 icalendar 库：MCP Server 的分发体验是产品的一部分。
用户 clone 下来 `python -m inftime.mcp_server` 就能跑，不需要先 pip install 任何东西。

正确性要点（这几条错了会导致日历导入失败或乱码）：
  · 折行：每行不得超过 75 个**字节**（不是字符），续行以空格开头
  · 转义：\\  ;  ,  换行 四个字符必须转义
  · 换行：CRLF
  · 全天事件用 VALUE=DATE，且 DTEND 是「次日」
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from .schema import Event

PRODID = "-//Inftime//Time Assistant//CN"
CAL_NAME = "Inftime"


# ---------------------------------------------------------------- 基础

def _esc(text: str) -> str:
    if text is None:
        return ""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """按 75 字节折行，续行前加一个空格。"""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out: list[bytes] = []
    cur = b""
    for ch in line:
        b = ch.encode("utf-8")
        limit = 75 if not out else 74   # 续行要留一个字节给前导空格
        if len(cur) + len(b) > limit:
            out.append(cur)
            cur = b
        else:
            cur += b
    if cur:
        out.append(cur)
    return "\r\n ".join(seg.decode("utf-8") for seg in out)


def _utc(dt: datetime) -> str:
    """转成真正的 UTC 再打 Z 后缀。

    坑（已踩过，后果严重）：`astimezone(tz=None)` 转的是**本地时区**，
    后面再拼一个 "Z" 就等于声称本地时间是 UTC —— 导入日历后会整体偏移 8 小时。
    所有事件都会错，而且悄无声息。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse(dt: Optional[str]) -> Optional[datetime]:
    if not dt:
        return None
    try:
        d = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return d


# ---------------------------------------------------------------- 单事件

def event_to_vevent(ev: Event, *, alarm_minutes: int = 15) -> list[str]:
    lines: list[str] = ["BEGIN:VEVENT"]
    # ⚠️ UID 必须由**确定性**值派生，绝不能含随机数。
    # 原实现是 f"{ev.id}-inftime@{uuid4().hex[:8]}.local" —— 每次生成都不同，
    # 于是同一条日程重复导入日历时会被当成两个不同事件：
    # 「改几条 → 重新导出全部 → 再导入」就会让日程翻倍，而这是很自然的用法。
    # 用 fingerprint（来源渠道 + 原文 + 时间）派生：与模型无关、重放时逐字相同，
    # 所以重复导入是「更新同一条」而不是「新增一条」。
    uid = f"{ev.fingerprint}-inftime@inftime.local"
    lines.append(f"UID:{uid}")
    lines.append(f"DTSTAMP:{_utc(datetime.now())}")

    # ⚠️ 时间/地点一律走 `effective_*`（核实值优先），否则联网核实到的
    # 精确开场时间会在这里被原始值盖掉 —— 那这个功能就白做了。
    # ⚠️ 但 UID 用的是 `fingerprint`（内含**原始** start），刻意不跟着核实值变：
    # 否则每核实一次就换一个 UID，日历里会多出一条而不是更新原条。
    start = _parse(ev.effective_start)
    end = _parse(ev.effective_end)

    if ev.effective_all_day or (start is not None and ev.effective_granularity == "day"):
        d0: date = (start or datetime.now()).date()
        d1 = d0 + timedelta(days=1)
        lines.append(f"DTSTART;VALUE=DATE:{d0.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{d1.strftime('%Y%m%d')}")
    elif start is not None:
        lines.append(f"DTSTART:{_utc(start)}")
        if end and end > start:
            lines.append(f"DTEND:{_utc(end)}")
        else:
            lines.append(f"DTEND:{_utc(start + timedelta(hours=1))}")
        if alarm_minutes > 0:
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"TRIGGER:-PT{alarm_minutes}M",
                f"DESCRIPTION:{_esc(ev.title)}",
                "END:VALARM",
            ]
    else:
        # 没有时间的事件：做成全天，标注「待定」，避免被静默丢掉
        d0 = datetime.now().date()
        lines.append(f"DTSTART;VALUE=DATE:{d0.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{(d0 + timedelta(days=1)).strftime('%Y%m%d')}")

    lines.append(f"SUMMARY:{_esc(('⏰ ' if ev.is_deadline else '') + (ev.title or '未命名'))}")
    loc = ev.effective_location
    if loc:
        lines.append(f"LOCATION:{_esc(loc)}")

    desc_bits = []
    if ev.raw_text:
        desc_bits.append(f"原文：{ev.raw_text.strip()}")
    if ev.source_ref:
        desc_bits.append(f"来源：{ev.source_channel} / {ev.source_ref}")
    if ev.is_verified:
        desc_bits.append(f"已核实：{ev.verified_source or '（未注明来源）'}")
    desc_bits.append(f"由 Inftime 生成 · 置信度 {ev.confidence:.2f} · 抽取器 {ev.extractor}")
    if ev.clarify_question:
        desc_bits.append(f"待澄清：{ev.clarify_question}")
    lines.append(f"DESCRIPTION:{_esc(chr(10).join(desc_bits))}")

    lines.append(f"CATEGORIES:{_esc(ev.source_channel)}")
    lines.append(f"PRIORITY:{max(1, min(9, 10 - ev.priority * 2))}")
    lines.append("TRANSP:OPAQUE")
    lines.append("END:VEVENT")
    return lines


# ---------------------------------------------------------------- 日历

def build_calendar(events: Iterable[Event], *, alarm_minutes: int = 15) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{CAL_NAME}",
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for ev in events:
        lines += event_to_vevent(ev, alarm_minutes=alarm_minutes)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


def write_calendar(events: Iterable[Event], path, *, alarm_minutes: int = 15) -> "object":
    from pathlib import Path

    events = list(events)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_calendar(events, alarm_minutes=alarm_minutes), "utf-8")
    return p


def write_single(ev: Event, path, *, alarm_minutes: int = 15):
    return write_calendar([ev], path, alarm_minutes=alarm_minutes)
