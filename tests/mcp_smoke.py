#!/usr/bin/env python3
"""
MCP 协议最小客户端 —— 用来验证 Inftime MCP Server 能不能被正常握手与调用。

不依赖任何 MCP SDK，直接按 stdio 协议发换行分隔的 JSON-RPC。
可以当冒烟测试跑，也可以当排错工具用。

    python tests/mcp_smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


class MCPClient:
    def __init__(self, cmd: list[str], cwd: str, env: dict | None = None):
        self.proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
        )
        self._id = 0

    def send(self, method: str, params: dict | None = None, notify: bool = False):
        self._id += 1
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = self._id
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read()
            raise RuntimeError(f"server 无响应。stderr:\n{err}")
        return json.loads(line)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def main() -> int:
    home = Path(os.environ.get("INFTIME_HOME", "/tmp/inftime_mcp_smoke"))
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, INFTIME_HOME=str(home))

    c = MCPClient([PY, "-m", "inftime.mcp_server"], cwd=str(ROOT), env=env)

    ok = 0
    total = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok, total
        total += 1
        ok += bool(cond)
        print(f"{'✅' if cond else '❌'} {name}" + (f"  {extra}" if extra else ""))

    # 1. initialize
    r = c.send("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "smoke", "version": "1"},
    })
    info = r.get("result", {})
    check("initialize 握手", "protocolVersion" in info)
    check("serverInfo 正确", info.get("serverInfo", {}).get("name") == "inftime",
          str(info.get("serverInfo")))
    check("声明 tools 能力", "tools" in info.get("capabilities", {}))

    c.send("notifications/initialized", notify=True)

    # 2. tools/list
    r = c.send("tools/list")
    tools = [t["name"] for t in r.get("result", {}).get("tools", [])]
    check("tools/list 返回 6 个工具", len(tools) == 6, str(tools))
    for expect in ("inftime_capture", "inftime_list", "inftime_confirm", "inftime_audit"):
        check(f"包含工具 {expect}", expect in tools)

    # 3. tools/call —— capture
    r = c.send("tools/call", {
        "name": "inftime_capture",
        "arguments": {"text": "教务处通知：10月20日上午9:30在综合楼305会议室开会，全体同学准时参加",
                      "channel": "wechat", "ref": "年级群", "use_llm": False},
    })
    text = r["result"]["content"][0]["text"]
    check("capture 返回内容", "综合楼305会议室" in text, text.splitlines()[0] if text else "")
    check("capture 未标记为错误", r["result"].get("isError") is False)

    # 4. tools/call —— list
    r = c.send("tools/call", {"name": "inftime_list", "arguments": {"scope": "all"}})
    text = r["result"]["content"][0]["text"]
    check("list 能读到刚写入的事件", "开会" in text or "年级" in text)

    # 5. 错误处理 —— 未知工具
    r = c.send("tools/call", {"name": "not_a_tool", "arguments": {}})
    check("未知工具返回 isError 而非崩溃", r["result"].get("isError") is True)

    # 6. 未知方法
    r = c.send("no/such/method", {})
    check("未知方法返回 JSON-RPC error", "error" in r)

    # 7. ping
    r = c.send("ping")
    check("ping 正常", "result" in r)

    c.close()
    print("-" * 44)
    print(f"MCP 冒烟测试：{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
