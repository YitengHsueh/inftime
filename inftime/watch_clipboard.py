"""
剪贴板监听 —— 打通 W3 的第一条输入通道。

现实中「消息 → 日程」最难的一步不是理解，是**捕获**。
你看到一条重要群通知，最自然的动作是 Cmd+C，而不是切换到另一个 App 填表。

所以：监听剪贴板（macOS 用 pbpaste，零依赖），一旦发现复制的内容像一条待办，
就抽成事件并弹系统通知。顺手，且完全不打断你原来的操作。

用法：
    python -m inftime.watch_clipboard              # 前台运行
    python -m inftime.watch_clipboard --dry-run    # 只提示不落库

想常驻，用 launchd 或 tmux，别用 nohup（本环境里会话结束会被回收）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

from .extract import extract
from .store import Store

MIN_LEN = 8
MAX_LEN = 800
POLL = 1.2

# 不像「事」的内容：纯链接、纯数字、代码片段
import re

_NOISE = re.compile(r"^(https?://\S+|\d+|[a-zA-Z0-9_\-]{1,40})$")

# --------------------------------------------------------------- 凭据过滤
#
# 为什么必须单独有一层：
#   剪贴板是**全机器最脏的数据源**。你复制密码、复制数据库连接串、复制 API key
#   都会经过它。而 _NOISE 只挡纯链接/纯数字/纯英文串，**完全挡不住凭据**。
#   2026-10-10 实测：合成密码 "Tq7#mKp2!vZx9"、postgres 连接串、
#   形如 xxx.yyy 的 API key、带空格的银行卡号 —— 全部会通过过滤，
#   然后被 use_llm=True 原样发到大模型厂商的服务器上。
#
# 这跟项目第一条原则（本地优先、数据不出本机）直接冲突，所以：
#   命中疑似凭据 → **直接丢弃，连本地规则都不跑，更不上云**，并明确告诉用户跳过了。
#
# 宁可漏掉一条真待办，也不能把密码送出去。

_SENSITIVE_WORDS = re.compile(
    r"(?i)password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key"
    r"|密码|口令|密钥|私钥|验证码|身份证|银行卡|信用卡|护照"
)

_SENSITIVE_PATTERNS = (
    re.compile(r"://[^\s/:@]+:[^\s/:@]+@"),      # scheme://user:pass@host
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),     # 手机号
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),    # 身份证
    re.compile(r"(?<!\d)(?:\d[ -]?){15,19}(?!\d)"),  # 银行卡
)

# 疑似密钥/密码的「无空格串」。
#
# 两个坑（都被测试抓出来过）：
#   ① 字符类必须包含 !@#$%^&*+=?#~ 这些密码常用符号，否则 "Tq7#mKp2!vZx9"
#      会被切成一堆短碎片，长度不够，直接漏网。
#   ② 不能只靠长度判断。13 位的密码很短，但字符类别混杂；
#      而 41 位全小写的英文单词应该放过。所以判据是**字符类别数**，不是长度。
_TOKEN = re.compile(r"[A-Za-z0-9_.\-!@#$%^&*+=?~]{8,}")


def _looks_like_secret_token(tok: str) -> bool:
    """含 3 类以上字符的无空格串 = 像密码/令牌。

    类别：小写 / 大写 / 数字 / 符号。英文单词只有 1~2 类，正常中文句子里
    根本切不出这么长的 ASCII 串，所以误杀面很小。
    误杀的代价只是「跳过并提示」，可以接受；放过的代价是密码上云，不可接受。
    """
    classes = sum(bool(re.search(c, tok))
                  for c in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    return classes >= 3


def looks_sensitive(text: str) -> str:
    """返回命中的类型名；空串表示看起来安全。

    返回类型名而不是布尔值，是为了让提示能说清「因为像密码所以跳过了」，
    而不是含糊地说「跳过了」——含糊的提示会让人以为工具坏了。
    """
    m = _SENSITIVE_WORDS.search(text or "")
    if m:
        return f"含敏感词「{m.group(0)}」"
    for p in _SENSITIVE_PATTERNS:
        if p.search(text or ""):
            return "匹配证号/卡号/连接串格式"
    for tok in _TOKEN.findall(text or ""):
        if _looks_like_secret_token(tok):
            return "疑似密码/密钥（大小写+数字+符号混杂的无空格串）"
    return ""


def read_clipboard() -> str:
    try:
        out = subprocess.run(["pbpaste"], capture_output=True, timeout=4)
        return out.stdout.decode("utf-8", "ignore")
    except (OSError, subprocess.SubprocessError):
        return ""


def notify(title: str, message: str) -> None:
    """系统通知（macOS）。失败不致命。"""
    safe_t = title.replace('"', "'")
    safe_m = message.replace('"', "'")[:220]
    script = f'display notification "{safe_m}" with title "{safe_t}"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=6)
    except (OSError, subprocess.SubprocessError):
        pass
    print(f"[inftime] {title}: {message}")


def watch(dry_run: bool = False, use_llm: bool = True, interval: float = POLL) -> None:
    store = Store()
    last = ""
    seen: set[str] = set()
    print(f"[inftime] 剪贴板监听中… db={store.path}（Ctrl+C 退出）")
    if dry_run:
        print("[inftime] dry-run 模式：只提示，不写入数据库")

    while True:
        try:
            cur = read_clipboard().strip()
        except KeyboardInterrupt:
            raise
        if cur and cur != last:
            last = cur
            if MIN_LEN <= len(cur) <= MAX_LEN and not _NOISE.match(cur) and cur not in seen:
                seen.add(cur)

                # 凭据闸门：命中就直接跳过，连本地规则都不跑，更不上云。
                hit = looks_sensitive(cur)
                if hit:
                    print(f"[inftime] 已跳过疑似凭据内容（{hit}）—— 未发送到任何外部服务")
                    continue

                try:
                    events = extract(cur, source_channel="clipboard", use_llm=use_llm)
                except Exception as exc:            # 监听器绝不能因为一条脏数据退出
                    print(f"[inftime] 抽取失败：{exc}", file=sys.stderr)
                    events = []

                # 没有模型时 extract 会做保守过滤；这里再放宽一点，
                # 因为「用户主动复制」本身就是很强的意图信号
                if not events and not use_llm:
                    from .extract import rule_extract

                    ev = rule_extract(cur, source_channel="clipboard")
                    if ev.title and len(ev.title) >= 3:
                        events = [ev]

                for ev in events:
                    if not dry_run:
                        _, created = store.add(ev)
                        if not created:
                            continue
                    when = (ev.start or "时间未定")[:16].replace("T", " ")
                    notify("Inftime 捕获到一件事", f"{when} · {ev.title}")
        time.sleep(interval)


def main() -> None:
    p = argparse.ArgumentParser(description="Inftime 剪贴板监听")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--interval", type=float, default=POLL)
    a = p.parse_args()
    try:
        watch(dry_run=a.dry_run, use_llm=not a.no_llm, interval=a.interval)
    except KeyboardInterrupt:
        print("\n[inftime] 已停止。")


if __name__ == "__main__":
    main()
