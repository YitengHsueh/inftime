"""
模型适配层 —— 可插拔、可选、默认关闭。

三条硬纪律：
  1. 只走 OpenAI 兼容协议（DeepSeek / Kimi / Qwen / 本地 vLLM / Ollama 全都兼容），
     不绑定任何一家厂商。
  2. 只用标准库 urllib，不引入 SDK。MCP Server 的分发体验比功能完备更重要。
  3. **没有配置就自动降级到规则引擎，绝不报错退出。**
     一个需要先申请 API Key 才能跑的工具，没人会去试。

配置优先级：环境变量 > ~/.inftime/config.json
    INFTIME_BASE_URL   如 https://api.deepseek.com/v1
    INFTIME_API_KEY
    INFTIME_MODEL      如 deepseek-chat / qwen-plus / gpt-4o-mini
    INFTIME_LLM=off    强制关闭模型，只用规则
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

CONFIG_PATH = Path(os.environ.get("INFTIME_HOME", Path.home() / ".inftime")) / "config.json"
TIMEOUT = float(os.environ.get("INFTIME_LLM_TIMEOUT", "45"))

# 探活请求的 token 上限。
#
# 这个数字不能小。GLM-5.3-Flash / DeepSeek-V4 这类**推理模型**是先烧 reasoning
# token、再产出 content 的。2026-10-10 实测：给 32 的上限时，32 个 token 全被
# reasoning 吃掉，返回 `content: ""` + `finish_reason: "length"` —— 探活会误判成
# 「调用成功但没内容」，把人引去查 key，而真正的原因是一个数字写小了。
# 给足余量：正常应答只需几十 token，留下的空间是给「想太久」的模型的。
PROBE_MAX_TOKENS = int(os.environ.get("INFTIME_PROBE_MAX_TOKENS", "512"))


@dataclass
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    enabled: bool = True

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.base_url) and bool(self.api_key) and bool(self.model)

    @property
    def masked(self) -> str:
        if not self.api_key:
            return "(未设置)"
        return self.api_key[:5] + "…" + self.api_key[-4:] if len(self.api_key) > 12 else "***"


def load_config() -> LLMConfig:
    cfg = LLMConfig()
    # 1) 配置文件
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text("utf-8"))
            cfg.base_url = data.get("base_url", "") or ""
            cfg.api_key = data.get("api_key", "") or ""
            cfg.model = data.get("model", "") or ""
            cfg.enabled = bool(data.get("enabled", True))
        except (json.JSONDecodeError, OSError):
            pass
    # 2) 环境变量覆盖
    cfg.base_url = os.environ.get("INFTIME_BASE_URL", cfg.base_url)
    cfg.api_key = os.environ.get("INFTIME_API_KEY", cfg.api_key)
    cfg.model = os.environ.get("INFTIME_MODEL", cfg.model)

    flag = os.environ.get("INFTIME_LLM", "").strip().lower()
    if flag in ("off", "0", "false", "no"):
        cfg.enabled = False
    elif flag in ("on", "1", "true", "yes"):
        cfg.enabled = True

    cfg.base_url = cfg.base_url.rstrip("/")
    return cfg


def chat_json(
    system: str,
    user: str,
    cfg: Optional[LLMConfig] = None,
    *,
    temperature: float = 0.0,
) -> Optional[dict[str, Any]]:
    """调用 OpenAI 兼容接口，返回解析后的 JSON。失败一律返回 None（调用方降级）。"""
    cfg = cfg or load_config()
    if not cfg.usable:
        return None

    payload = {
        "model": cfg.model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    # 支持 OpenAI 的 JSON mode；不支持的厂商会忽略这个字段
    payload["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        f"{cfg.base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            json.JSONDecodeError):
        return None

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return _loose_json(content)


def _loose_json(text: str) -> Optional[dict[str, Any]]:
    """模型偶尔会把 JSON 包在 ``` 里，或者前后带废话，宽松处理。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    text = text.strip()
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        pass
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            out = json.loads(text[a : b + 1])
            return out if isinstance(out, dict) else None
        except json.JSONDecodeError:
            return None
    return None


# 常见 OpenAI 兼容端点预设。
# 记这一张表的意义：base_url 写错是最常见的失败，而它只会以「网络不可达」这种
# 模糊症状出现。预设把这条路径上的人为错误直接消掉。
PRESETS: dict[str, dict[str, str]] = {
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        # 2026-10-10 实测：glm-4-flash 已下线，/models 只返回 glm-5.3-flash 系列。
        # 这类默认值本来就是「猜」，所以写入时一定要过 _check_model 核对。
        "model": "glm-5.3-flash",
        "hint": "智谱 GLM · Flash 系列官方免费，手机号注册即可，无需信用卡",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "hint": "DeepSeek · 需账户余额",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "hint": "硅基流动 · 注册送礼额度，部分小模型永久免费",
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "hint": "阿里云百炼 · 通义千问，有免费体验额度",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "hint": "月之暗面 Kimi",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "hint": "本地 Ollama · 零成本、数据不出本机（key 随便填，如 local）",
    },
}


def list_models(cfg: Optional[LLMConfig] = None) -> tuple[Optional[list[str]], str]:
    """向 `{base_url}/models` 要一份服务端真实模型清单。

    存在的意义：**模型 id 一律以服务端清单为准，不要按名字猜。**
    （2026-10-10 实测教训：猜的 `deepseek-v4-flash` 根本不存在，
      而余额为空时错误码是 402 而不是「模型不存在」，这个错误可以潜伏很久。）

    不是所有 OpenAI 兼容端点都实现这个接口，所以失败不算异常 —— 返回 (None, 原因)。
    """
    cfg = cfg or load_config()
    if not cfg.base_url:
        return None, "未配置 base_url"

    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    req = urllib.request.Request(f"{cfg.base_url}/models", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return None, f"{e}"

    data = body.get("data") if isinstance(body, dict) else body
    if not isinstance(data, list):
        return None, "返回结构里没有 data 数组"
    ids = [d["id"] for d in data if isinstance(d, dict) and d.get("id")]
    if not ids:
        return None, "清单为空"
    return sorted(ids), f"{len(ids)} 个"


def write_config(base_url: str, api_key: str, model: str) -> Path:
    """写入配置文件（权限 0600，不要让 key 被别的进程读到）。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(
            {"base_url": base_url, "api_key": api_key, "model": model, "enabled": True},
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    return CONFIG_PATH


def probe(cfg: Optional[LLMConfig] = None) -> tuple[bool, str]:
    """真连一次，验证 base_url / api_key / model 是否可用。

    和 chat_json 的区别：这里把 HTTP 错误原文带回来，
    因为「配错了」和「模型不稳定」是两回事，报错要能区分。
    """
    cfg = cfg or load_config()
    if not cfg.base_url:
        return False, "未配置 base_url"
    if not cfg.api_key:
        return False, "未配置 api_key"
    if not cfg.model:
        return False, "未配置 model"

    payload = {
        "model": cfg.model,
        "temperature": 0,
        "max_tokens": PROBE_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": "你是一个测试助手，只输出 JSON。"},
            {"role": "user", "content": '返回 {"ok": true}'},
        ],
    }
    req = urllib.request.Request(
        f"{cfg.base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg.api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:400]
        except Exception:
            pass
        hints = {
            401: "key 无效或已过期",
            402: "余额不足",
            403: "该 key 没有调用此模型的权限",
            404: "base_url 或 model 名称不对",
            429: "触发限流",
        }
        return False, f"HTTP {e.code}（{hints.get(e.code, '未知错误')}）\n{detail}"
    except urllib.error.URLError as e:
        return False, f"网络不可达：{e.reason}（检查 base_url 或代理）"
    except (TimeoutError, OSError) as e:
        return False, f"请求失败：{e}"

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return False, f"返回结构异常：{json.dumps(body, ensure_ascii=False)[:400]}"

    # 二次防线：HTTP 200 但内容为空，多半是 token 上限被推理过程吃光。
    # 这种情况绝不能报「调用成功」—— 否则使用者会去查 key，而问题在一个数字上。
    if not (content or "").strip():
        finish = body["choices"][0].get("finish_reason") if body.get("choices") else "?"
        rt = ((body.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
        extra = f"，reasoning_tokens={rt}" if rt else ""
        return False, (f"模型返回空内容（finish_reason={finish}{extra}）。"
                       f"推理模型会先烧 reasoning token，"
                       f"若把 max_tokens 设小（当前 {PROBE_MAX_TOKENS}）就会这样。")

    usage = body.get("usage") or {}
    return True, (f"调用成功 · 模型 {body.get('model', cfg.model)} · "
                  f"返回 {content.strip()[:80]} · tokens {usage.get('total_tokens', '?')}")
