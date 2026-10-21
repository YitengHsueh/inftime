"""
抽取层 —— 把一段杂乱的中文消息变成一条结构化事件。

架构决策（这一条很关键，别改）：
    **时间永远由规则引擎算，模型只负责语义。**
    LLM 在「算日期」这件事上会幻觉（它会把 2026 算成 2025、把周三算错一周），
    而它恰恰是「判断这句话是不是一件事、事是什么、在哪儿」的强项。
    各干各擅长的，出错率会低一个数量级。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

from .llm import chat_json, load_config
from .schema import Event
from .timeparse import (
    WhenResult,
    collect_spans,
    normalize,
    parse_when,
    strip_time_expressions,
)

# ---------------------------------------------------------------- 词典

POLITE_PREFIX = (
    "麻烦", "请", "记得", "别忘了", "别忘记", "提醒", "通知", "大家", "各位",
    "同学们", "各位同学", "同学", "全体", "注意", "友情提示", "温馨提示", "统一",
    "以此为准", "紧急通知", "重要通知", "划重点",
)
POLITE_SUFFIX = (
    "收到请回复", "收到回复", "收到请扣1", "谢谢", "谢谢大家", "多谢", "感谢",
    "请知悉", "知悉", "请准时参加", "准时参加", "辛苦", "辛苦大家", "拜托", "蟹蟹",
    "over", "谢谢配合", "请配合",
)

ACTION_VERBS = (
    "提交", "交", "回复", "确认", "参加", "出席", "开会", "会议", "面试", "考试", "答辩",
    "报名", "缴费", "支付", "取", "领", "送达", "寄", "发", "填", "写", "完成", "结束",
    "准备", "汇报", "评审", "讨论", "约", "见", "见面", "聚餐", "上线", "发布", "复核",
    "核对", "签", "阅", "打卡", "值班", "排练", "彩排", "培训", "讲座", "分享会", "活动",
    "体检", "预约", "续费", "截止", "收尾", "处理", "跟进", "安排", "联系", "沟通",
    "复盘", "整理", "更新", "修改", "优化", "订", "买", "还", "寄回", "开会讨论", "对接",
    "回访", "登记", "上报", "归档", "验收", "测试", "评估", "开学", "注册",
    # 模糊但明确是「承诺」的说法 —— 2018 年 BP 里专门讨论过这类指代不明语句
    "记得", "别忘了", "别忘记", "搞定", "落实", "做好", "弄好", "办好", "处理一下",
    "跟进一下", "看一下", "确认一下", "别忘了",
)
URGENT_WORDS = ("尽快", "立即", "马上", "务必", "紧急", "急", "今天", "今晚", "今日",
                "马上要", "最晚", "截止", "DDL", "deadline", "一定")

_LOC_PREFIX = re.compile(r"(?:地点|位置|地址|地点是)\s*[:：]?\s*([^，。；！？,;!?\n]{2,30})")
_LOC_IN = re.compile(r"在\s*([^\s，。；！？,;!?]{2,22}?)\s*(?:集合|举行|开会|见面|等您|等你|碰头|举办)")

# 地点抽取的三层模式（顺序即优先级）：
#   1) 楼上带房间号：「综合楼305会议室」
#   2) 纯房间号：「305会议室」
#   3) 地标后缀：「东方艺术中心」「同济大学菜鸟驿站」
# 早期版本用一个泛化字符类，把「请在6点前凭提货码至同济大学菜鸟驿站」整段吞成了地点。
_LOC_CORE = re.compile(
    r"([\u4e00-\u9fa5]{2,8}\d{1,4}(?:会议室|教室|报告厅|多功能厅|室|楼|厅)"
    r"|\d{1,4}(?:会议室|教室|报告厅|多功能厅|室|楼|厅)"
    r"|[\u4e00-\u9fa5]{2,10}(?:会议室|教室|报告厅|大礼堂|图书馆|食堂|体育馆|体育场"
    r"|活动中心|驿站|中心|广场|车站|机场|酒店|大厦|园区|校区|大楼|门口|咖啡馆|餐厅))"
)
# 命中里含这些字，说明吞进了动词或虚词，需要先裁剪
_LOC_DIRTY = ("在", "的", "至", "前", "请", "要", "去", "把", "和", "与", "到", "给", "凭", "收", "发")
# 裁剪点：取这些字符最后一次出现的位置之后的部分
_LOC_CUT = "在的至前到凭把从向离给"


def _trim_location(cand: str) -> Optional[str]:
    """「在综合楼305会议室」-> 「综合楼305会议室」；「提货码至同济大学菜鸟驿站」-> 「同济大学菜鸟驿站」。

    正则的字符类无法表达「不含汉字虚词」，所以匹配后裁剪。
    """
    cand = (cand or "").strip(" ，,。.、：:")
    cut = -1
    for i, ch in enumerate(cand):
        if ch in _LOC_CUT:
            cut = i
    if cut >= 0:
        cand = cand[cut + 1 :]
    cand = cand.lstrip("在的至前到凭把从向离给 ")
    if len(cand) < 3:
        return None
    if any(c in cand for c in _LOC_CUT):
        return None
    return cand

_MENTION = re.compile(r"@([\u4e00-\u9fa5A-Za-z0-9_\-]{1,20})")
_MASS = ("全体成员", "全体同学", "所有人", "大家", "各位", "全员", "全体")

# 消息行前缀：「[项目群] 李工: 正文」
_SRC_PREFIX = re.compile(
    r"^\s*(?:\[(?P<src1>[^\]]{1,40})\]\s*)?(?:(?P<who>[^:：\[\]\n]{1,20})\s*[:：]\s*)?(?P<msg>.+)$",
    re.S,
)


def split_source_prefix(line: str) -> tuple[Optional[str], Optional[str], str]:
    """拆出「来源 / 说话人 / 正文」，让 capture 和 audit 共用同一套解析。

    支持：`[项目群] 李工: 明天下午开会` / `李工：明天下午开会` / 裸消息
    保守处理：像「教务处通知：」这种其实是标题而非人名的，不拆。
    """
    line = (line or "").strip()
    if not line:
        return None, None, ""
    m = _SRC_PREFIX.match(line)
    if not m:
        return None, None, line
    src = (m.group("src1") or "").strip() or None
    who = (m.group("who") or "").strip() or None
    msg = (m.group("msg") or "").strip()

    # 误拆保护：正文太短，或「说话人」其实是个标题
    if who and (len(who) > 14 or who.endswith(("通知", "公告", "提醒", "须知", "说明", "要求"))):
        who = None
    if who is None and src is None:
        return None, None, line
    if len(msg) < 3:
        return src, None, line
    return src, who, msg

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF\u2b00-\u2bff\ufe0f]+"
)


# ---------------------------------------------------------------- 规则抽取

def rule_extract(
    text: str,
    *,
    now: Optional[datetime] = None,
    source_channel: str = "manual",
    source_ref: Optional[str] = None,
) -> Event:
    now = now or datetime.now().astimezone()
    raw = (text or "").strip()
    t = normalize(raw)

    when = parse_when(t, now)

    spans = collect_spans(t, when, now)
    title = _clean_title(t, spans)
    location = _find_location(t)
    participants = _find_participants(t)
    is_question = _is_question(t)

    # 标题里若混进了地点，去掉，避免「在305会议室」重复出现。
    # 连同地点前面的方位介词一起删，否则会留下「在 召开…」「凭提货码至 领取…」这种残骸。
    if location:
        title = re.sub(r"[在至到往去往]\s*" + re.escape(location), " ", title)
        title = title.replace(location, " ")
    title = _tidy(re.sub(r"\s+", " ", title))

    ev = Event(
        title=title or _fallback_title(t),
        start=when.start.isoformat(timespec="seconds") if when.start else None,
        end=when.end.isoformat(timespec="seconds") if when.end else None,
        all_day=when.all_day,
        location=location,
        participants=participants,
        source_channel=source_channel,
        source_ref=source_ref,
        raw_text=raw,
        is_deadline=when.is_deadline,
        time_granularity=when.granularity,
        extractor="rule",
    )

    ev.priority = _priority(t, when, now)
    ev.confidence = _confidence(ev, when, is_question)
    ev.notes = when.note

    # 待澄清判定
    q = None
    if is_question:
        q = "这条消息是个问句，需要你确认才算数——要不要按「是」处理？"
    elif when.note:
        q = when.note
    elif not when.ok and _looks_like_action(t):
        q = "看起来是件要做的事，但没说出时间。什么时候做？"
    if q:
        ev.needs_clarification = True
        ev.clarify_question = q

    return ev


_LEAD_NOISE = re.compile(r"^[\s，,、:：。.;；\-—]+")
_TRAIL_NOISE = re.compile(r"[\s，,、:：。.;；\-—]+$")
# 时间被抠掉后，句首容易剩一个孤零零的连接词（可能后面还跟着标点或空格）
_CONNECTIVE = re.compile(r"^[在到至的了对把给和与就也还而且][\s，,、:：]*(?=[\u4e00-\u9fa5])")
_PRONOUN = re.compile(r"^(?:我们|你们|咱们|我|你|他|她)\s*(?=[\u4e00-\u9fa5]{2,})")
# 这些词本身以连接词开头，不能剥
_KEEP_HEAD = ("到期", "到货", "到手", "到场", "到位", "在线", "在座", "在即", "的确",
              "对手", "对接", "对话", "和好", "与共")
# 句首话语标记：是「怎么说话」的，不是「要做什么」的，一律剥掉
_FILLER_HEAD = ("那么", "那", "另外", "对了", "还有", "顺便", "这个", "这些", "就是",
                "其实", "总之", "所以", "然后", "接着", "随后", "至于", "关于")


def _tidy(s: str) -> str:
    s = _LEAD_NOISE.sub("", s)
    if len(s) > 3 and not s.startswith(_KEEP_HEAD):
        for _ in range(4):
            before = s
            s = _LEAD_NOISE.sub("", s)
            s = _CONNECTIVE.sub("", s)
            s = _PRONOUN.sub("", s)
            for w in _FILLER_HEAD:
                if s.startswith(w) and len(s) - len(w) >= 3:
                    s = s[len(w):]
                    break
            if s == before:
                break
    # 时间被抠掉后，「在␣提交」这种残骸要清掉；
    # 被抠位置留下的空格只在汉字之间合并，保证「这份 PPT」这种中英混排不受影响。
    s = re.sub(r"[在到至]\s+(?=[\u4e00-\u9fa5])", "", s)
    s = re.sub(r"(?<=[\u4e00-\u9fa5]) +(?=[\u4e00-\u9fa5])", "", s)
    return _TRAIL_NOISE.sub("", _LEAD_NOISE.sub("", s)).strip()


def _clean_title(t: str, spans: list) -> str:
    s = strip_time_expressions(t, spans)
    s = _EMOJI.sub(" ", s)
    # 客套前缀可能叠好几层（「麻烦请大家…」），循环剥
    for _ in range(4):
        before = s
        for w in POLITE_PREFIX:
            if s.startswith(w):
                s = s[len(w):]
                break
        if s == before:
            break
    for w in POLITE_SUFFIX:
        if s.endswith(w):
            s = s[: -len(w)]
            break
    s = _MENTION.sub(" ", s)
    return _tidy(s)


def _fallback_title(t: str) -> str:
    s = _EMOJI.sub(" ", t).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:40] + "…") if len(s) > 40 else s


def _find_location(t: str) -> Optional[str]:
    m = _LOC_PREFIX.search(t)
    if m:
        loc = _trim_location(m.group(1))
        if loc and 2 <= len(loc) <= 30:
            return loc
    m = _LOC_IN.search(t)
    if m:
        loc = _trim_location(m.group(1))
        if loc:
            return loc
    hits = []
    for m in _LOC_CORE.finditer(t):
        cand = _trim_location(m.group(1))
        if not cand:
            continue
        hits.append((m.start(), len(cand), cand))
    if not hits:
        return None
    # 取最长的一个；长度相同取靠前的
    hits.sort(key=lambda x: (-x[1], x[0]))
    return hits[0][2][:30]


def _find_participants(t: str) -> list[str]:
    out: list[str] = []

    def push(name: str) -> None:
        name = name.strip()
        if not name:
            return
        # 去重时按包含关系判断，避免同时出现「全体成员」和「全体」
        for i, exist in enumerate(out):
            if name in exist or exist in name:
                if len(name) > len(exist):
                    out[i] = name
                return
        out.append(name)

    for m in _MENTION.finditer(t):
        push(m.group(1))
    for w in _MASS:
        if w in t:
            push("全体")
            break
    return out[:8]


def _is_question(t: str) -> bool:
    s = t.strip()
    if s.endswith(("？", "?")):
        return True
    if re.search(r"(吗|呢|是不是|有没有|能不能|可不可以|要不要)\s*[？?]?\s*$", s):
        return True
    return False


def _looks_like_action(t: str) -> bool:
    return any(v in t for v in ACTION_VERBS)


def _priority(t: str, when: WhenResult, now: datetime) -> int:
    p = 3
    if any(w in t for w in URGENT_WORDS):
        p += 1
    if when.is_deadline:
        p += 1
    if when.start:
        days = (when.start.date() - now.date()).days
        if days <= 0:
            p += 2
        elif days <= 1:
            p += 1
        elif days >= 14:
            p -= 1
    return max(1, min(5, p))


def _confidence(ev: Event, when: WhenResult, is_question: bool) -> float:
    c = 0.45
    if when.granularity == "minute":
        c += 0.30
    elif when.granularity == "hour":
        c += 0.24
    elif when.granularity == "half_day":
        c += 0.14
    elif when.granularity == "day":
        c += 0.10
    else:
        c -= 0.10
    if _looks_like_action(ev.raw_text):
        c += 0.12
    if len(ev.title) >= 4:
        c += 0.06
    if when.note:
        c -= 0.12
    if is_question:
        c -= 0.18
    return round(max(0.05, min(0.95, c)), 2)


# ---------------------------------------------------------------- LLM 增强

_SYSTEM = """你是一个中文消息理解引擎，服务于一个个人时间管理助理。
你的唯一任务：判断一段消息里是否包含「我需要采取行动的事」，并把语义抽出来。

