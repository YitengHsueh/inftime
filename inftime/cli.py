"""
Inftime 命令行入口。

    python -m inftime.cli capture "明天下午三点在综合楼305开会"
    python -m inftime.cli today
    python -m inftime.cli audit 聊天记录.txt --days 30 --out 审计.md
    python -m inftime.cli doctor
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .audit import render_markdown, run_audit
from .extract import extract
from .ics import write_single
from .llm import CONFIG_PATH, PRESETS, list_models, load_config, probe, write_config
from .store import Store


def _store() -> Store:
    return Store()


def cmd_capture(a: argparse.Namespace) -> int:
    store = _store()
    text = a.text if a.text else sys.stdin.read()
    events = extract(text, source_channel=a.channel, source_ref=a.ref, use_llm=not a.no_llm)
    if not events:
        print("（没有识别出需要行动的事）")
        return 0
    for ev in events:
        # 用**库里那份**来回显，而不是刚抽出来的这份。
        # 重复命中时 store.add 返回的是已存在的那条 —— 它可能带着核实结果
        # （verified_start / verified_location）。打印新抽的会显示成
        # 「全天 / 原始地点」，让人误以为核实信息丢了。
        stored, created = store.add(ev)
        print(("🆕 " if created else "♻️  ") + stored.human())
        print()
    return 0


def cmd_extract(a: argparse.Namespace) -> int:
    text = a.text if a.text else sys.stdin.read()
    events = extract(text, source_channel="dryrun", use_llm=not a.no_llm)
    if not events:
        print("（没有识别出事件）")
        return 0
    for ev in events:
        print(ev.to_json())
    return 0


def cmd_list(a: argparse.Namespace) -> int:
    store = _store()
    if a.scope == "today":
        evs = store.today()
    elif a.scope == "pending":
        evs = store.list(status="pending", limit=a.limit)
    elif a.scope == "all":
        evs = store.list(limit=a.limit)
    else:
        evs = store.upcoming(a.days)
    if not evs:
        print(f"（{a.scope} 范围内没有事件）")
        return 0
    for e in evs:
        print(f"{'⏰' if e.is_deadline else ' ·'} [{e.id}] {e.when_label():20s} {e.title}")
        if e.needs_clarification:
            print(f"      ⚠ {e.clarify_question}")
    return 0


def cmd_confirm(a: argparse.Namespace) -> int:
    store = _store()
    ev = store.get(a.id) or store.by_id_prefix(a.id)
    if not ev:
        print(f"找不到事件：{a.id}")
        return 1
    if a.title:
        ev = store.patch(ev.id, title=a.title) or ev
    if a.start:
        ev = store.patch(ev.id, start=a.start) or ev
    store.set_status(ev.id, "confirmed")
    ev = store.get(ev.id) or ev
    out = Path(a.out) if a.out else (Path.home() / ".inftime" / "ics" / f"{ev.id}.ics")
    write_single(ev, out, alarm_minutes=a.alarm)
    print(f"✅ 已生成：{out}")
    print(ev.human())
    return 0


def cmd_ignore(a: argparse.Namespace) -> int:
    store = _store()
    ev = store.get(a.id) or store.by_id_prefix(a.id)
    if not ev:
        print(f"找不到事件：{a.id}")
        return 1
    store.set_status(ev.id, "ignored")
    print(f"已忽略 [{ev.id}] {ev.title}")
    return 0


def cmd_push(a: argparse.Namespace) -> int:
    """把事件导出成 .ics，交给系统日历导入（**不需要 AppleScript 授权**）。

    实现细节（AppleScript 为什么走不通、确认框为什么会看不到）
    都在 `deliver.py` 的模块 docstring 里，这里只负责取数据 + 打印。
    """
    from .deliver import push_events

    store = _store()
    try:
        evs = [e for e in store.list()
               if e.status in ("pending", "confirmed") and e.is_actionable]
    finally:
        store.close()

    if not evs:
        print("没有可推送的事件。先用 capture / 企微桥收几条。")
        return 0

    res = push_events(evs, out=a.out, alarm_minutes=a.alarm,
                      dry_run=a.dry_run, to_icloud=a.to_icloud)

    print(f"已生成 {res.path}（{len(evs)} 条事件，提前 {a.alarm} 分钟提醒）")
    for ev in evs:
        loc = f"  @ {ev.location}" if ev.location else ""
        print(f"  · {ev.when_label()}  {ev.title}{loc}")

    if res.icloud_copy:
        print(f"已复制到 iCloud Drive：{res.icloud_copy}")
        print("  → iPhone 打开「文件」app 点它，即可导入日历")

    if res.delivered:
        print("已交给系统日历 —— 请在弹出的确认框里点「添加」完成导入。")
        print("  （若没看到确认框：切到「日历」app，它可能在等确认。零授权、只需点一次）")
    else:
        print(f"（{res.note}）")
    return 0


def cmd_audit(a: argparse.Namespace) -> int:
    src = Path(a.file)
    if not src.exists():
        print(f"文件不存在：{src}")
        return 1
    res = run_audit(src.read_text("utf-8"), window_days=a.days, use_llm=not a.no_llm)
    md = render_markdown(res)
    if a.out:
        Path(a.out).write_text(md, "utf-8")
        print(f"✅ 报告已写入：{a.out}")
        print(f"   扫描 {res.scanned} 条消息，识别 {res.total} 件事，"
              f"其中 {len(res.no_time)} 件没有时间。")
    else:
        print(md)
    return 0


def _check_model(cfg) -> bool:
    """写入配置后，拿服务端清单核一遍模型名。

    这条检查是一次真实教训的产物：余额为空时错误码是 402 而不是
    「模型不存在」，模型名写错可以潜伏到你充值成功那一刻才爆。
    所以必须在写入时就拦，不能等调用时才发现。
    """
    ids, note = list_models(cfg)
    if ids is None:
        print(f"   ⚠ 端点未提供模型清单（{note}），跳过模型名校验")
        return True
    if cfg.model in ids:
        print(f"   ✅ 模型名已核对（该端点提供 {len(ids)} 个模型）")
        return True
    print(f"   ❌ 服务端清单里没有「{cfg.model}」——不改的话，等你充值成功那一刻才会报错。")
    free = [i for i in ids if any(k in i.lower() for k in ("flash", "free", "lite", "-7b", "-8b"))]
    show = (free or ids)[:10]
    print("      可用模型（前 10）：" + "、".join(show))
    print(f"      改用：python3 -m inftime.cli config --model <上面某一个>")
    return False


def cmd_config(a: argparse.Namespace) -> int:
    if a.models:
        cfg = load_config()
        if not cfg.base_url:
            print("还没配置端点。先跑 config --use <预设> <key>")
            return 1
        ids, note = list_models(cfg)
        if ids is None:
            print(f"该端点未提供模型清单（{note}）。")
            return 1
        print(f"{cfg.base_url} 共 {len(ids)} 个模型：\n")
        for i in ids:
            print(("  ★ " if i == cfg.model else "    ") + i)
        print("\n（★ = 当前配置）")
        return 0

    if a.use:
        name, key = a.use
        pset = PRESETS.get(name.lower())
        if not pset:
            print(f"未知预设「{name}」。可用：" + "、".join(PRESETS))
            print("也可以直接指定：config --set <base_url> <key> <model>")
            return 1
        model = a.model or pset["model"]
        p = write_config(pset["base_url"], key.strip(), model)
        print(f"✅ 已写入 {p}（权限 600）")
        print(f"   {pset['hint']}")
        cfg = load_config()
        print(f"   厂商 {cfg.base_url} · 模型 {cfg.model} · key {cfg.masked}")
        good = _check_model(cfg)
        return _finish_config(a, cfg, good)

    if a.set:
        base, key, model = a.set
        p = write_config(base, key, model)
        print(f"✅ 已写入 {p}（权限 600）")
        cfg = load_config()
        print(f"   厂商 {cfg.base_url} · 模型 {cfg.model} · key {cfg.masked}")
        good = _check_model(cfg)
        return _finish_config(a, cfg, good)

    if a.model:
        base, key = load_config().base_url, load_config().api_key
        if not base or not key:
            print("还没配置端点和 key。先跑 config --use <预设> <key>")
            return 1
        p = write_config(base, key, a.model)
        print(f"✅ 模型已改为 {a.model}（{p}）")
        cfg = load_config()
        good = _check_model(cfg)
        return _finish_config(a, cfg, good)

    if a.test:
        ok, msg = probe()
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1

    cfg = load_config()
    print(f"配置文件 : {CONFIG_PATH}")
    print(f"base_url : {cfg.base_url or '(未设置)'}")
    print(f"api_key  : {cfg.masked}")
    print(f"model    : {cfg.model or '(未设置)'}")
    print(f"可用状态 : {'✅ 已启用大模型增强' if cfg.usable else '⬜ 未启用（使用规则引擎，功能完全可用）'}")
    if not cfg.usable:
        print("\n可用预设：")
        for k, v in PRESETS.items():
            print(f"  {k:12s} {v['hint']}")
        print("\n接一个：python3 -m inftime.cli config --use zhipu <你的key> --test")
    return 0


def _finish_config(a: argparse.Namespace, cfg, model_ok: bool) -> int:
    """写入后收尾：可选真连测试。模型名不对就直接判失败，别让它潜伏。"""
    if not a.test:
        if not model_ok:
            return 1
        print("   下一步：加 --test 真连一次，或直接跑 compare 看差异率。")
        return 0
    print("   正在真连测试…")
    ok, msg = probe(cfg)
    print(("   ✅ " if ok else "   ❌ ") + msg)
    return 0 if (ok and model_ok) else 1


def cmd_doctor(a: argparse.Namespace) -> int:
    from .timeparse import parse_when
    from datetime import datetime

    print(f"Inftime v{__version__} · 自检")
    print("-" * 46)
    store = _store()
    print(f"数据库      : {store.path}")
    print(f"事件总数    : {store.stats()['total']}")
    cfg = load_config()
    print(f"大模型      : {'已配置 ' + cfg.model if cfg.usable else '未配置（规则引擎）'}")
    print("-" * 46)
    checks = [
        ("明天下午三点开会", {"hour": 15}),
        ("下周三上午十点面试", {"hour": 10}),
        ("本周内交掉", {"granularity": "day"}),
    ]
    ok = 0
    for text, expect in checks:
        r = parse_when(text)
        good = all(
            (r.start is not None and r.start.hour == v) if k == "hour"
            else getattr(r, k) == v
            for k, v in expect.items()
        )
        ok += good
        print(f"{'✅' if good else '❌'} 时间解析：{text} -> {r.start}")
    print("-" * 46)
    print(f"结论：{ok}/{len(checks)} 项通过。" + ("可以开工。" if ok == len(checks) else "有问题，先修。"))
    return 0 if ok == len(checks) else 1


def cmd_compare(a: argparse.Namespace) -> int:
    """规则引擎 vs 大模型，逐条对照。

    这个命令存在的意义：决定「要不要为语义增强付钱」。
    没有它，你只能凭感觉判断大模型有没有用。

    跑得慢（每条一次模型调用，推理模型单条 2~15 秒），所以进度实时打印到 stderr，
    结果可以和进度分开重定向。
    """
    from .compare import render_html, render_text, run_compare

    cfg = load_config()
    if not cfg.usable:
        print("⚠️  未配置大模型，无法对比。先跑：")
        print("   python3 -m inftime.cli config --use zhipu <key> --test")
        return 1

    ok, msg = probe(cfg)
    if not ok:
        print(f"⚠️  大模型不可用，无法对比：{msg}")
        return 1

    if a.file:
        text = Path(a.file).read_text("utf-8")
    else:
        text = sys.stdin.read()
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        print("没有可对比的内容。")
        return 0

    import time

    t0 = time.time()

    def progress(i, total, line):
        pct = i / total * 100
        sys.stderr.write(f"\r  对照中 {i}/{total}（{pct:.0f}%）{line[:26]:<26}")
        sys.stderr.flush()

    res = run_compare(lines, model=cfg.model, progress=progress)
    sys.stderr.write(f"\r  完成 {res.n} 条，用时 {time.time() - t0:.0f}s{' ' * 30}\n")

    if a.out:
        out = Path(a.out)
        if out.suffix.lower() in (".html", ".htm"):
            out.write_text(render_html(res), "utf-8")
        else:
            out.write_text(render_text(res), "utf-8")
        print(f"✅ 报告已写入：{out}")
        print(f"   一致 {res.n_same} · 不同 {res.n_diff}（{res.rate:.0f}%）"
              f" · 模型成功 {res.llm_ok}/{res.llm_calls}")
        if not res.viable:
            print("   ⚠ 模型成功率不足 80%，本次差异率不足以支撑结论。")
        if res.kinds:
            print("   差异构成：" + "、".join(f"{k} {v}" for k, v in res.kinds.items()))
        return 0

    print(render_text(res))
    return 0


def _normalize_iso(s: str) -> str:
    """把用户/agent 给的 ISO 时间补上时区，返回规范形式。

    ⚠️ 为什么要管这个：坑 1 就是「本地时间被当成 UTC」导致全部偏移 8 小时。
    这里**不猜 UTC**，只用本机时区补齐，并把补了什么打出来让人看见。
    """
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"时间格式不对：{s!r}（要 ISO8601，如 2026-10-31T19:30:00+08:00）") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.isoformat(timespec="seconds")


def _needs_verification(ev) -> list[str]:
    """这条事件「还缺什么」，缺就说明值得联网核实。

    只按两个客观维度判断，不做语义猜测：
      · 精度是 `day` → 只知道哪一天，不知道几点（演唱会、展会最典型）
      · 没有地点     → 不知道去哪
    """
    lacks = []
    if ev.effective_granularity == "day":
        lacks.append("具体时间")
    if not ev.effective_location:
        lacks.append("地点")
    return lacks


def _suggest_query(ev) -> str:
    """给一条「起手式」检索词。

    刻意**不用大模型**生成 —— 那会让 `enrich --list` 变得又慢又花钱，
    而且换不来多少质量。真正的检索词由调用方（agent）根据上下文再打磨。
    """
    day = (ev.effective_start or "")[:10]
    parts = [ev.effective_location or "", day, ev.title]
    return " ".join(p for p in parts if p).strip()


def cmd_enrich(a: argparse.Namespace) -> int:
    """联网核实到的「精确时间 / 场馆」写回事件。

    为什么单独一个字段、不改 `start`：
      `fingerprint`（去重键）里含 `start`。改了 start，同一条群消息下次再读
      会算出新指纹 → 重复入库。所以核实结果存进 `verified`，
      `start` 永远保持「从消息里读到的原始值」。

    典型用法（由 agent 串起来）：
        inftime enrich --list                    # ① 看哪些缺时间/地点
        # ② agent 用检索词去搜（WebSearch）
        inftime enrich --apply 9c3b8c687691 \\
            --start 2026-10-31T19:30:00+08:00 \\
            --location 苏州奥体中心体育馆 \\
            --source "大麦网 https://..."
    """
    store = _store()
    try:
        if a.clear:
            ev = store.set_verified(a.clear, None)
            if not ev:
                print(f"找不到事件：{a.clear}")
                return 1
            print(f"已清除核实信息：{ev.title}")
            return 0

        if a.apply:
            ev = store.get(a.apply)
            if not ev:
                print(f"找不到事件：{a.apply}")
                return 1

            data = dict(ev.verified or {})
            changed = []

            if a.start:
                try:
                    data["start"] = _normalize_iso(a.start)
                except ValueError as exc:
                    print(f"❌ {exc}")
                    return 1
                changed.append(f"时间 → {data['start']}")
            if a.end:
                try:
                    data["end"] = _normalize_iso(a.end)
                except ValueError as exc:
                    print(f"❌ {exc}")
                    return 1
                changed.append(f"结束 → {data['end']}")
            if a.location:
                data["location"] = a.location
                changed.append(f"地点 → {a.location}")

            if not changed:
                print("什么都没改。要写点什么就得给 --start / --end / --location 之一。")
                return 1

            # ⚠️ 来源必填。核实信息一旦没有可指认的出处，就和「模型凭记忆编的」
            # 没有区别 —— 那正是「时间由规则算」要防的东西。
            if not a.source:
                print("❌ --apply 必须带 --source（出处 / 链接 / 「用户确认」）。")
                print("   没有出处的核实值，和模型幻觉没有区别，所以这里不让写。")
                return 1

            data["source"] = a.source
            from datetime import datetime
            data["at"] = datetime.now().astimezone().isoformat(timespec="seconds")

            if a.title:
                store.patch(a.apply, title=a.title)
                changed.append(f"标题 → {a.title}")

            ev = store.set_verified(a.apply, data)
            print(f"✅ 已核实 [{ev.id}] {ev.title}")
            for c in changed:
                print(f"   {c}")
            print(f"   来源：{a.source}")
            print(f"   展示时间：{ev.when_label()}")
            # 顺手确认去重键没被影响 —— 这是这套设计的核心承诺
            print(f"   指纹未变：{ev.fingerprint[:12]}…（核实不进去重键）")
            return 0

        # ---- 默认：列出待核实的事件 ----
        rows = store.list()
        if not a.all:
            rows = [e for e in rows if not e.is_verified]
        rows = [e for e in rows
                if e.status in ("pending", "confirmed") and e.is_actionable]
        todo = [(e, _needs_verification(e)) for e in rows]
        todo = [(e, lacks) for e, lacks in todo if lacks]

        if not todo:
            print("没有需要外部核实的事件。")
            return 0

        if a.json:
            import json as _json
            print(_json.dumps([{
                "id": e.id, "title": e.title,
                "when": e.when_label(),
                "location": e.effective_location,
                "lacks": lacks,
                "suggested_query": _suggest_query(e),
            } for e, lacks in todo], ensure_ascii=False, indent=2))
            return 0

        print(f"需要外部核实的事件（{len(todo)} 条）\n")
        for e, lacks in todo:
            print(f"· [{e.id}] {e.when_label()}  {e.title}")
            print(f"   已知地点: {e.effective_location or '—'}")
            print(f"   缺      : {'、'.join(lacks)}")
            print(f"   建议检索: {_suggest_query(e)}")
            print()
        print("查到之后用 `enrich --apply <id> --start ... --location ... --source ...` 写回。")
        return 0
    finally:
        store.close()


def cmd_auto_calendar(a: argparse.Namespace) -> int:
    """通过快捷指令把事件**全自动**写进系统日历（不弹任何框）。

    与 `push` 的区别：
      · push     ：生成 .ics → `open -a Calendar` → 你点一次「添加」
      · 本命令    ：`shortcuts run` → 直接写进日历 → 无任何弹窗

    前提：快捷指令「Inftime记日程」已建好（建法见 README）。
    第一次运行时 macOS 会问一次「允许访问日历」，同意后就是全自动。
    """
    from . import calendar_shortcut as cs

    if not cs.shortcut_exists(a.shortcut):
        print(f"❌ 找不到快捷指令「{a.shortcut}」。先在「快捷指令」app 里建一个，")
        print("   步骤见 README 的「全自动日历：快捷指令中转」。")
        return 1

    store = _store()
    try:
        evs = [e for e in store.list()
               if e.status in ("pending", "confirmed") and e.is_actionable]

        # ⚠️ **必须**默认只推没推过的。快捷指令是**裸写**日历，没有任何去重
        # （ICS 路线靠 UID 去重，这条路没有）。不筛的话，每小时轮询会把
        # 同一条事件重复写 N 遍。
        if not a.all:
            evs = [e for e in evs if not e.pushed_at]

        if a.newer_than:
            from datetime import datetime, timedelta
            cut = datetime.now().astimezone() - timedelta(minutes=a.newer_than)
            evs = [e for e in evs
                   if (e.updated_at or "") >= cut.isoformat(timespec="seconds")]

        # 过期宽限：开始时间早于 N 小时前的事件不进日历（2026-10-21 加：
        # 实测库里躺着一条过期 33h 的「年级大会」，无脑推只会污染日历）。
        # 关机补跑场景最多隔夜，24h 足够；更早的说明这条日程已经结束了。
        # ⚠️ 跳过必须打印 —— 「什么都没发生」最难排查（坑 17）。
        if not a.force_old and evs:
            from datetime import datetime, timedelta
            cutoff = datetime.now().astimezone() - timedelta(hours=a.skip_older_than)
            fresh, stale = [], []
            for e in evs:
                s = e.effective_start_dt
                (stale if (s and s < cutoff) else fresh).append(e)
            evs = fresh
            if stale:
                names = "、".join(f"「{e.title}」（{e.when_label()}）" for e in stale)
                print(f"⏭ 跳过 {len(stale)} 条开始时间早于 {a.skip_older_than} 小时前的事件：{names}")
                print("   （确认还要推就加 --force-old）")

        if not evs:
            print("没有要写进日历的事件（都已写过，或库里没有可行动的事件）。")
            return 0

        print(f"通过快捷指令「{a.shortcut}」写入 {len(evs)} 条事件…")
        results = cs.push_via_shortcut(evs, shortcut=a.shortcut,
                                       alarm_minutes=a.alarm, calendar=a.calendar)
        ok = 0
        # 只把**成功**的记成已推。失败的留着，下一轮还会试。
        # （全程用同一个 store 连接 —— 中途重开会把「已关闭」的问题带出来）
        for r in results:
            if r.ok:
                ok += 1
                store.set_pushed(r.event.id)
                print(f"  ✅ {r.event.when_label()}  {r.event.title}")
            else:
                print(f"  ❌ {r.event.title} —— {r.detail}")
        print(f"\n完成：{ok}/{len(results)} 条已写入日历。")
        if ok < len(results):
            print("（第一次运行时 macOS 可能弹「允许访问日历」——同意一次，以后就全自动了）")
            return 3
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inftime", description="Inftime · 把消息变成日程")
    p.add_argument("--version", action="version", version=f"Inftime {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="吃掉一段消息，抽成事件并保存")
    c.add_argument("text", nargs="?", help="消息文本；不传则从 stdin 读")
    c.add_argument("--channel", default="manual")
    c.add_argument("--ref", default=None)
    c.add_argument("--no-llm", action="store_true")
    c.set_defaults(func=cmd_capture)

    e = sub.add_parser("extract", help="只抽取不保存")
    e.add_argument("text", nargs="?")
    e.add_argument("--no-llm", action="store_true")
    e.set_defaults(func=cmd_extract)

    ls = sub.add_parser("list", help="列出事件")
    ls.add_argument("--scope", default="upcoming",
                    choices=["today", "upcoming", "pending", "all"])
    ls.add_argument("--days", type=int, default=7)
    ls.add_argument("--limit", type=int, default=50)
    ls.set_defaults(func=cmd_list)

    t = sub.add_parser("today", help="看今天")
    t.set_defaults(func=lambda a: cmd_list(argparse.Namespace(
        scope="today", days=1, limit=50)))

    cf = sub.add_parser("confirm", help="确认事件并生成 .ics")
    cf.add_argument("id")
    cf.add_argument("--title", default=None)
    cf.add_argument("--start", default=None, help="ISO8601，如 2026-10-12T14:00:00+08:00")
    cf.add_argument("--alarm", type=int, default=15, help="提前提醒分钟数")
    cf.add_argument("--out", default=None)
    cf.set_defaults(func=cmd_confirm)

    pu = sub.add_parser("push", help="导出 .ics 并交给系统日历（无需 AppleScript 授权）")
    pu.add_argument("--out", default=None, help="输出路径，默认 ./Inftime-日程.ics")
    pu.add_argument("--alarm", type=int, default=15, help="提前多少分钟提醒（默认 15）")
    pu.add_argument("--to-icloud", action="store_true",
                    help="同时复制到 iCloud Drive，供 iPhone「文件」里点开导入")
    pu.add_argument("--dry-run", action="store_true", help="只生成文件，不打开日历")
    pu.set_defaults(func=cmd_push)

    ig = sub.add_parser("ignore", help="忽略事件")
    ig.add_argument("id")
    ig.set_defaults(func=cmd_ignore)

    au = sub.add_parser("audit", help="承诺审计")
    au.add_argument("file")
    au.add_argument("--days", type=int, default=30)
    au.add_argument("--out", default=None)
    au.add_argument("--no-llm", action="store_true")
    au.set_defaults(func=cmd_audit)

    co = sub.add_parser("config", help="查看 / 设置模型")
    co.add_argument("--use", nargs=2, metavar=("PRESET", "API_KEY"),
                    help="一键接入预设端点，如 --use zhipu <key>（预设：" + "/".join(PRESETS) + "）")
    co.add_argument("--set", nargs=3, metavar=("BASE_URL", "API_KEY", "MODEL"),
                    help="手动指定，如 --set https://api.deepseek.com sk-xxx deepseek-flash")
    co.add_argument("--model", default=None, help="只改模型名")
    co.add_argument("--models", action="store_true", help="列出端点提供的全部模型 id")
    co.add_argument("--test", action="store_true",
                    help="发一次真实请求验证 key 可用（可单独用，也可跟写入类参数一起用）")
    co.set_defaults(func=cmd_config)

    dr = sub.add_parser("doctor", help="自检")
    dr.set_defaults(func=cmd_doctor)

    en = sub.add_parser(
        "enrich",
        help="把联网核实到的精确时间/场馆写回事件（不动去重键）")
    en.add_argument("--list", action="store_true",
                    help="列出还缺「具体时间/地点」的事件（默认动作）")
    en.add_argument("--all", action="store_true",
                    help="连同已核实过的一起列出")
    en.add_argument("--json", action="store_true", help="机器可读输出")
    en.add_argument("--apply", metavar="ID", default=None, help="把核实结果写入这个事件")
    en.add_argument("--start", default=None,
                    help="核实到的开始时间（ISO8601，如 2026-10-31T19:30:00+08:00）")
    en.add_argument("--end", default=None, help="核实到的结束时间")
    en.add_argument("--location", default=None, help="核实到的地点/场馆")
    en.add_argument("--title", default=None, help="顺带修正标题")
    en.add_argument("--source", default=None,
                    help="⚠️ 必填：出处（链接 / 平台名 / 「用户确认」）")
    en.add_argument("--clear", metavar="ID", default=None, help="清除某个事件的核实信息")
    en.set_defaults(func=cmd_enrich)

    ac = sub.add_parser(
        "auto-calendar",
        help="通过快捷指令把事件全自动写进系统日历（零弹窗）")
    ac.add_argument("--shortcut", default="Inftime记日程", help="快捷指令名")
    ac.add_argument("--calendar", default="个人", help="写入哪个日历")
    ac.add_argument("--alarm", type=int, default=15, help="提前多少分钟提醒（默认 15）")
    ac.add_argument("--newer-than", type=int, default=None, metavar="分钟",
                    help="只处理最近 N 分钟内更新过的事件（给轮询用）")
    ac.add_argument("--skip-older-than", type=int, default=24, metavar="小时",
                    help="跳过开始时间早于 N 小时前的事件（默认 24，防把过期日程"
                         "写进日历；补跑场景可用 --force-old 覆盖）")
    ac.add_argument("--force-old", action="store_true",
                    help="连同已过期的事件一起推（调试/补录用）")
    ac.add_argument("--all", action="store_true",
                    help="连同已推送过的一起重推（⚠️ 会在日历里产生重复）")
    ac.set_defaults(func=cmd_auto_calendar)

    cp = sub.add_parser("compare", help="规则引擎 vs 大模型，逐条对照")
    cp.add_argument("file", nargs="?", help="消息文件，一行一条；不传则从 stdin 读")
    cp.add_argument("--out", default=None, help="报告输出路径，.html 出暗色报告页")
    cp.set_defaults(func=cmd_compare)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
