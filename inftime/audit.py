"""
承诺审计 —— 复活的第 0 步，也是唯一能防止自嗨的一步。

思路：先不写产品，先拿真实消息量一遍——
「过去这 30 天里，我到底答应过多少件事，其中多少件后来忘了？」

用法：
    inftime audit 我的聊天记录.txt --days 30 --out 审计报告.md

输入格式很随意：一行一条消息，或者整段粘贴。空行忽略。
也可以带来源标记，形如 `[项目群] 张三: 明天下午两点开会`，工具会自动解析。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .extract import extract, split_source_prefix
from .schema import Event

_SRC = re.compile(r"^\[(?P<src>[^\]]{1,40})\]\s*(?:(?P<who>[^:：]{1,20})[:：]\s*)?(?P<msg>.+)$")


@dataclass
class AuditResult:
    scanned: int = 0
    events: list[Event] = field(default_factory=list)
    parsed_sources: dict[str, int] = field(default_factory=dict)
    generated_at: str = ""
    window_days: int = 30

    # ---------- 指标 ----------

    @property
    def total(self) -> int:
        return len(self.events)

    @property
    def with_time(self) -> list[Event]:
        return [e for e in self.events if e.time_granularity != "unknown"]

    @property
    def no_time(self) -> list[Event]:
        return [e for e in self.events if e.time_granularity == "unknown"]

    @property
    def vague(self) -> list[Event]:
        """时间精度粗到只剩「天」或「半天」——这类最容易忘。"""
        return [e for e in self.events if e.time_granularity in ("day", "half_day")]

    @property
    def urgent(self) -> list[Event]:
        return [e for e in self.events if e.priority >= 4]

    def detection_rate(self) -> float:
        return self.total / self.scanned if self.scanned else 0.0

    def timed_rate(self) -> float:
        return len(self.with_time) / self.total if self.total else 0.0


# ---------------------------------------------------------------- 主流程

def run_audit(
    text: str,
    *,
    window_days: int = 30,
    now: Optional[datetime] = None,
    use_llm: bool = True,
    tag_incomplete: bool = True,
) -> AuditResult:
    now = now or datetime.now().astimezone()
    res = AuditResult(window_days=window_days,
                      generated_at=now.isoformat(timespec="seconds"))

    lines = [l.strip() for l in (text or "").splitlines()]
    lines = [l for l in lines if l and not l.startswith("#")]

    for line in lines:
        res.scanned += 1
        src, who, msg = _parse_line(line)
        ref = " / ".join(x for x in (src, who) if x) or None
        if src:
            res.parsed_sources[src] = res.parsed_sources.get(src, 0) + 1
        try:
            evs = extract(msg, now=now, source_channel="audit", source_ref=ref, use_llm=use_llm)
        except Exception:          # 审计工具不该因为单条脏数据整体崩掉
            continue
        for e in evs:
            if tag_incomplete:
                e.status = "pending"
            res.events.append(e)

    return res


def _parse_line(line: str) -> tuple[Optional[str], Optional[str], str]:
    return split_source_prefix(line)


# ---------------------------------------------------------------- 报告

def render_markdown(res: AuditResult, *, now: Optional[datetime] = None) -> str:
    now = now or datetime.now().astimezone()
    n = res.total
    lines: list[str] = []
    A = lines.append

    A(f"# 承诺审计报告 · 最近 {res.window_days} 天")
    A("")
    A(f"生成时间：{now.strftime('%Y-%m-%d %H:%M')}　　扫描消息：{res.scanned} 条　　"
      f"识别出的「事」：{n} 条")
    A("")

    A("## 一、关键数字")
    A("")
    A("| 指标 | 数值 | 说明 |")
    A("| :--- | ---: | :--- |")
    A(f"| 扫描消息总数 | {res.scanned} | 你粘贴进来的原始行数 |")
    A(f"| 识别出的事件 | {n} | 每 {1/res.detection_rate():.1f} 条消息里就有 1 件要办的事 |"
      if res.detection_rate() else "| 识别出的事件 | 0 | |")
    A(f"| 有明确时间 | {len(res.with_time)} | 占比 {res.timed_rate()*100:.0f}%，这部分能直接进日历 |")
    A(f"| **完全没有时间** | **{len(res.no_time)}** | **占比 {(len(res.no_time)/n*100 if n else 0):.0f}%"
      "，这是最危险的一类——它们会消失** |")
    A(f"| 时间很模糊 | {len(res.vague)} | 只说了「本周」「下午」，同样容易漏 |")
    A(f"| 高优先级 | {len(res.urgent)} | 判定为紧急或临近截止 |")
    A("")

    A("## 二、需要你人工勾选的清单")
    A("")
    A("下面每条，请在你**事后确实忘了做**的那条前面打 `x`。")
    A("打完标记后，把这个文件发回来，我用来算「遗忘率」——那才是这个项目真正的验收数据。")
    A("")
    A("| 忘了吗 | 日期 | 事 | 来源 | 优先级 | 置信度 | 原文 |")
    A("| :---: | :--- | :--- | :--- | :---: | ---: | :--- |")
    for e in sorted(res.events, key=lambda x: (x.start or "9999", -x.priority)):
        day = (e.start or "")[:10] or "—"
        note = f"⚠ {e.clarify_question}" if e.needs_clarification else ""
        raw = (e.raw_text or "").replace("|", "\\|")[:60]
        A(f"| [ ] | {day} | {_md(e.title)}{('　' + note) if note else ''} | "
          f"{_md(e.source_ref or '—')} | {e.priority} | {e.confidence:.2f} | {raw} |")
    A("")

    if res.no_time:
        A("## 三、没抓到时间的那些（重点看这里）")
        A("")
        A("这些是消息里你答应了但没定时间的事。人的记忆不会替你保存它们。")
        A("")
        for e in res.no_time:
            A(f"- {_md(e.title)}　<span>来源：{_md(e.source_ref or '—')}</span>")
        A("")

    if res.parsed_sources:
        A("## 四、来源分布")
        A("")
        A("| 来源 | 条数 |")
        A("| :--- | ---: |")
        for k, v in sorted(res.parsed_sources.items(), key=lambda kv: -kv[1]):
            A(f"| {_md(k)} | {v} |")
        A("")

    A("---")
    A("")
    A("## 五、这份报告有什么用")
    A("")
    A("如果「完全没有时间」那一栏占比很高，说明你要的不是日历，是**待办捕获**——")
    A("先把东西收进来，别让它消失。")
    A("")
    A("如果「时间很模糊」占比高，说明你需要的是**排期决策**——帮你把「本周内」落到具体某天。")
    A("")
    A("如果识别出的事件总数本来就很少（比如 30 天不到 20 件），那说明这个痛点对你不够强，")
    A("这个项目值得重新考虑——**这是好事，省下了三周**。")
    A("")
    A(f"> 由 Inftime 生成 · 窗口 {res.window_days} 天 · 全部处理在本机完成，未上传任何数据")
    A("")
    return "\n".join(lines)


def _md(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()