严格输出 JSON，不要任何解释文字。字段：
{
  "is_actionable": true/false,     // 是否包含需要收件人行动的事
  "title": "动词短语，8-20字，不要含时间地点，不要保留「请/麻烦/记得」这类客套词",
  "location": "地点，没有则 null",
  "participants": ["涉及的人或群体"],
  "category": "work|study|life|social|other",
  "reason": "一句话说明你的判断依据"
}

判断准则：
- 闲聊、表情包、纯寒暄、已经在群里完成的确认 → is_actionable = false
- 「那你记得去把这件事做好」这类指代不明但明显是任务 → is_actionable = true
- 「那我们明天下午去参加比赛？」是提问，但隐含一个安排 → is_actionable = true
- 不要推断或补全原文没有的时间。时间由另一个模块处理，你不需要输出任何时间。"""


def llm_extract(
    text: str,
    *,
    source_channel: str = "manual",
    source_ref: Optional[str] = None,
    context: str = "",
) -> Optional[dict]:
    cfg = load_config()
    if not cfg.usable:
        return None
    user = f"消息内容：\n{text}"
    if context:
        user = f"消息上下文（群名/发件人）：{context}\n\n{user}"
    return chat_json(_SYSTEM, user, cfg)


def extract(
    text: str,
    *,
    now: Optional[datetime] = None,
    source_channel: str = "manual",
    source_ref: Optional[str] = None,
    use_llm: bool = True,
    stats: Optional[dict] = None,
) -> list[Event]:
    """主入口：一段消息 -> 0..n 条事件。

    为什么可能返回多条：一条消息里常有多件事（「记得交表；另外周五聚餐」）。
    规则层先按分号/换行切分，LLM 层再做语义过滤。

    stats：可选的出参字典。填 llm_calls / llm_ok / llm_used，
    用于外部判断「模型到底有没有真的参与」。
    **这一点对 compare 至关重要**：模型调用失败时 extract 会静默降级回规则结果，
    两边看起来完全一致 —— 若不区分，就会把「调用失败」统计成「没有差异」，
    从而系统性地低估模型价值。
    """
    now = now or datetime.now().astimezone()

    # 若调用方没给来源，尝试从「[群名] 说话人: 正文」里拆出来
    if source_ref is None and "\n" not in (text or "").strip():
        src, who, msg = split_source_prefix(text or "")
        if src or who:
            source_ref = " / ".join(x for x in (src, who) if x)
            text = msg

    segments = _split_segments(text)

    events: list[Event] = []
    llm_used = False
    llm_calls = 0
    llm_ok = 0

    for seg in segments:
        ev = rule_extract(seg, now=now, source_channel=source_channel, source_ref=source_ref)
        if use_llm:
            llm_calls += 1
            enriched = llm_extract(seg, source_channel=source_channel, source_ref=source_ref,
                                   context=source_ref or "")
            if enriched is not None:
                llm_used = True
                llm_ok += 1
                if not enriched.get("is_actionable", True):
                    continue                      # 模型判定为闲聊 -> 丢弃
                title = (enriched.get("title") or "").strip()
                if title:
                    ev.title = title[:60]
                loc = enriched.get("location")
                if loc and not ev.location:
                    ev.location = str(loc)[:40]
                ppl = enriched.get("participants") or []
                for p in ppl[:5]:
                    if isinstance(p, str) and p and p not in ev.participants:
                        ev.participants.append(p)
                cat = enriched.get("category")
                if cat:
                    ev.notes = (ev.notes + f" 类别:{cat}").strip()
                ev.confidence = round(min(0.97, ev.confidence + 0.18), 2)
                ev.extractor = "llm+rule"
        events.append(ev)

    if llm_used:
        # 规则层置信度是「基于证据」的，模型层再加权；这里统一补一个标记
        for e in events:
            e.extractor = "llm+rule"

    # 纯规则模式下，把不像事的段落压掉
    if not llm_used:
        events = [e for e in events if _keep_without_llm(e)]

    if stats is not None:
        stats["llm_calls"] = llm_calls
        stats["llm_ok"] = llm_ok
        stats["llm_used"] = llm_used

    return events


def _split_segments(text: str) -> list[str]:
    parts = re.split(r"[\n；;]+", text or "")
    out = []
    for p in parts:
        p = p.strip()
        if len(p) >= 3:
            out.append(p)
    return out or [(text or "").strip()]


def _keep_without_llm(ev: Event) -> bool:
    """没有模型时的保守过滤：有动作词，或时间很明确，才留下。"""
    if _looks_like_action(ev.raw_text):
        return True
    if ev.time_granularity in ("minute", "hour") and len(ev.title) >= 3:
        return True
    return False
