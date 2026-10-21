"""
规则引擎 vs 大模型 —— 对照实验。

存在的意义只有一个：**决定值不值得为语义增强付费。**
没有它，你只能凭感觉判断模型有没有用。

两条测量纪律（都是踩过的坑）：

1. **置信度不能算进差异。** confidence 是模型的自我报告，两边本来就不可能相等。
   早期版本把它拼进对比字符串，导致几乎每一条都「有差异」，差异率虚高到没有意义。

2. **模型调用失败不能算作「没有差异」。** extract() 在模型不可用时会静默降级回规则结果，
   两边看起来一模一样。若不单独统计，就会把「调用失败」记成「结论一致」，
   系统性地低估模型价值 —— 一个 47 条里 30 条超时的实验会得出「模型没用」的错误结论。

所以差异只按语义维度判定：抽了几条 / 标题是什么 / 时间是什么。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from .extract import extract

# 归一化标题时剔掉这些标点，避免「开会。」和「开会」被算成两条不同的东西
_PUNCT = re.compile(r"[\s，。、,.!！?？~～·:：；;\"'“”‘’（）()\[\]【】]+")


def _norm(s: str) -> str:
    return _PUNCT.sub("", s or "")


def _sig(events) -> set[tuple[str, str]]:
    """一组事件的语义签名。**故意不含 confidence。**"""
    return {(_norm(e.title), e.when_label()) for e in events}


@dataclass
class Item:
    text: str
    rule: list
    llm: list
    llm_failed: bool = False

    @property
    def kind(self) -> str:
        """差异类型；空串表示结论一致。"""
        if self.llm_failed:
            return "模型未参与"
        r, l = self.rule, self.llm
        if len(r) != len(l):
            return "条数不同"
        if not r and not l:
            return ""
        if _sig(r) == _sig(l):
            return ""
        rt, lt = {_norm(e.title) for e in r}, {_norm(e.title) for e in l}
        rw, lw = {e.when_label() for e in r}, {e.when_label() for e in l}
        if rt == lt and rw != lw:
            return "时间不同"
        if rt != lt and rw == lw:
            return "标题不同"
        return "标题与时间都不同"


@dataclass
class CompareResult:
    model: str
    items: list[Item] = field(default_factory=list)
    created: str = ""
    llm_calls: int = 0
    llm_ok: int = 0

    @property
    def n(self) -> int:
        return len(self.items)

    @property
    def n_diff(self) -> int:
        return sum(1 for i in self.items if i.kind)

    @property
    def n_same(self) -> int:
        return self.n - self.n_diff

    @property
    def rate(self) -> float:
        return (self.n_diff / self.n * 100) if self.n else 0.0

    @property
    def kinds(self) -> dict[str, int]:
        d: dict[str, int] = {}
        for i in self.items:
            if i.kind:
                d[i.kind] = d.get(i.kind, 0) + 1
        return dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))

    @property
    def viable(self) -> bool:
        """模型调用成功率是否足以支撑结论。"""
        return self.llm_calls > 0 and self.llm_ok / self.llm_calls >= 0.8


def run_compare(
    lines: list[str],
    model: str = "",
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> CompareResult:
    res = CompareResult(model=model, created=datetime.now().strftime("%Y-%m-%d %H:%M"))
    total = len(lines)
    for idx, line in enumerate(lines, 1):
        rule = extract(line, source_channel="cmp", use_llm=False)
        stats: dict = {}
        llm = extract(line, source_channel="cmp", use_llm=True, stats=stats)
        res.llm_calls += stats.get("llm_calls", 0)
        res.llm_ok += stats.get("llm_ok", 0)
        res.items.append(Item(
            text=line, rule=rule, llm=llm,
            llm_failed=bool(stats.get("llm_calls")) and not stats.get("llm_ok"),
        ))
        if progress:
            progress(idx, total, line)
    return res


def _brief(events) -> str:
    if not events:
        return "（无事件）"
    bits = []
    for e in events:
        clar = " ⚠待澄清" if e.needs_clarification else ""
        bits.append(f"{e.title} @ {e.when_label()}{clar}")
    return " ｜ ".join(bits)


def render_text(res: CompareResult) -> str:
    out = [f"模型：{res.model or '(未配置)'}    样本 {res.n} 条", "=" * 88]
    for i, it in enumerate(res.items, 1):
        if not it.kind:
            continue
        out.append(f"\n[{i}] {it.text}")
        out.append(f"   规则 : {_brief(it.rule)}")
        out.append(f"   增强 : {_brief(it.llm)}   ← {it.kind}")

    out.append("\n" + "=" * 88)
    out.append(f"结论一致的 {res.n_same} 条，不同 {res.n_diff} 条（{res.rate:.0f}%）。")
    if res.kinds:
        out.append("差异构成：")
        for k, v in res.kinds.items():
            out.append(f"  {k:16s} {v} 条")
    out.append(f"模型调用：{res.llm_calls} 次，成功 {res.llm_ok} 次。")
    if not res.viable:
        out.append("⚠ 模型成功率偏低，本次差异率不足以支撑结论，建议重跑。")
    return "\n".join(out)


# ------------------------------------------------------------------ HTML 报告

_CSS = """
:root{
  --bg:#0e1014; --panel:#161a21; --panel2:#1d222b; --line:#252b36;
  --fg:#e7eaf0; --muted:#8b93a7; --dim:#5f6779;
  --accent:#6ea8fe; --ok:#4ade80; --warn:#fbbf24; --bad:#f87171;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1000px;margin:0 auto;padding:56px 28px 96px}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13.5px;margin-bottom:40px}
h2{font-size:15px;color:var(--muted);font-weight:600;letter-spacing:.06em;
  text-transform:uppercase;margin:44px 0 16px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px}
.card .n{font-size:30px;font-weight:650;letter-spacing:-.02em;line-height:1.15}
.card .l{font-size:12.5px;color:var(--muted);margin-top:5px}
.card.hl .n{color:var(--accent)}
.bar{display:flex;height:10px;border-radius:6px;overflow:hidden;background:var(--panel2);margin:18px 0 10px}
.bar i{display:block}
.legend{display:flex;flex-wrap:wrap;gap:16px;font-size:13px;color:var(--muted)}
.legend span{display:inline-flex;align-items:center;gap:7px}
.dot{width:9px;height:9px;border-radius:3px;display:inline-block}
.note{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:8px;padding:14px 18px;color:var(--muted);font-size:13.5px;margin-top:18px}
.note.warn{border-left-color:var(--warn);color:var(--warn)}
.row{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px;margin-bottom:10px}
.row .t{font-size:14.5px;margin-bottom:12px;color:var(--fg)}
.row .t em{color:var(--dim);font-style:normal;margin-right:8px;font-size:12.5px}
.side{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.side div{background:var(--panel2);border-radius:8px;padding:10px 12px;font-size:13.5px;line-height:1.6}
.side b{display:block;font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--dim);font-weight:600;margin-bottom:4px}
.tag{display:inline-block;font-size:11.5px;padding:2px 9px;border-radius:20px;
  background:rgba(110,168,254,.13);color:var(--accent);margin-left:8px;vertical-align:middle}
.empty{color:var(--dim);font-size:13.5px}
footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--line);
  color:var(--dim);font-size:12.5px}
