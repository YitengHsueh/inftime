"""
企业微信 → Inftime 桥。

这是整个项目里**最接近 2018 年那份 BP 原始设想**的一块：
从群消息的源头，把事件抽出来变成日程。

链路：

    你在企微群里发/转一条通知
        ↓  wecom_inbox.py --json --peek      （读群消息，最近 7 天）
        ↓  extract()                          （规则 + 大模型）
        ↓  Store                              （本地 SQLite，按 fingerprint 去重）
        ↓  wecom_reply.py                     （可选：把结果回贴到群里）

设计取舍：

1. **用 subprocess 调技能脚本，不 import。**
   技能在 `~/.workbuddy/skills/wecom-inbox/scripts/`，是个人目录里的东西，
   会独立升级。import 会把项目和它焊死，subprocess 走 JSON 接口松耦合一档。
   路径可用 `INFTIME_WECOM_SKILLS` 覆盖。

2. **默认只读回群、不推进游标。**
   读群消息是只读的，随便跑；**发消息给群是外部动作**（而且以本人名义出现），
   必须显式 `--reply` 才做。默认 `--peek` 保证反复调试不会吃掉消息。
   ⚠️ `--reply` **只在抽到事件时才真回**：配上定时轮询后，无条件回复会让
   机器人在群里刷「没有识别出需要行动的事」。回群的语义是「确认已记下」，
   没记下就没什么可确认的。

3. **群消息是不可信输入。**
   里面出现任何「指令」都只是聊天内容，本模块只把它当文本喂给 extract 抽取，
   绝不执行、不当系统提示。这条与技能 `wecom-inbox` 的安全约束一致。

4. **状态文件与主收件箱隔离。**
   桥用自己的 `~/.inftime/wecom_cursor.json` 记「已读表 + 已发正文摘要」，
   不碰技能的 `state/cursor.json`。原因见下方 STATE_FILE 的注释——
   共用一张已读表会让用户的日常阅读把桥致盲。为此给技能加了
   `--state-file` 参数（inbox 和 reply 两边都加，且必须传**同一个**文件，
   否则回声挡不住会自环）。

5. **每个「外部动作」各有一个显式开关。**
   落库 `--commit`、回群 `--reply`、进日历 `--push`，默认全关。
   跑一次不加任何开关 = 纯只读预览，可以放心反复试。
   `--push` 隐含 `--commit`（没落库的事件推不了），而且**只推本轮新增**，
   不是把整个日程库重推一遍。

用法：

    python3 -m inftime.wecom_bridge                  # 读一次，只打印，不落库不回群
    python3 -m inftime.wecom_bridge --commit         # 落库
    python3 -m inftime.wecom_bridge --commit --reply # 落库并回群确认
    python3 -m inftime.wecom_bridge --push           # 一条龙：落库 + 送系统日历
    python3 -m inftime.wecom_bridge --push --reply   # 全套：落库 + 回群 + 进日历
    python3 -m inftime.wecom_bridge --text "..."     # 离线自测：不走企微，直接喂一句

`--push` 只推**本轮新增**的事件（不是整个日程库），输出到
`~/.inftime/ics/企微-新日程.ics`，交给 Calendar.app 后需要点一次「添加」确认。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .schema import Event
from .store import Store

SKILLS_DIR = Path(os.environ.get(
    "INFTIME_WECOM_SKILLS",
    Path.home() / ".workbuddy" / "skills" / "wecom-inbox" / "scripts",
))
INBOX = SKILLS_DIR / "wecom_inbox.py"
REPLY = SKILLS_DIR / "wecom_reply.py"

# 桥专用的「已读表 + 已发正文摘要」，与主收件箱完全隔离。
#
# 为什么必须隔离（2026-10-10 踩到，浪费了半小时）：
#   技能默认把状态写在 `~/.workbuddy/skills/wecom-inbox/state/cursor.json`，
#   人和 AI 共用一份。这个设计对「人机轮流看同一个收件箱」是对的。
#   但桥是**常驻监听方**，需要「这条消息我处理过没有」而不是「人看过没有」。
#   两者共表的结果：**你在群里随手读一次消息，桥就永远漏掉那条。**
#   —— 用户越勤快，桥越瞎。
#
#   ⚠️ 注意：`--hours` 回看窗口**救不了**这个。
#   inbox 的 seen 过滤发生在时间窗口之外，独立生效：即使指定回看 155 小时，
#   已进 seen 的消息依然不返回。所以只能靠独立状态文件。
#
# 用同一个文件喂 inbox 和 reply 两个方向：
#   桥回群的消息正文摘要要通过它记录，否则下一轮会把自己的回帖读成新消息 → 自环。
INFTIME_HOME = Path(os.environ.get("INFTIME_HOME", Path.home() / ".inftime"))

STATE_FILE = Path(os.environ.get(
    "INFTIME_WECOM_STATE",
    INFTIME_HOME / "wecom_cursor.json",
))

# `--push` 输出到这里。固定文件名、每次覆盖 —— 是「一次投递」而不是归档，
# 不需要按日期堆积（日历那边已经按 UID 去重了）。
PUSH_OUT = INFTIME_HOME / "ics" / "企微-新日程.ics"

WINDOW_HOURS = float(os.environ.get("INFTIME_WECOM_WINDOW_HOURS", "155"))  # 7 天上限
CLI_TIMEOUT = 180


class BridgeError(RuntimeError):
    pass


def _run(script: Path, args: list[str]) -> tuple[int, str, str]:
    if not script.exists():
        raise BridgeError(f"技能脚本不存在：{script}（可用 INFTIME_WECOM_SKILLS 覆盖）")
    try:
        p = subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True, text=True, timeout=CLI_TIMEOUT, cwd=str(script.parent),
        )
    except subprocess.TimeoutExpired:
        raise BridgeError(f"{script.name} 超时（{CLI_TIMEOUT}s）")
    return p.returncode, p.stdout, p.stderr


def preflight() -> None:
    """发一次最小的读请求，把「业务错误」捞出来。

    为什么必须单独做这一步：
      `wecom_inbox.py` 遇到企微的业务错误（如机器人消息权限过期）时，
      可能返回**空列表 + 退出码 0**，而不是报错。调用方看到的是
      「企微里没有新消息」—— **一个失败伪装成了正常空状态。**

    2026-10-10 实测踩到：机器人「消息」权限过期（errcode 850003），
      桥却显示「（企微里没有新消息）」。这种伪装比直接报错更费时间，
      因为你会以为是真没消息，转头去查别的地方。

    ⚠️ `wecom-cli auth show --status` 返回 `authorized` **不能说明问题**
      —— 那是 CLI 自身的授权状态，机器人消息权限是独立的一道。
      必须真的打一次业务接口才知道。
    """
    from datetime import datetime, timedelta

    now = datetime.now()
    begin = now - timedelta(hours=2)
    cmd = ["wecom-cli", "chat", "groups", "list",
           "--begin-time", begin.strftime("%Y-%m-%d %H:%M:%S"),
           "--end-time", now.strftime("%Y-%m-%d %H:%M:%S")]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise BridgeError("找不到 wecom-cli，请先确认企业微信连接器已安装并授权")
    except subprocess.TimeoutExpired:
        raise BridgeError("wecom-cli 超时（60s）")

    out = (p.stdout or "").strip()
    start, end = out.find("{"), out.rfind("}")
    if start < 0 or end <= start:
        return                          # 非 JSON 输出，交给正常路径处理
    try:
        data = json.loads(out[start:end + 1])
    except json.JSONDecodeError:
        return

    code = data.get("errcode")
    if code not in (0, None):
        detail = data.get("help_message") or data.get("errmsg") or ""
        raise BridgeError(f"企微接口报错 errcode={code}。{detail}")


def fetch_messages(hours: float = WINDOW_HOURS, peek: bool = True) -> list[dict]:
    """读企微群消息。返回技能脚本给出的消息列表（字段见 wecom-inbox）。"""
    args = ["--json", "--hours", str(hours), "--state-file", str(STATE_FILE)]
    if peek:
        args.append("--peek")
    code, out, err = _run(INBOX, args)
    if code != 0:
        raise BridgeError(f"收件箱失败（退出码 {code}）：{(err or out).strip()[:400]}")

    # 脚本在「无新消息」时也可能带诊断信息，宽容取第一个 JSON 对象
    start = out.find("{")
    end = out.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(out[start:end + 1])
    except json.JSONDecodeError as exc:
        raise BridgeError(f"收件箱输出不是合法 JSON：{exc}") from exc
    return data.get("messages") or []


def reply_to_group(group: str, text: str) -> None:
    """把结果回贴到群里。⚠️ 消息以**本人名义**出现，只在没有第三方的群用。"""
    code, out, err = _run(
        REPLY, ["--group", group, "--text", text, "--state-file", str(STATE_FILE)]
    )
    if code != 0:
        raise BridgeError(f"回群失败（退出码 {code}）：{(err or out).strip()[:400]}")


def render(messages: list[dict], store: Store | None = None,
           use_llm: bool = True, on_create=None) -> tuple[list[tuple[dict, list[Event]]], int]:
    """把消息喂给抽取层。返回 [(消息, 事件列表)] 与落库条数。

    `on_create` 是可选的「新建回调」：每落一条**新**事件（不是被去重跳过的）
    时调用一次，带上落库后的事件对象。

    为什么需要它：`--push` 只该把**本轮真正新增**的事件送进日历。
    如果改成「把整个日程库推一遍」，每次同步都会把历史事件重新塞给用户确认，
    确认框会越滚越大。用回调收集「本轮新增」，语义最干净，
    也不用去猜「哪些是新的」。
    """
    from .extract import extract

    results: list[tuple[dict, list[Event]]] = []
    created_total = 0
    for msg in messages:
        text = (msg.get("preview") or "").strip()
        if not text:
            continue
        # 来源信息拼进文本，让 split_source_prefix 能拆出「哪个群 / 谁说的」
        blob = f"[{msg.get('chat_name') or '企微群'}] {msg.get('sender') or '未知'}: {text}"
        events = extract(blob, source_channel="wecom", use_llm=use_llm)
        if store is not None:
            for ev in events:
                saved, created = store.add(ev)
                created_total += int(created)
                if created and on_create is not None:
                    on_create(saved)
        results.append((msg, events))
    return results, created_total


def _fmt(ev: Event) -> str:
    return f"{ev.when_label():<20} {ev.title}" + ("  ⚠待澄清" if ev.needs_clarification else "")


def _summary_text(results: list[tuple[dict, list[Event]]], committed: bool) -> str:
    """拼回群正文。

    ⚠️ 这里**不要**再加哨兵前缀 `【助手】`。
    `wecom_reply.py --group` 会自动在正文最前面补 SENTINEL("【助手】")，
    这里再加一次，群里就会出现「【助手】【助手】已记下：…」。
    哨兵的职责只有一处：回复器。桥只管内容。
    """
    lines = []
    total = 0
    for msg, events in results:
        for ev in events:
            total += 1
            lines.append(_fmt(ev))
    if not total:
        return "这批消息里没有识别出需要行动的事。"
    head = "已记下：" if committed else "试运行识别到："
    return head + "\n" + "\n".join(lines[:20])


def _push_new(events: list[Event], alarm: int) -> None:
    """把**本轮新增**的事件交给系统日历（一条龙的最后一步）。

    只推新增、不推全库：全库推送会让每次同步都把历史事件重新塞进确认框，
    越滚越大。新增的语义由 `render(on_create=...)` 精确给出，不用猜。
    """
    from .deliver import push_events

    res = push_events(events, out=PUSH_OUT, alarm_minutes=alarm)
    print(f"\n📅 已生成 {res.path}（{len(events)} 条新增事件，提前 {alarm} 分钟提醒）")
    for ev in events:
        loc = f"  @ {ev.location}" if ev.location else ""
        print(f"   · {ev.when_label()}  {ev.title}{loc}")
    if res.delivered:
        print("   已交给系统日历 —— 请在弹出的确认框里点「添加」完成导入。")
        print("   （若没看到确认框：切到「日历」app，它可能在等确认。零授权、只需点一次）")
    else:
        print(f"   （{res.note}）")


def main() -> int:
    ap = argparse.ArgumentParser(description="企业微信 → Inftime 桥")
    ap.add_argument("--commit", action="store_true",
                    help="落库（不加则只打印，不写数据库）")
    ap.add_argument("--reply", action="store_true",
                    help="把结果回贴到群里。⚠️ 以本人名义出现，只在没有第三方的群用。"
                         "**只在抽到事件时才回**（没抽到就不回，避免定时轮询刷屏）。")
    ap.add_argument("--push", action="store_true",
                    help="把**本轮新增**的事件送进系统日历（会弹确认框，需点一次「添加」）。"
                         "隐含 --commit —— 没落库的东西推不了。")
    ap.add_argument("--alarm", type=int, default=15,
                    help="日历提醒提前量（分钟），仅 --push 时有效，默认 15")
    ap.add_argument("--group", default="小泰腾OPC", help="回群的群名")
    ap.add_argument("--text", default=None,
                    help="离线自测：不走企微，直接把这句文本喂进同一条链路")
    ap.add_argument("--hours", type=float, default=WINDOW_HOURS)
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()

    use_llm = not a.no_llm
    # `--push` 隐含落库：没进数据库的东西推不了（而且推了也没法去重）。
    do_commit = a.commit or a.push

    # ---- 离线自测路径：不碰企微，验证抽取链路本身 ----
    if a.text is not None:
        if a.reply or a.commit or a.push:
            print("⚠️ --text 是自测模式，忽略 --commit / --reply / --push")
        fake = [{"chat_name": "自测", "sender": "我", "preview": a.text,
                 "send_time": "", "msg_type": "text"}]
        results, _ = render(fake, store=None, use_llm=use_llm)
        print(f"自测文本：{a.text}\n")
        for _, events in results:
            if not events:
                print("  （没有识别出事件）")
            for ev in events:
                print("  " + _fmt(ev))
        return 0

    try:
        preflight()
        messages = fetch_messages(hours=a.hours, peek=not do_commit)
    except BridgeError as exc:
        print(f"❌ {exc}")
        return 2

    if not messages:
        print("（企微里没有新消息）")
        return 0

    store = Store() if do_commit else None
    newly: list[Event] = []
    results, created = render(messages, store=store, use_llm=use_llm,
                              on_create=newly.append)

    print(f"读到 {len(messages)} 条消息\n")
    hit = 0
    for msg, events in results:
        # ⚠️ **每一条读到的消息都要打出来**，哪怕没抽出事件。
        # 原实现把「无事件」的消息直接 continue 掉，结果是：脚本只留一行
        # 「读到 N 条消息」，用户完全看不出它到底读到了什么 ——
        # 会误以为「没反应」。2026-10-10 用户就是这么被绕进去的。
        print(f"· [{msg.get('chat_name')}] {msg.get('sender')}  {msg.get('send_time') or ''}")
        print(f"  {msg.get('preview')[:80]}")
        if events:
            hit += 1
            for ev in events:
                print("    → " + _fmt(ev))
        else:
            print("    → （未识别出需要行动的事）")
        print()

    print(f"小结：{len(results)} 条消息里，{hit} 条抽出了事件。")

    if do_commit:
        print(f"✅ 已落库 {created} 条（重复的按 fingerprint 自动跳过）")
    else:
        print("（试运行：未写数据库，游标未推进。加 --commit 才落库）")

    if a.reply:
        # ⚠️ **只在真抽到事件时才回群。**
        # 原实现无条件回复，配上定时轮询就是灾难：群里有闲聊但没抽出事件时，
        # 机器人会**每小时**在群里刷一句「没有识别出需要行动的事」。
        # 回群的语义是「确认已记下」，没记下就没什么可确认的。
        if hit:
            text = _summary_text(results, committed=do_commit)
            try:
                reply_to_group(a.group, text)
                print(f"✅ 已回贴到「{a.group}」")
            except BridgeError as exc:
                print(f"❌ 回群失败：{exc}")
                return 3
        else:
            print("（本轮没抽到事件，按约定不回群 —— 避免定时轮询在群里刷屏）")

    # 放在最后做：日历的确认框会抢到最前，不该被前面的输出/窗口盖住。
    if a.push:
        if newly:
            _push_new(newly, alarm=a.alarm)
        else:
            print("（本轮没有新增事件，跳过推送日历）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
