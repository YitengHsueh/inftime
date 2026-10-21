"""
Inftime MCP Server —— 手写 stdio 协议，零依赖。

MCP 的 stdio 传输本质就是「换行分隔的 JSON-RPC 2.0」。
不引入官方 SDK，换来的是一个重要属性：
    **任何人 clone 下来，`python -m inftime.mcp_server` 就能跑，不用 pip install 任何东西。**

暴露的工具：
    inftime_capture   吃掉一段消息，抽成事件并落库
    inftime_extract   只抽取不落库（干跑，看它怎么理解）
    inftime_list      列出事件（今天 / 待办 / 全部）
    inftime_confirm   确认某条事件，生成 .ics
    inftime_ignore    忽略某条事件（训练偏好用）
    inftime_audit     承诺审计：一段聊天记录 -> 一份遗忘率报告

所有数据只写本机 ~/.inftime/inftime.db，不联网（除非你自己配了模型）。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__
from .audit import render_markdown, run_audit
from .extract import extract
from .ics import write_calendar, write_single
from .llm import load_config
from .schema import Event
from .store import Store

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "inftime"
SERVER_TITLE = "Inftime · 时间助理"

INFTIME_HOME = Path(os.environ.get("INFTIME_HOME", Path.home() / ".inftime"))
OUT_DIR = INFTIME_HOME / "ics"


def log(*a: Any) -> None:
    """日志一律走 stderr —— stdout 是协议通道，写一个字符都会破坏握手。"""
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 工具实现

_TOOLS: dict[str, dict] = {}


def tool(name: str, description: str, schema: dict) -> Callable:
    def deco(fn: Callable) -> Callable:
        _TOOLS[name] = {"name": name, "description": description, "inputSchema": schema,
                        "fn": fn}
        return fn

    return deco


@tool(
    "inftime_capture",
    "把一段消息（群聊通知、邮件正文、转发的截图文字）转换成结构化日程事件并保存。"
    "返回被识别出的事件列表。这是最常用的入口。",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "原始消息文本，可以包含多件事"},
            "channel": {
                "type": "string",
                "enum": ["wechat", "mail", "clipboard", "screenshot", "manual"],
                "description": "消息来源渠道，默认 manual",
            },
            "ref": {"type": "string", "description": "来源说明，如群名或发件人"},
            "use_llm": {"type": "boolean", "description": "是否使用大模型增强语义理解，默认 true"},
        },
        "required": ["text"],
    },
)
def t_capture(args: dict, store: Store) -> str:
    text = str(args.get("text", ""))
    events = extract(
        text,
        source_channel=str(args.get("channel") or "manual"),
        source_ref=args.get("ref"),
        use_llm=bool(args.get("use_llm", True)),
    )
    if not events:
        return "这条消息里没有识别出需要行动的事（判定为闲聊或信息同步）。"

    lines = []
    for ev in events:
        _, created = store.add(ev)
        flag = "🆕" if created else "♻️ 已存在"
        lines.append(f"{flag} {ev.id}\n{ev.human()}")
    return "\n\n".join(lines)


@tool(
    "inftime_extract",
    "只抽取不保存。用来干跑检查它怎么理解一段文字，不产生任何副作用。",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "use_llm": {"type": "boolean"},
        },
        "required": ["text"],
    },
)
def t_extract(args: dict, store: Store) -> str:
    events = extract(str(args.get("text", "")),
                     source_channel="dryrun",
                     use_llm=bool(args.get("use_llm", True)))
    if not events:
        return "未识别到事件（判定为闲聊或信息同步）。"
    return "\n\n".join(e.to_json() for e in events)


@tool(
    "inftime_list",
    "列出已捕获的事件。scope 可选 today / upcoming / pending / all。",
    {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["today", "upcoming", "pending", "all"],
                      "description": "默认 upcoming"},
            "days": {"type": "integer", "description": "upcoming 的前瞻天数，默认 7"},
            "limit": {"type": "integer", "description": "最多返回条数，默认 30"},
        },
    },
)
def t_list(args: dict, store: Store) -> str:
    scope = str(args.get("scope") or "upcoming")
    days = int(args.get("days") or 7)
    limit = int(args.get("limit") or 30)

    if scope == "today":
        evs = store.today()[:limit]
    elif scope == "upcoming":
        evs = store.upcoming(days)[:limit]
    elif scope == "pending":
        evs = store.list(status="pending", limit=limit)
    else:
        evs = store.list(limit=limit)

    if not evs:
        return f"（{scope} 范围内没有事件）"

    head = f"{scope} · 共 {len(evs)} 条\n"
    body = []
    for e in evs:
        mark = "⏰" if e.is_deadline else "·"
        flag = " ⚠待澄清" if e.needs_clarification else ""
        body.append(f"{mark} [{e.id}] {e.when_label():16s} {e.title}{flag}")
    return head + "\n".join(body)


@tool(
    "inftime_confirm",
    "确认一条事件，生成 .ics 日历文件并返回文件路径。用户把这个文件拖进日历即可。",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "事件 id（可用前几位）"},
            "alarm_minutes": {"type": "integer", "description": "提前多少分钟提醒，默认 15"},
            "patch": {
                "type": "object",
                "description": "可选修正，如 {\"start\": \"2026-10-12T14:00:00+08:00\", \"title\": \"...\"}",
            },
        },
        "required": ["id"],
    },
)
def t_confirm(args: dict, store: Store) -> str:
    eid = str(args.get("id", "")).strip()
    ev = store.get(eid) or store.by_id_prefix(eid)
    if not ev:
        return f"找不到事件：{eid}"

    patch = args.get("patch") or {}
    if isinstance(patch, dict) and patch:
        ev = store.patch(ev.id, **patch) or ev

    ev = store.set_status(ev.id, "confirmed") or ev
    alarm = int(args.get("alarm_minutes") or 15)
    out = OUT_DIR / f"{ev.id}.ics"
    write_single(ev, out, alarm_minutes=alarm)

    return (f"✅ 已确认并生成日历文件：\n{out}\n\n{ev.human()}\n\n"
            f"把这个 .ics 拖进系统日历，或让日历订阅该文件即可。"
            f"（提醒：提前 {alarm} 分钟）")


@tool(
    "inftime_ignore",
    "把一条事件标为忽略。用于记录「这条你理解错了」，后续可用来调优。",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
)
def t_ignore(args: dict, store: Store) -> str:
    eid = str(args.get("id", "")).strip()
    ev = store.get(eid) or store.by_id_prefix(eid)
    if not ev:
        return f"找不到事件：{eid}"
    store.set_status(ev.id, "ignored")
    return f"已忽略 [{ev.id}] {ev.title}"


@tool(
    "inftime_audit",
    "承诺审计：输入一段聊天记录（一行一条，可带 [群名] 张三: 前缀），"
    "输出一份报告——你答应了哪些事、多少件没有时间、多少件其实已经过期。"
    "这是决定要不要继续做这个项目的关键数据。",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "聊天记录原文，一行一条消息"},
            "window_days": {"type": "integer", "description": "审计窗口天数，默认 30"},
            "use_llm": {"type": "boolean"},
        },
        "required": ["text"],
    },
)
def t_audit(args: dict, store: Store) -> str:
    res = run_audit(str(args.get("text", "")),
                    window_days=int(args.get("window_days") or 30),
                    use_llm=bool(args.get("use_llm", True)))
    return render_markdown(res)


HANDLERS: dict[str, Callable[[dict, Store], str]] = {
    name: spec["fn"] for name, spec in _TOOLS.items()
}


# ---------------------------------------------------------------- 协议实现

def _ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(req: dict, store: Store) -> Optional[dict]:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    # 通知（无 id）不需要响应
    is_notification = "id" not in req or req_id is None

    if method == "initialize":
        client_ver = params.get("protocolVersion") or PROTOCOL_VERSION
        cfg = load_config()
        return _ok(req_id, {
            "protocolVersion": client_ver,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": SERVER_TITLE, "version": __version__},
            "instructions": (
                "Inftime 把散落的中文消息变成日历事件。"
                "收到群通知、邮件、截图文字时调用 inftime_capture；"
                "用户问「我今天有什么」时调用 inftime_list(scope='today')；"
                f"确认后生成 .ics 文件。数据只存在本机 {INFTIME_HOME}。"
                f"当前语义增强：{'已启用 ' + cfg.model if cfg.usable else '未配置，使用规则引擎'}"
            ),
        })

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return _ok(req_id, {})

    if method == "tools/list":
        return _ok(req_id, {
            "tools": [
                {"name": s["name"], "description": s["description"], "inputSchema": s["inputSchema"]}
                for s in _TOOLS.values()
            ]
        })

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return _ok(req_id, {
                "content": [{"type": "text", "text": f"未知工具：{name}"}],
                "isError": True,
            })
        try:
            text = fn(args, store)
            return _ok(req_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:                      # 工具异常不能让 server 挂掉
            log("tool error:", name, traceback.format_exc())
            return _ok(req_id, {
                "content": [{"type": "text", "text": f"执行失败：{exc}"}],
                "isError": True,
            })

    if method in ("resources/list", "prompts/list"):
        key = "resources" if method.startswith("resources") else "prompts"
        return _ok(req_id, {key: []})

    if is_notification:
        return None

    return _err(req_id, -32601, f"Method not found: {method}")


def serve() -> None:
    store = Store()
    log(f"[inftime] MCP server v{__version__} on stdio · db={store.path}")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            log("[inftime] 无法解析的输入行，已跳过")
            continue

        if isinstance(req, list):       # 批量请求
            out = [r for r in (handle(x, store) for x in req) if r]
            if out:
                sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            continue

        resp = handle(req, store)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def main() -> None:
    try:
        serve()
    except (KeyboardInterrupt, BrokenPipeError):
        pass


if __name__ == "__main__":
    main()
