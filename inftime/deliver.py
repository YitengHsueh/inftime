"""
把事件**送进系统日历** —— 捕获链路的最后一公里。

单独成模块的理由：`cli.py` 的 `push` 子命令和 `wecom_bridge` 的「一条龙」
都要用到它。抄两份必然分叉，所以抽出来共用。

--------------------------------------------------------------------------
为什么不做 AppleScript 直写（2026-10-10 实测，别再试了）
--------------------------------------------------------------------------
`osascript -e 'tell application "Calendar" to ...'` 从这条进程链
（WorkBuddy → python → `/usr/bin/osascript`）发起时，macOS 直接返回
**-10004「权限违例」，而且连授权弹窗都不弹** —— 系统无法给一个
没有 app 身份（bundle id）的进程弹窗，于是直接拒绝。

排查过程（省得下次重踩）：
  · `tell application "Finder" to get name` → **成功**，
    说明 AppleScript 通道本身没问题，是 Calendar 这一项单独拿不到授权；
  · Calendar 未启动时报 `-600 应用程序没有运行`，启动后变回 `-10004`，
    说明 -600 只是「没运行」，不是好消息；
  · 试图绕过：`Accounts4.sqlite` → `authorization denied`；
    复制 `Group Containers/group.com.apple.calendar/Calendar.sqlitedb`
    → **EPERM**（TCC 保护，2026-10-10 二次确认）。日历数据没有旁路。

所以改走 LaunchServices：把 .ics 交给 Calendar.app **自己**打开。
绕开 AppleScript 权限，零配置、零授权。

✅ 2026-10-10 实测（截图验证）：会弹出「要添加这些日程吗」确认框，
点「添加」后事件进入日历。**零授权，代价是点一次确认。**

⚠️ 踩过的坑：确认框**不一定自动跳到最前**。实测有过一次它压根没显示出来，
用户以为「没反应」——其实是在等确认。所以这里**先 `open -a Calendar`
把日历激活到前台，隔一下再送文件**（顺序有断言守着）。

🔴 我还在这里错过一次（比 bug 更重要）：
   我一度把它记成「静默导入、不弹框、全自动」，唯一证据是
   「用户说没看到弹框」+ `lsappinfo front` 显示前台 app 变成了「日历」。
   —— 我**把一次现象外推成了系统设计**，还据此改了 6 处文档。
   用户随后纠正：「不，还是要点确认的，是刚才没跳出，但确认后就成功了」。
   教训：**用户的第一手观察 > 我的间接推断；现象 ≠ 设计。**
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, NamedTuple

from .ics import write_calendar
from .schema import Event

DEFAULT_ALARM_MINUTES = 15

ICLOUD_DIR = (Path.home() / "Library" / "Mobile Documents"
              / "com~apple~CloudDocs" / "Inftime")


class PushResult(NamedTuple):
    path: Path
    delivered: bool          # 是否真的交给了系统日历
    icloud_copy: Path | None = None
    note: str = ""           # 未交付时的原因


def default_out_path() -> Path:
    """默认写 `~/.inftime/ics/`，**不是当前目录**。

    否则在源码目录里跑一次 `push` 就会多出一个 .ics，污染工作树。
    """
    from .store import DEFAULT_DB
    return DEFAULT_DB.parent / "ics" / "Inftime-日程.ics"


def push_events(events: Iterable[Event], *,
                out: Path | str | None = None,
                alarm_minutes: int = DEFAULT_ALARM_MINUTES,
                dry_run: bool = False,
                to_icloud: bool = False) -> PushResult:
    """把事件写成 .ics，并（非 dry-run 时）交给系统日历。

    返回值里的 `delivered` 表示**是否真的发起了交付动作**，
    不代表用户在确认框上点了「添加」—— 那一步只能由人完成，我们看不到。
    所以调用方千万不要据此打印「导入成功」，只能说「已交给日历」。
    """
    evs = list(events)
    path = Path(out).expanduser() if out else default_out_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_calendar(evs, path, alarm_minutes=alarm_minutes)

    icloud_copy: Path | None = None
    if to_icloud:
        ICLOUD_DIR.mkdir(parents=True, exist_ok=True)
        icloud_copy = ICLOUD_DIR / path.name
        shutil.copy2(path, icloud_copy)

    if dry_run:
        return PushResult(path, False, icloud_copy, "dry-run：没有打开日历")

    if sys.platform != "darwin":
        return PushResult(path, False, icloud_copy, "非 macOS：请手动导入")

    # 先把日历激活到前台，再把 .ics 交给它 —— 否则确认框有可能被别的窗口
    # 挡住/不跳到最前，用户会以为「没反应」。实测踩过这个坑。
    subprocess.run(["open", "-a", "Calendar"], check=False)
    time.sleep(1)
    subprocess.run(["open", "-a", "Calendar", str(path)], check=False)
    return PushResult(path, True, icloud_copy)
