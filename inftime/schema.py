"""
Event —— Inftime 的核心数据结构。

沿用 2018 年那份 BP 里的 5W1H 框架，但把它落到一个可序列化、可校验的 schema 上：
    When  -> start / end / all_day / is_deadline / time_granularity
    Where -> location
    Who   -> participants / sender
    What  -> title
    How   -> priority / notes
外加一个现代字段：confidence + needs_clarification，
因为「拿不准的时候宁可问一句，也不要静默地建错日程」。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

# 时间精度：从粗到细。用于决定「是建一个具体日程，还是只丢进待办池」
GRANULARITY = ("unknown", "day", "half_day", "hour", "minute")

STATUS = ("pending", "confirmed", "ignored", "done")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Event:
    """一条被抽取出来的、可能值得放进日历的事件。"""

    title: str = ""
    start: Optional[str] = None          # ISO8601 带时区；无明确时间时为 None
    end: Optional[str] = None
    all_day: bool = False
    location: Optional[str] = None
    participants: list[str] = field(default_factory=list)

    # 来源溯源 —— 每条事件都必须能回答「你凭什么认为这是件事」
    source_channel: str = "manual"        # wechat / mail / clipboard / screenshot / manual
    source_ref: Optional[str] = None      # 群名、发件人、文件名
    raw_text: str = ""

    # 判断类字段
    is_deadline: bool = False             # 「14 点前交」这类，起点=截止点
    time_granularity: str = "unknown"     # 见 GRANULARITY
    priority: int = 3                     # 1..5，越大越紧急
    confidence: float = 0.0               # 0..1
    needs_clarification: bool = False
    clarify_question: Optional[str] = None

    status: str = "pending"
    extractor: str = "rule"               # rule / llm / llm+rule
    notes: str = ""

    # 外部核实结果（联网搜索 / 人工确认）。
    #
    # 为什么不直接改写 start（2026-10-10 的决策）：
    #   `fingerprint` 里**含 `start`**。如果核实后把 start 改掉，同一条群消息
    #   下次再读会算出**新指纹** → 判为新事件 → **重复入库**。
    #   那是坑 11（去重键混进可变字段）的翻版。
    #   所以核实结果单独存，start 永远保持「从消息里读到的原始值」。
    #
    # 形状：
    #   {"start": ISO8601, "end": ISO8601|None, "all_day": bool,
    #    "granularity": str, "location": str, "source": "大麦网 / 检索于 ...",
    #    "at": ISO8601}     # at = 核实时间
    verified: Optional[dict] = None

    # 已通过快捷指令写进日历的时间（防重复用）。
    # ⚠️ 只对「快捷指令路线」有意义 —— ICS 导入路线靠 UID 去重，不需要它。
    pushed_at: Optional[str] = None

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=_now_iso)

    # ---------- 核心：原始值 vs 有效值 ----------
    #
    # 约定（很重要，后面所有展示/导出都必须走 effective_*）：
    #   · `start` / `location` / …      = **从消息里读到的原始观测**，只写一次，不再变
    #   · `effective_*`                 = 展示与导出用的值 = 核实值优先，否则回落到原始值
    # 这样「事后修正」永远不会污染去重键。

    @property
    def is_verified(self) -> bool:
        """是否已被外部核实过（哪怕只核实了地点）。"""
        return bool(self.verified)

    def _v(self, key: str):
        """取核实值；没有就返回 None（**不要**在这里回落，回落逻辑在下面各自的方法里）。"""
        return (self.verified or {}).get(key)

    @property
    def effective_start(self) -> Optional[str]:
        return self._v("start") or self.start

    @property
    def effective_end(self) -> Optional[str]:
        return self._v("end") or self.end

    @property
    def effective_location(self) -> Optional[str]:
        return self._v("location") or self.location

    @property
    def effective_all_day(self) -> bool:
        v = self.verified or {}
        if "all_day" in v:
            return bool(v["all_day"])
        if v.get("start"):
            # 核实给了具体时刻 → 就不再是全天
            return "T" not in str(v["start"])
        return self.all_day

    @property
    def effective_granularity(self) -> str:
        v = self.verified or {}
        if "granularity" in v:
            return str(v["granularity"])
        if v.get("start"):
            return "minute" if "T" in str(v["start"]) else "day"
        return self.time_granularity

    @property
    def verified_source(self) -> str:
        return str(self._v("source") or "")

    @property
    def effective_start_dt(self) -> Optional[datetime]:
        return _parse_iso(self.effective_start)

    # ---------- 派生 ----------

    @property
    def fingerprint(self) -> str:
        """用于去重：同一条消息被反复喂进来时不要重复建事件。

        ⚠️ 键里**绝对不能含 title**。
        title 由大模型生成，同一条输入两次跑会给出不同措辞
        （实测同一条群通知跑 4 次得到「参加年级大会」「按时参加年级大会」
        「准时出席年级大会」「全员参加年级大会」四个版本）。
        一旦把 title 算进指纹，同一条消息每读一次就是一个新指纹
        → 每条都判为新事件 → **去重完全失效，日程库里堆重复项**。

        只用与模型无关的三样：来源渠道 + 原文 + 时间。
        原文和时间在「同一消息重放」时逐字相同，所以能稳定命中同一条。

        🔴 注意这里用的是 **`self.start`（原始值），不是 `effective_start`**。
        核实（联网查到的精确时间）走 `verified` 字段，**不碰 start**——
        否则核实一次就换个指纹，下次读到同一条消息会重复入库。
        `start` 的语义是「我们从这条消息里读到了什么」，是**观测**，不是状态。
        """
        basis = (f"{self.source_channel}|{(self.raw_text or '').strip()}"
                 f"|{(self.start or '')}")
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()

    @property
    def start_dt(self) -> Optional[datetime]:
        return _parse_iso(self.start)

    @property
    def is_actionable(self) -> bool:
        """够不够格进日历：有标题 + 有一定的时间精度 + 置信度不太差。

        用 `effective_granularity`：一条本来只有日期的事件，被核实出具体
        开场时间后，它就更够格进日历了。
        """
        return bool(self.title) and self.effective_granularity in (
            "day", "half_day", "hour", "minute")

    def days_until(self, now: Optional[datetime] = None) -> Optional[int]:
        s = self.effective_start_dt
        if not s:
            return None
        now = now or datetime.now().astimezone()
        return (s.date() - now.date()).days

    # ---------- 序列化 ----------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fingerprint"] = self.fingerprint
        d["is_actionable"] = self.is_actionable
        d["days_until"] = self.days_until()
        # 有效值单独给一份，调用方不用自己去算「核实优先还是原始优先」
        d["effective_start"] = self.effective_start
        d["effective_end"] = self.effective_end
        d["effective_location"] = self.effective_location
        d["effective_all_day"] = self.effective_all_day
        d["is_verified"] = self.is_verified
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in allowed})

    # ---------- 展示 ----------

    def when_label(self) -> str:
        """给人看的时间标签。

        ⚠️ 一律走 `effective_*`，不要用原始值 —— 否则核实过的具体时间
        会在这里被「全天」盖掉，用户看到的还是没核实的样子。

        全天事件不要显示成「09:00」：规则层对「只有日期没有时刻」的输入
        会给一个 `T09:00` 的占位值，那是内部约定，不是产品行为。
        """
        start = self.effective_start
        if not start:
            return "时间未定"
        day = start[:10]
        if self.effective_all_day or self.effective_granularity == "day":
            return f"全天 {day}"
        hm = start[11:16]
        return f"{day} {hm}"

    def human(self) -> str:
        when = self.when_label() + ("  ← 截止" if self.is_deadline else "")
        title = self.title + ("  ✅已核实" if self.is_verified else "")
        bits = [f"[{self.status}] {title}", f"  时间: {when}"]
        loc = self.effective_location
        if loc:
            # 核实过的地点标出来，让人一眼看出这是外部信息而不是消息里写的
            mark = "（核实）" if self._v("location") else ""
            bits.append(f"  地点: {loc}{mark}")
        if self.participants:
            bits.append(f"  参与: {', '.join(self.participants)}")
        bits.append(
            f"  精度: {self.effective_granularity}  优先级: {self.priority}  "
            f"置信度: {self.confidence:.2f}  抽取器: {self.extractor}"
        )
        if self.source_ref:
            bits.append(f"  来源: {self.source_channel}/{self.source_ref}")
        if self.is_verified:
            bits.append(f"  核实: {self.verified_source or '（未注明来源）'}")
            # 原始观测也留着，方便对照「我们原本读到的是什么」
            bits.append(f"  原始: {self.start or '无时间'} / {self.location or '无地点'}")
        if self.needs_clarification and self.clarify_question:
            bits.append(f"  ⚠ 待澄清: {self.clarify_question}")
        return "\n".join(bits)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