"""

_PALETTE = ["#6ea8fe", "#fbbf24", "#4ade80", "#f87171", "#c084fc", "#38bdf8"]


def render_html(res: CompareResult) -> str:
    e = html.escape
    kinds = res.kinds

    bar, legend = [], []
    if kinds:
        for i, (k, v) in enumerate(kinds.items()):
            pct = v / res.n * 100
            c = _PALETTE[i % len(_PALETTE)]
            bar.append(f'<i style="width:{pct:.3f}%;background:{c}"></i>')
            legend.append(f'<span><i class="dot" style="background:{c}"></i>{e(k)} · {v}</span>')

    rows = []
    for idx, it in enumerate(res.items, 1):
        if not it.kind:
            continue
        rows.append(
            f'<div class="row"><div class="t"><em>#{idx}</em>{e(it.text)}'
            f'<span class="tag">{e(it.kind)}</span></div>'
            f'<div class="side"><div><b>规则引擎</b>{e(_brief(it.rule))}</div>'
            f'<div><b>大模型</b>{e(_brief(it.llm))}</div></div></div>'
        )
    if not rows:
        rows.append('<div class="empty">本次样本中两边结论完全一致，没有差异项。</div>')

    warn = ""
    if not res.viable:
        warn = (f'<div class="note warn">⚠ 模型只成功了 {res.llm_ok}/{res.llm_calls} 次，'
                f'成功率不足 80%。失败的调用会静默降级成规则结果，'
                f'被算作「结论一致」，从而**低估**模型价值。建议重跑再说。</div>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inftime · 规则引擎 vs 大模型</title><style>{_CSS}</style></head>
<body><div class="wrap">
  <h1>规则引擎 vs 大模型</h1>
  <div class="sub">对照模型 <b>{e(res.model or '(未配置)')}</b> · 生成于 {e(res.created)}
    · 差异只按语义维度判定，不含置信度浮点噪声</div>

  <div class="cards">
    <div class="card"><div class="n">{res.n}</div><div class="l">样本条数</div></div>
    <div class="card"><div class="n">{res.n_same}</div><div class="l">结论一致</div></div>
    <div class="card hl"><div class="n">{res.n_diff}</div><div class="l">结论不同</div></div>
    <div class="card hl"><div class="n">{res.rate:.0f}%</div><div class="l">差异率</div></div>
  </div>

  <div class="bar">{''.join(bar)}</div>
  <div class="legend">{''.join(legend)}</div>
  {warn}

  <h2>差异明细</h2>
  {''.join(rows)}

  <footer>Inftime v0.1 · 本地优先 · 数据不出本机 ·
    模型调用 {res.llm_ok}/{res.llm_calls} 次成功</footer>
</div></body></html>"""
