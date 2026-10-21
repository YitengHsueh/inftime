"""
通过 macOS 快捷指令把事件写进系统日历 —— **全自动、零弹窗**。

--------------------------------------------------------------------------
为什么需要这条路（2026-10-10 的处境）
--------------------------------------------------------------------------
把事件写进系统日历，此前只有两条路：

1. `open -a Calendar <ics>`  —— 零授权，但**每次都要点一次「添加」**。
   无人值守的轮询会让确认框堆积，等于不可用。
2. AppleScript 直写          —— 从这条进程链发起会返回 -10004，
   而且连授权框都不弹（进程没有 app 身份，系统不知道该向谁征求同意）。

快捷指令（Shortcuts.app）是第三条路：**它有正规的 app 身份**，
日历权限只需在第一次运行时授一次，之后 `shortcuts run` 从命令行调用
就是全自动的，没有任何弹窗。

安装路径（2026-10-10 实测更新）：
  · **首选：`make_shortcut.py` 程序化生成** —— 未签名 plist → `shortcuts sign`
    签名 → `open` 导入，用户只点一次「添加快捷指令」。实测 `shortcuts sign
    --mode anyone` 在本机可用（旧假设「无法生成可导入文件」已证伪）。
    生成动作是 Run Shell Script 包 osascript，输入方式写死 stdin，
    **绕开了 Run AppleScript 那个用户找不到的「传入输入内容」菜单**。
  · 备选：人手动建 —— 用本模块的 `APPLESCRIPT` 粘贴（模板里唯一要改的就是
    日历名，而日历名我们改成了从输入传入，用户**什么都不用改**），
    但必须手动把「传入输入内容」设成「作为参数」（简体中文版译名，
    argv 的官方翻译），否则报 `item 2 of {}`。
  · `push_via_shortcut()` —— 从命令行调用它

--------------------------------------------------------------------------
输入格式（管道分隔，10 段）
--------------------------------------------------------------------------
    标题|YYYY|MM|DD|HH|MM|allDay|时长分钟|地点|日历名

为什么把日期拆成数字段而不是传 ISO 字符串：
AppleScript **没有** ISO8601 解析器，手写字符串解析既长又容易错；
而「把数字逐个塞进 date 的组件」是 AppleScript 里最稳的日期构造法
（顺序必须是 年→月→日，否则 1月31日 + 月份改成2月会溢出成 3月3日）。

⚠️ 标题/地点里若有 `|`，会被替换成「，」—— 它是分隔符，不能出现在字段里。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

from .schema import Event

# ---------------------------------------------------------------- AppleScript

APPLESCRIPT = '''on run {input, parameters}
\tset _delims to AppleScript's text item delimiters
\tset AppleScript's text item delimiters to "|"
\tset p to text items of (input as text)
\tset AppleScript's text item delimiters to _delims

\t-- ⚠️ 组件必须按 年→月→日 的顺序设置，否则会发生日期溢出
\t-- （例如 1月31日，先把月份改成 2 月会得到 3 月 3 日）
\tset d to current date
\tset time of d to 0
\tset year of d to (item 2 of p) as integer
\tset month of d to (item 3 of p) as integer
\tset day of d to (item 4 of p) as integer

\tset allDay to (item 7 of p) as integer
\tset evLoc to item 9 of p
\tset calName to item 10 of p

\tif allDay is 1 then
\t\tset e to d + 1 * days
\telse
\t\tset hours of d to (item 5 of p) as integer
\t\tset minutes of d to (item 6 of p) as integer
\t\tset seconds of d to 0
\t\tset e to d + ((item 8 of p) as integer) * minutes
\tend if

\ttell application "Calendar"
\t\tset matched to (calendars whose name is calName)
\t\tif (count of matched) is 0 then
\t\t\terror "没有找到名为「" & calName & "」的日历。打开「日历」app 看侧边栏里的确切名字，然后在命令里用 --calendar 传入那个名字。"
\t\tend if
\t\ttell (item 1 of matched)
\t\t\tset newEv to make new event with properties {summary:(item 1 of p), start date:d, end date:e, location:evLoc}
\t\t\tif allDay is 1 then
\t\t\t\tset allday event of newEv to true
\t\t\tend if
\t\t\tif (item 8 of p) as integer is greater than 0 and allDay is 0 then
\t\t\t\tmake new sound alarm at end of sound alarms of newEv with properties {trigger interval:-((item 8 of p) as integer)}
\t\t\tend if
\t\tend tell
\tend tell
\treturn "ok"
end run
'''

# ---------------------------------------------------------------- 输入构造


def build_input(ev: Event, *, alarm_minutes: int = 15,
                calendar: str = "个人") -> str:
    """把一个事件编码成快捷指令期待的管道分隔文本。"""
    start = ev.effective_start
    if not start:
        raise ValueError(f"事件没有时间，无法进日历：{ev.title!r}")

    all_day = 1 if ev.effective_all_day else 0
    if all_day:
        hour = minute = 0                       # 占位；快捷指令会把它设成全天
        duration = 0
    else:
        # start 形如 2026-10-31T19:30:00+08:00
        hour = int(start[11:13])
        minute = int(start[14:16])
        if ev.effective_end:
            from datetime import datetime
            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.fromisoformat(ev.effective_end.replace("Z", "+00:00"))
            duration = max(1, int((e - s).total_seconds() // 60))
        else:
            duration = 60                        # 默认 1 小时

    def clean(s: str) -> str:
        return (s or "").replace("|", "，").strip()

    return "|".join([
        clean(ev.title),
        start[0:4], start[5:7], start[8:10],      # YYYY MM DD
        f"{hour}", f"{minute}",
        str(all_day), str(duration),
        clean(ev.effective_location or ""),
        clean(calendar),
    ])


# ---------------------------------------------------------------- 调用

SHORTCUT_NAME = "Inftime记日程"


class ShortcutResult(NamedTuple):
    event: Event
    ok: bool
    detail: str = ""


def shortcut_exists(name: str = SHORTCUT_NAME) -> bool:
    """快捷指令装没装。没装就提前失败，不要等到逐条调用时才炸。"""
    if shutil.which("shortcuts") is None:
        return False
    p = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=30)
    return name in (p.stdout or "").splitlines()


def payload_path() -> Path:
    """CLI → 快捷指令的约定文件（Plan C）。

    ⚠️ 2026-10-10 实测：`shortcuts run` 不支持管道 stdin；`-i <文件>` 在本机
    也送不进动作（连显式 ShortcutInput 绑定都无效）。所以走约定文件：
    CLI 写这里 → `shortcuts run` → 快捷指令读这里 → CLI 用完即删。
    """
    base = Path(os.environ.get("INFTIME_HOME", Path.home() / ".inftime"))
    return base / "shortcut_input.txt"


RUNNER_NAME = "write_event.sh"


def install_runner() -> Path:
    """把执行脚本装到 ~/.inftime/write_event.sh（每次运行前装，永远与代码同步）。

    架构（2026-10-10）：快捷指令只是**薄壳**（调这个文件），导入一次永不再改；
    逻辑全在这里，修 bug = 改 make_shortcut.LOGIC_SCRIPT，零用户操作。
    """
    from .make_shortcut import build_logic_script

    base = Path(os.environ.get("INFTIME_HOME", Path.home() / ".inftime"))
    base.mkdir(parents=True, exist_ok=True)
    runner = base / RUNNER_NAME
    runner.write_text(build_logic_script())
    os.chmod(runner, 0o700)
    return runner


def run_shortcut(text: str, name: str = SHORTCUT_NAME,
                 timeout: int = 60) -> tuple[bool, str]:
    """调用一次快捷指令：装执行脚本 + 写约定文件 → run → 删约定文件。"""
    install_runner()
    p_path = payload_path()
    fd = os.open(p_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    env = dict(os.environ, INFTIME_PAYLOAD=str(p_path))
    try:
        p = subprocess.run(
            ["shortcuts", "run", name],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    finally:
        try:
            p_path.unlink()
        except OSError:
            pass
    ok = p.returncode == 0
    detail = ((p.stderr or "") + (p.stdout or "")).strip()[:300]
    return ok, detail


def push_via_shortcut(events: Iterable[Event], *,
                      shortcut: str = SHORTCUT_NAME,
                      alarm_minutes: int = 15,
                      calendar: str = "个人") -> list[ShortcutResult]:
    """把事件逐条写进系统日历（全自动，不弹任何框）。"""
    out: list[ShortcutResult] = []
    for ev in events:
        if not ev.effective_start:
            out.append(ShortcutResult(ev, False, "没有时间，跳过"))
            continue
        try:
            ok, detail = run_shortcut(build_input(ev, alarm_minutes=alarm_minutes,
                                                  calendar=calendar),
                                      name=shortcut)
            out.append(ShortcutResult(ev, ok, detail))
        except subprocess.TimeoutExpired:
            out.append(ShortcutResult(ev, False, f"快捷指令超时（{timeout}s）"))
        except Exception as exc:                     # noqa: BLE001 —— 单条失败不该打断整批
            out.append(ShortcutResult(ev, False, str(exc)[:200]))
    return out
