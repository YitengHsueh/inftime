"""生成可导入的 .shortcut 文件 —— 「Inftime记日程」的**全自动安装包**。

--------------------------------------------------------------------------
为什么有这个文件（2026-10-10 的处境）
--------------------------------------------------------------------------
`calendar_shortcut.py` 原本假设快捷指令**必须由人手动建**（拿不到 action
标识符、Shortcuts.sqlite 受 TCC 保护）。但实际卡住用户的不是脚本，
是编辑器里「传入输入内容 → 作为参数」这个藏在标题行蓝字里的设置 ——
用户找不到，三次截图都没解。

解法：**在文件层面生成整个快捷指令**：
  · 动作只用一个 `is.workflow.actions.runshellscript`（Run Shell Script），
    它的输入方式键 `InputMode` 明确写死 "to stdin"（shell 的天然模式），
    **彻底绕开 Run AppleScript 那个输入方式菜单**。
  · shell 读一行 → `osascript -` 跑 AppleScript（参数从 argv 取，
    与 Shortcuts 内的 {input, parameters} 解包方式不同，已实测）。
  · 产出未签名 plist → `shortcuts sign` 签名 → `open` 导入，
    用户只需点一次「添加快捷指令」。

用法：
    python3 -m inftime.make_shortcut -o /tmp/Inftime记日程.unsigned.shortcut
    shortcuts sign --mode anyone -i <unsigned> -o <signed>
    open <signed>

⚠️ 首次运行会弹一次「快捷指令想要访问日历」—— 授权一次之后全自动。
"""

from __future__ import annotations

import argparse
import plistlib
from pathlib import Path

from .calendar_shortcut import APPLESCRIPT

SHORTCUT_SCRIPT = '''\
# 薄壳：只负责调用执行脚本（2026-10-10 架构决定）
# 快捷指令的导入对用户是高摩擦操作（要手动点），所以**只导入一次、永不再改**；
# 全部逻辑放 ~/.inftime/write_event.sh，由 CLI 每次运行前自动安装 ——
# 以后修 bug = 改磁盘文件，零用户操作。
RUNNER="$HOME/.inftime/write_event.sh"
if [ ! -f "$RUNNER" ]; then
  echo "还没有初始化：先在终端跑一次 inftime auto-calendar，它会装好执行脚本。" >&2
  exit 1
fi
zsh "$RUNNER"
'''

LOGIC_SCRIPT = '''\
#!/bin/zsh
# 「Inftime记日程」的真正逻辑 —— 由 calendar_shortcut.run_shortcut() 每次运行前
# 自动安装，**手动改动会被下次运行覆盖**；要改逻辑请改 inftime/make_shortcut.py。
# 来料优先级：参数（文件/文本）→ stdin → CLI 约定文件
# （2026-10-10 实测：`shortcuts run` 不支持管道 stdin，`-i` 在本机也送不进
#  动作 —— 连显式 ShortcutInput 绑定都无效，所以 CLI 走约定文件兜底）
PAYLOAD="${INFTIME_PAYLOAD:-$HOME/.inftime/shortcut_input.txt}"
if [ $# -gt 0 ]; then
  if [ -f "$1" ]; then line=$(cat "$1"); else line="$*"; fi
else
  line=$(cat)
fi
if [ -z "$line" ] && [ -f "$PAYLOAD" ]; then
  line=$(cat "$PAYLOAD")
fi
if [ -z "$line" ]; then
  echo "没有收到输入。正常由 inftime auto-calendar 自动调用。" >&2
  exit 1
fi
# 日历没在跑时 tell 会报 -600「应用程序没有运行」，先拉起再写
pgrep -x Calendar >/dev/null 2>&1 || { open -a Calendar; sleep 2; }
osascript - "$line" <<'AS_END'
'''

LOGIC_FOOTER = '''\
AS_END
'''


def build_shell_script() -> str:
    """快捷指令里的脚本：薄壳（永久不变）。"""
    return SHORTCUT_SCRIPT


def build_logic_script() -> str:
    """~/.inftime/write_event.sh 的内容：真正逻辑（CLI 自动安装）。"""
    body = APPLESCRIPT
    # 两处适配点必须精确命中，否则生成的脚本跑不起来
    old_head = "on run {input, parameters}"
    old_input = "(input as text)"
    assert body.startswith(old_head), "APPLESCRIPT 模板头部变了，适配失效"
    assert old_input in body, "APPLESCRIPT 模板取输入的方式变了，适配失效"
    body = body.replace(old_head, "on run argv", 1)
    body = body.replace(old_input, "(item 1 of argv as text)", 1)
    # 输入段数防呆：错了要大声说，别让用户看到裸的下标错误
    body = body.replace(
        "\tset allDay to (item 7 of p) as integer",
        "\tif (count of p) is not 10 then\n"
        "\t\terror \"输入格式不对：需要 10 段（标题|年|月|日|时|分|全天|时长|地点|日历名），"
        "实际收到 \" & (count of p) & \" 段。\"\n"
        "\tend if\n"
        "\tset allDay to (item 7 of p) as integer",
        1,
    )
    assert "count of p) is not 10" in body, "防呆段没有注入成功"
    return LOGIC_SCRIPT + body + LOGIC_FOOTER


def _shortcut_input_binding() -> dict:
    """「输入」参数 → 快捷指令输入（ShortcutInput）变量的标准序列化。

    ⚠️ 2026-10-10 实测：不写这个绑定时，导入的快捷指令动作收不到任何输入
    （`shortcuts run -i` 也不行）—— 编辑器里「输入: 输入」看着像绑了，实际是空。
    """
    return {
        "Value": {
            "Type": "Variable",
            "VariableName": "ShortcutInput",
            "Aggrandizements": [],
        },
        "WFSerializationType": "WFTextTokenAttachment",
    }


def build_workflow() -> dict:
    """最小可导入的 .shortcut 顶层结构。"""
    return {
        "WFWorkflowActions": [
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.runshellscript",
                "WFWorkflowActionParameters": {
                    "Input": _shortcut_input_binding(),
                    "InputMode": "to stdin",
                    "Script": build_shell_script(),
                    "Shell": "/bin/zsh",
                },
            }
        ],
        "WFWorkflowClientVersion": "2605.0.5",
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowIcon": {
            "WFWorkflowIconGlyphNumber": 59446,
            "WFWorkflowIconStartColor": 4274264319,
        },
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": [
            "WFAppContentItem",
            "WFAppStoreAppContentItem",
            "WFArticleContentItem",
            "WFContactContentItem",
            "WFDateContentItem",
            "WFEmailAddressContentItem",
            "WFFolderContentItem",
            "WFGenericFileContentItem",
            "WFImageContentItem",
            "WFiTunesProductContentItem",
            "WFLocationContentItem",
            "WFDCMapsLinkContentItem",
            "WFAVAssetContentItem",
            "WFPDFContentItem",
            "WFPhoneNumberContentItem",
            "WFRichTextContentItem",
            "WFSafariWebPageContentItem",
            "WFStringContentItem",
            "WFURLContentItem",
        ],
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": ["NCWidget", "WatchKit"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成「Inftime记日程」未签名 .shortcut")
    ap.add_argument("-o", "--output", required=True, help="输出路径（未签名 plist）")
    a = ap.parse_args()
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        plistlib.dump(build_workflow(), f, fmt=plistlib.FMT_XML)
    print(f"已生成未签名快捷指令：{out}")
    print("下一步：")
    print(f"  shortcuts sign --mode anyone -i {out} -o <同目录>/Inftime记日程.shortcut")
    print("  open <签名后的文件>   → 在弹窗里点「添加快捷指令」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
