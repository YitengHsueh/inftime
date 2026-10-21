# Inftime

![version](https://img.shields.io/badge/version-0.1.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![platform](https://img.shields.io/badge/core-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)

> 把你随手丢过来的任何中文消息，变成你日历里真正会发生的事件。**数据不出你的电脑。**

2018 年，我们几个同济在读的学生写过一份 54 页的商业计划书，想用 NLP 从微信群消息里
自动抽出日程。那年我们没做成——不是因为想法错了，是因为当时做这件事需要一个博士团队
干两三年。

2026 年重做这件事，只需要一个人和几周时间。但这个项目已经不该是一个 App 了。
**它应该是一个能力：一个本地跑、被 agent 调用的 MCP Server。**

---

## 它解决什么

你在 15 个群里，每天有几十条消息。真正冲着你来的可能只有三条：

> `@李工 请大家在本周五下午两点前把评审材料发我，务必不要拖`
> `教务处通知：10月20日上午9:30在综合楼305会议室召开年级大会`
> `你的私教课还剩3节，这个月内用完，过期作废`

剩下的 97% 是闲聊。而人脑不擅长从 97% 的噪音里捞出那 3%——**那 3% 就是你会忘掉的事。**

Inftime 做一件事：**吃进去一段消息，吐出一个可以进日历的事件。**

```
"请大家在本周五下午两点前把评审材料发我"
        ↓
{
  "title": "评审材料发我，务必不要拖",
  "start": "2026-10-16T14:00:00+08:00",
  "is_deadline": true,
  "time_granularity": "hour",
  "location": null,
  "participants": ["全体"],
  "priority": 5,
  "confidence": 0.87
}
        ↓
   20261016T060000Z  ← 可直接导入系统日历
```

---

## 三条设计原则

| 原则 | 为什么 |
| :--- | :--- |
| **零第三方依赖** | 只用 Python 标准库。clone 下来就能跑，不用 `pip install` 任何东西。分发体验是产品的一部分。 |
| **本地优先** | 数据只写 `~/.inftime/inftime.db`。日程和消息是最私密的数据之一，不出本机不是妥协，是卖点。 |
| **时间用规则算，语义交给模型** | LLM 在「算日期」上会幻觉（把周三算错一周、把 2026 说成 2025），但它在「判断这句话是不是一件事」上很强。各干各擅长的。 |

---

## 平台适用性

| 功能 | 平台 |
| :--- | :--- |
| 语义抽取、事件库、ICS 导出、MCP Server、审计 | **全平台**（macOS / Linux / Windows，Python 3.9+ 实测） |
| 剪贴板监听 | macOS / Windows |
| 快捷指令直写日历、企业微信群桥 | **仅 macOS**（依赖 Shortcuts 与 AppleScript，是本项目的「参考实现」而非核心） |

核心能力不绑定任何操作系统；macOS 相关的两个通道是作者自己的使用环境，
按需取用，代码在 `inftime/calendar_shortcut.py` 与 `inftime/wecom_bridge.py`。

---

## 快速开始

**要求：Python 3.9+（3.9.6 与 3.13 均实测全绿）。没有其他依赖。**

```bash
cd inftime

# 1. 自检（三个内置用例必须全绿）
python3 -m inftime.cli doctor

# 2. 试一条消息
python3 -m inftime.cli capture "明天下午三点在综合楼305会议室开会，请全体参加"

# 3. 看今天有什么
python3 -m inftime.cli today

# 4. 确认并生成日历文件
python3 -m inftime.cli confirm <事件id> --alarm 15 --out ~/Desktop/事件.ics
```

不需要配置任何 API Key。**默认走规则引擎，功能完整可用。**

---

## 接入 agent（MCP）

把这行加到你的 MCP 客户端配置里：

```json
{
  "mcpServers": {
    "inftime": {
      "command": "python3",
      "args": ["-m", "inftime.mcp_server"],
      "cwd": "/绝对路径/到/inftime"
    }
  }
}
```

配置文件位置参考：
- Claude Desktop：`~/Library/Application Support/Claude/claude_desktop_config.json`
- Cursor / WorkBuddy / 其他支持 MCP 的客户端：各自的 MCP 设置面板

装好之后，你可以直接说：

> 「把这条消息记下来：下周三下午三点面谈」
> 「我今天有什么事？」
> 「帮我审计一下这段聊天记录，我答应了哪些事没做」

### 暴露的 6 个工具

| 工具 | 用途 |
| :--- | :--- |
| `inftime_capture` | 吃掉一段消息，抽成事件并落库 |
| `inftime_extract` | 只抽取不保存（干跑，看它怎么理解） |
| `inftime_list` | 列出事件（today / upcoming / pending / all） |
| `inftime_confirm` | 确认事件并生成 `.ics` 日历文件 |
| `inftime_ignore` | 忽略某条（记录「你理解错了」，用于后续调优） |
| `inftime_audit` | 承诺审计：一段聊天记录 → 一份遗忘率报告 |

---

## 打开大模型增强（可选）

不配也能用。配了之后，语义判断（这条是不是事、事是什么、涉及谁）会明显更准，
尤其是「那你记得去把这件事做好」这类指代不明的句子。

任何 **OpenAI 兼容**接口都行。常用的六个已经内置成预设，**不用记地址**：

```bash
python3 -m inftime.cli config --use zhipu 你的key --test
```

| 预设 | 端点 | 费用 |
| :--- | :--- | :--- |
| `zhipu` | 智谱 GLM | **Flash 系列官方免费**，手机号注册即可，无需信用卡 |
| `deepseek` | DeepSeek | 需账户余额 |
| `siliconflow` | 硅基流动 | 注册送礼额度，部分小模型永久免费 |
| `dashscope` | 阿里云百炼 | 通义千问，有免费体验额度 |
| `moonshot` | 月之暗面 Kimi | 按量计费 |
| `ollama` | 本地 Ollama | **零成本，数据不出本机**（key 随便填） |

想手动指定（任意自建端点、vLLM 等）：

```bash
python3 -m inftime.cli config --set https://你的地址/v1 sk-你的key 模型名 --test
```

### 模型名不要去猜

```bash
python3 -m inftime.cli config --models   # 列出该端点真实提供的全部模型 id
```

写入配置时，程序会**自动拿服务端清单核对模型名**，对不上当场报错并列出可选项。

> 这条防线是一次真实教训的产物：2026-10-10 实测发现 DeepSeek 的
> `/chat/completions` 是**先查余额、后校验模型名** —— 余额为空时，无论模型名
> 对不对都返回 `402`。也就是说**写错的模型名会一路潜伏到你充值成功那一刻才爆**。
> 所以别猜，问服务端要清单。（另外：不是所有端点都实现 `/models`，
> 这种情况会跳过校验并提示，不会挡住你。）

`--test` 会真发一次请求验证，并区分「key 错了 / 余额不足 / 没权限 / 地址或模型名不对 / 限流 / 网络不通」。
配完想单独复测：`python3 -m inftime.cli config --test`。只想换模型：`config --model <新模型名>`。

配置写进 `~/.inftime/config.json`（权限 600）：

```json
{ "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": "...", "model": "glm-5.3-flash" }
```

> 智谱的 key 形如 `xxxxx.yyyyy`，中间的点号是 key 的一部分，**不要拆开**。

### 推理模型的坑：max_tokens 给小了会返回空内容

GLM-5.3-Flash、DeepSeek-V4 这类模型是**先烧 reasoning token、再产出 content**。
探活请求若只给 32 个 token，预算会在模型开口之前就用光，返回
`content: ""` + `finish_reason: "length"` —— **HTTP 200，看起来一切正常，实际什么都没有**。

这比直接报错更难查：人会去怀疑 key，而问题在一个数字上。现在探活默认给 512
（`INFTIME_PROBE_MAX_TOKENS` 可覆盖），并把「空内容」明确判为失败、在报错里直说是推理吃光了预算。

实测（glm-5.3-flash）：

| max_tokens | 结果 |
| :--- | :--- |
| 32 | `content=''`，`finish_reason='length'`，reasoning_tokens=30 |
| 512 | `content='{"ok": true}'`，`finish_reason='stop'` |

另外，`response_format: {"type": "json_object"}` 智谱是支持的，无需降级。

环境变量优先级更高：`INFTIME_BASE_URL` / `INFTIME_API_KEY` / `INFTIME_MODEL`。
临时关掉模型：`INFTIME_LLM=off`。

> **key 放在哪**：只存 `~/.inftime/config.json`（600），**不写进 MCP 配置**。
> 所以分享 MCP 配置给别人时不会连带泄露凭据。

---

## 捕获通道

现实里最难的一步不是理解，是**捕获**。你看到一条通知，最自然的动作是 Cmd+C，
而不是切换 App 填表。

### 剪贴板监听（已内置）

```bash
python3 -m inftime.watch_clipboard            # 前台运行
python3 -m inftime.watch_clipboard --dry-run  # 只看它怎么理解，不落库
```

复制任何像待办的内容，系统通知会告诉你捕获到了什么。

> ### ⚠️ 剪贴板是全机器最脏的数据源，所以有一条硬闸门
>
> 剪贴板会流过你的密码、数据库连接串、API key、银行卡号。而这个工具的
> `use_llm=True` 意味着内容会**发到大模型厂商的服务器上**。
>
> 2026-10-10 实测：原来的过滤只有一个「纯链接 / 纯数字 / 纯英文串」规则，
> **完全挡不住凭据**——合成密码、postgres 连接串、`xxx.yyy` 形式的 API key、
> 带空格的银行卡号，全部会通过并上云。这跟项目第一条原则（本地优先、数据不出本机）
> 直接冲突。
>
> 现在有一道独立闸门 `looks_sensitive()`，命中即**直接丢弃、连本地规则都不跑、更不上云**，
> 并打印跳过原因：
>
> | 判定依据 | 例子（均为合成样本） |
> | :--- | :--- |
> | 敏感关键词 | `password` `token` `api_key` `密码` `口令` `身份证` `银行卡` |
> | 连接串格式 | `postgres://user:pass@host:5432/db` |
> | 证号 / 卡号 | 手机号、18 位身份证、15~19 位卡号 |
> | 密码/密钥特征 | **大小写+数字+符号混杂的无空格串**（≥3 类字符） |
>
> 判据是**字符类别数**而不是长度——13 位的密码很短但类别混杂，
> 41 位全小写的英文单词应该放过。误杀的代价只是「跳过并提示」，
> 放过的代价是密码上云，两者不对等。
>
> 想彻底不上云：用 `--no-llm`，全程本地规则，一个字都不发出去。

### macOS 快捷指令（推荐，3 分钟配好）

打开「快捷指令」App → 新建：

```
1. 接收「共享」表单输入（文本、图片、Safari 网页）
2. 如果输入是图片 → 用「从图像中提取文本」
3. 运行 Shell 脚本：
     cd /绝对路径/到/inftime
     python3 -m inftime.cli capture "$1" --channel clipboard
```

保存后，在微信里长按消息 → 分享 → 选这个快捷指令。**这就是「转发一条消息给我，
它进日历」的完整体验。**

### 企业微信群 → 自动抽取（可选）

这是**最接近 2018 年那份 BP 原始设想**的一条路：从群消息的源头抽事件。

```bash
python3 -m inftime.wecom_bridge                  # 读一次，只打印，不落库不回群
python3 -m inftime.wecom_bridge --commit         # 落库
python3 -m inftime.wecom_bridge --commit --reply # 落库并回群确认
python3 -m inftime.wecom_bridge --push           # 一条龙：落库 + 送系统日历
python3 -m inftime.wecom_bridge --push --reply   # 全套：落库 + 回群 + 进日历
```

> ⚠️ **这不是常驻监听。** `wecom_bridge` 是**一次性命令** —— 它只在你（或调度器）
> 唤起它时读一轮。你在群里发消息的那一刻，**没有任何进程在听**。
> 所以「我在群里说了个日程，怎么没反应」的默认答案是：
> **需要跑一次桥**（或给它配个定时器，见下）。
> 别把它当成「发出去就会自动进日历」的聊天机器人 —— 它不是。

**一条龙（2026-10-10 接通）**：`--push` 是在原有的「读 → 抽 → 落库 → 回群」
之后再接一步「进日历」，于是**群里发一条通知，最后会变成一个带提醒的日历事件**：

```
企微群消息 → wecom_inbox → extract() → Store(SQLite) → ics(+VALARM)
    → open -a Calendar → 你在确认框点「添加」 → iPhone 到点提醒
```

三个细节，都是有意为之：

- **`--push` 隐含 `--commit`**：没落库的事件推不了，也没法靠 fingerprint 去重。
- **只推「本轮新增」的事件**，不是整个日程库。否则每同步一次，确认框里
  就重新冒出全部历史事件，点到手软。新增的语义由 `render(on_create=...)`
  精确给出，不靠猜。
- **`--push` 放在流程最后**：日历确认框要抢到最前，不该被前面的输出盖住。

依赖技能 `wecom-inbox`（脚本在 `~/.workbuddy/skills/wecom-inbox/scripts/`，
可用 `INFTIME_WECOM_SKILLS` 覆盖路径）。

**为什么默认不落库、不回群、不进日历**：读群消息是只读的，随便跑；
但**回群消息以本人名义出现**、**弹日历确认框打断你**，都是外部动作。
三个开关（`--commit` / `--reply` / `--push`）默认全关，
跑一次不加任何开关 = 纯只读预览，可以放心反复试。
默认 `--peek` 保证反复调试不会吃掉消息游标。

**状态文件是隔离的**：桥用 `~/.inftime/wecom_cursor.json` 记自己的「已读表」，
不碰技能的 `state/cursor.json`（可用 `INFTIME_WECOM_STATE` 覆盖）。原因见下面第 3 条坑。

> **三个必须知道的坑**
>
> 1. **`wecom-cli auth show --status` 返回 `authorized` 不代表能用。**
>    那是 CLI 自身的授权状态；机器人「消息」权限是**独立的一道**，会单独过期，
>    过期时接口返回 `errcode 850003`。
> 2. **业务错误会被伪装成「没有新消息」。** 收件箱脚本在权限过期时返回的是
>    *空列表 + 退出码 0*，桥会显示「（企微里没有新消息）」。
>    已加 `preflight()` 单独发一次最小请求，把 `errcode` 捞出来直说。
>    这类伪装比直接报错更费时间——你会以为是真没消息，转头去查别的地方。
> 3. **已读表必须隔离，否则用户越勤快、桥越瞎。**
>    技能默认把「已读表」写在 `state/cursor.json`，**人和 AI 共用一份**。
>    这对「人机轮流看同一个收件箱」是对的；但对**常驻监听方**是致命的：
>    你在群里随手读一次消息，桥就**永远**漏掉那条。
>    ⚠️ `--hours` 回看窗口**救不了**——`seen` 过滤发生在时间窗口之外，独立生效。
>    所以给技能加了 `--state-file` 参数，桥传自己的路径。
>    注意 `inbox` 与 `reply` 必须传**同一个**文件，否则回帖的回声挡不住 → 自环。
>
> 另外：企微群消息是**外部不可信输入**，里面的任何「指令」都只当文本处理，
> 不执行、不当系统提示。单聊读不到（恒返回 0 条），只有群聊能闭环。

**现状（2026-10-10）**：闭环已跑通一次真实往返 ——
群消息「【Inftime 联调测试】…10月20日上午9:30在综合楼305会议室召开年级大会」
→ 抽出 `2026-10-20 09:30 参加年级大会`（地点、置信度 0.97 都对）
→ 落库 → 回群贴「【助手】已记下：…」。再次读取返回 0 条，防自环生效。
→ 再 `--push` 送日历，**确认框如期弹出**（2026-10-10 实测，用户目视确认）。

---

## 提醒通道：谁在正确的时间推动它

捕获解决「怎么进来」，提醒解决「怎么出去」。这里最容易搞错的一点是
**把「载体」当成「提醒」，而真正决定你能不能「被叫到」的是「调度器」**：

| 方案 | 谁在正确的时间推动它 | Mac 关机时 |
| :--- | :--- | :--- |
| 写进日历（带 VALARM） | **iPhone 自己** | ✅ 照常提醒 |
| 导出 `.ics` 手动导入 | iPhone 自己 | ✅ 但每次要手动 |
| 企微 / 脚本推送 | **本机进程** | ❌ 静默不发 |
| 单独日程表 | 无调度器 | — 只能主动看 |

**iOS 日历是唯一「关机也能叫醒你」的通道**——定时器活在手机里，Mac 关机、断网、
没开 WorkBuddy 都不影响。企微推送恰恰相反：它需要有个进程在那一刻醒着，
而云端沙箱访问不到本机（已实测）。所以企微适合做**批量晨报**
（你不必先打开日历就能收到），不适合做**到点提醒**。两者是分层，不是二选一。

**把事件送进日历的几条路**：

| 路径 | 自动化 | 隐私 | 代价 |
| :--- | :--- | :--- | :--- |
| **`inftime auto-calendar`**（快捷指令中转） | ✅ **全自动、零弹窗** | 全本地 | 建一次快捷指令（3 分钟） |
| **`inftime push`**（`open -a Calendar`） | 半自动 —— **需点一次「添加」确认** | 全本地 | 点一次确认 |
| 写 `.ics` 到 iCloud Drive 中转 | 半自动 | 全本地 | 手机上点一下导入 |
| AppleScript 直写 `Calendar.app` | 想要全自动，**但本机做不到** | — | 见下方 |
| 订阅 `webcal://` | 自动 | ⚠️ ICS 必须托管在公网 | 不推荐——等于把日程公开 |

```bash
python3 -m inftime.cli push              # 导出并交给系统日历（会弹导入确认框）
python3 -m inftime.cli push --to-icloud  # 顺带复制到 iCloud Drive，供 iPhone 导入
python3 -m inftime.cli push --dry-run    # 只生成 .ics，不打开日历
python3 -m inftime.cli push --alarm 1440 # 提前一天提醒（默认 15 分钟）
python3 -m inftime.cli auto-calendar     # 全自动：走快捷指令直写日历，零弹窗
```

### 全自动日历：快捷指令中转（3 分钟配好）

`push` 那条路每次都要点一次「添加」，无人值守的定时轮询会让确认框堆积。
快捷指令有**正规的 app 身份**，日历权限只需授一次，之后就是全自动：

**① 装快捷指令（一次）**

**方法 A（推荐）：程序化生成，不用碰编辑器**

```bash
# 在代码根目录执行；未签名文件 → 签名 → 弹出导入窗 → 点一次「添加快捷指令」
python3 -m inftime.make_shortcut -o /tmp/Inftime记日程.unsigned.shortcut
shortcuts sign --mode anyone -i /tmp/Inftime记日程.unsigned.shortcut -o /tmp/Inftime记日程.shortcut
open /tmp/Inftime记日程.shortcut
```

生成的是一个 **Run Shell Script** 动作（输入方式写死 stdin），内部调
`osascript` 写日历 —— **完全绕开「运行 AppleScript」动作里那个
「传入输入内容」设置**（它是历史上卡住最多人的地方：英文叫
as arguments / to stdin，简体中文版译作「作为参数」/「给 stdin」）。

**方法 B（备选）：手动建**

1. 打开「快捷指令」app → 右上角 **+** 新建
2. 搜索动作 **「运行 AppleScript」**，添加
3. 点动作标题行里的蓝字「**传入输入内容**」，选 **「作为参数」**
   （= 英文 as arguments / argv。**不要选「给 stdin」**，选错会报
   `item 2 of {}`）
4. 把下面的脚本整段粘贴进去（覆盖原有内容）
5. 命名为 **`Inftime记日程`**（CLI 默认按这个名字找）

```applescript
on run {input, parameters}
	set _delims to AppleScript's text item delimiters
	set AppleScript's text item delimiters to "|"
	set p to text items of (input as text)
	set AppleScript's text item delimiters to _delims

	-- ⚠️ 组件必须按 年→月→日 的顺序设置，否则会发生日期溢出
	-- （例如 1月31日，先把月份改成 2 月会得到 3 月 3 日）
	set d to current date
	set time of d to 0
	set year of d to (item 2 of p) as integer
	set month of d to (item 3 of p) as integer
	set day of d to (item 4 of p) as integer

	set allDay to (item 7 of p) as integer
	set evLoc to item 9 of p
	set calName to item 10 of p

	if allDay is 1 then
		set e to d + 1 * days
	else
		set hours of d to (item 5 of p) as integer
		set minutes of d to (item 6 of p) as integer
		set seconds of d to 0
		set e to d + ((item 8 of p) as integer) * minutes
	end if

	tell application "Calendar"
		set matched to (calendars whose name is calName)
		if (count of matched) is 0 then
			error "没有找到名为「" & calName & "」的日历。打开「日历」app 看侧边栏里的确切名字，然后在命令里用 --calendar 传入那个名字。"
		end if
		tell (item 1 of matched)
			set newEv to make new event with properties {summary:(item 1 of p), start date:d, end date:e, location:evLoc}
			if allDay is 1 then
				set allday event of newEv to true
			end if
			if (item 8 of p) as integer is greater than 0 and allDay is 0 then
				make new sound alarm at end of sound alarms of newEv with properties {trigger interval:-((item 8 of p) as integer)}
			end if
		end tell
	end tell
	return "ok"
end run
```

**② 用**

```bash
python3 -m inftime.cli auto-calendar                  # 把没写进日历的事件全写进去
python3 -m inftime.cli auto-calendar --newer-than 70  # 只处理最近 70 分钟的（给轮询用）
python3 -m inftime.cli auto-calendar --calendar 个人   # 指定日历（默认「个人」）
python3 -m inftime.cli auto-calendar --force-old      # 连同已过期的事件一起推（补录）
```

**过期宽限（默认 24 小时）**：开始时间早于 24 小时前的事件默认**不推** ——
日程都结束了，写进日历只是污染。关机补跑最多隔夜，宽限足够；确认还要推更早的，
加 `--force-old`（跳过会打印说明了跳的是谁，不会静默）。

第一次运行 macOS 会弹一次「"快捷指令"想要访问你的日历」→ 允许，之后全自动。

**③ 两条路别混用**

`auto-calendar`（快捷指令）是**裸写**日历，**没有 UID 去重**；
`push`（ICS 导入）靠 UID 去重。同一个事件两条路都走，日历里会出现两条。
所以**选定一条就用下去**。CLI 用 `pushed_at` 字段记住哪些已经通过快捷指令写过，
默认只推没写过的 —— 这是这条路唯一的防重复机制。

**④ 常见卡点（第一次做最容易停在这几处）**

| 现象 | 原因 | 怎么办 |
| :--- | :--- | :--- |
| 搜索「AppleScript」搜不到动作 | 搜的是「快捷指令」的 App 搜索框 | 输入 `运行 Apple` 试试；确认在**操作库**而不是右上角的设置里搜 |
| 粘贴后脚本显示成一行 / 报语法错误 | 粘贴时只替换了中间一段 | 在脚本框里先 **Cmd+A 全选**，再 Cmd+V 覆盖 |
| 报「没有找到名为「个人」的日历」 | 日历名不是「个人」 | 打开「日历」app 看侧边栏的确切名字，命令里加 `--calendar 那个名字` |
| 第一次运行弹「"快捷指令"想要访问你的日历」 | 正常，只需一次 | 点**允许**，之后不再弹 |
| `shortcuts run` 报「找不到快捷指令」 | 名字对不上（多了空格等） | `shortcuts list` 看确切名字；CLI 可用 `--shortcut 名字` 指定 |
| `echo … \| shortcuts run` 报「没有收到输入」 | **`shortcuts run` 不支持管道 stdin**（静默丢弃） | CLI 已改用 `-i 文件`（`run_shortcut` 自动处理）；手动验证用 `shortcuts run 名字 -i 文件` |
| 脚本里出现红色报错划线 | macOS 会在你还没输入前就标红 | 忽略编辑器的提示，直接保存运行验证 |

**测试状态（诚实说明）**：脚本里的日期构造逻辑已用 `osascript` 单独验证
（含「年→月→日顺序」的溢出陷阱）；`tell application "Calendar"` 那半句
只能等你把快捷指令建好、第一次真跑时验证。CLI 这端已做好防呆：
快捷指令不存在会**提前失败并指路**，不会假装成功。

> **为什么不用 AppleScript 直写**（2026-10-10 实测，别再试了）
>
> 从这条进程链（WorkBuddy → python → `/usr/bin/osascript`）访问 Calendar 会返回
> **`-10004` 权限违例，而且连授权弹窗都不弹**。逐项排除后确认根因是
> **进程没有可识别的 app 身份（bundle id）**，系统不知道该向谁征求同意，
> 于是直接拒绝。佐证：
> - `tell application "Finder" to get name` → **成功**（AppleScript 通道本身没问题，
>   是 Calendar 这一项单独拿不到权限）；
> - Calendar 未启动时报 `-600 应用程序没有运行`，`open -a Calendar` 拉起主进程后
>   变回 `-10004`；
> - 旁路也断：`Accounts4.sqlite` → `authorization denied`，日历数据受 TCC 保护。
>
> 所以退一步走 LaunchServices：让 Calendar.app **自己**打开 `.ics`。
> 绕开 AppleScript 权限，零配置、零授权。
>
> ✅ **实测（2026-10-10，截图验证）**：Calendar 会弹出
> **「要添加这些日程吗」确认框**，点「添加」后事件才进入日历。
> **零配置、零授权，代价是点一次确认**。导入后事件自带 VALARM
> （日历详情里显示「开始之前 15 分钟提醒」），提醒由 iPhone/macOS 自己调度，
> **不依赖任何常驻进程**。
>
> ⚠️ **确认框不一定跳到最前**：实测有过一次确认框压根没显示出来，
> 用户以为「没反应」——其实日历正在等确认。所以 `push` 会
> **先 `open -a Calendar` 把日历激活到前台，隔一下再送文件**，
> 降低确认框被别的窗口挡住的概率（测试
> `test_real_run_activates_calendar_before_handing_over_file` 锁住这个顺序）。
> 万一还是没看到：切到「日历」app 找一下，它多半在等你点。

> ⚠️ **UID 必须稳定**：`ics.py` 的 UID 由 `fingerprint` 派生，**绝不能含随机数**。
> 否则同一条日程重复导入日历会被当成两个事件——
> 「改几条 → 重新导出全部 → 再导入」就会让日程翻倍。见测试
> `test_uid_is_stable_across_regeneration`。

---

## 承诺审计（先做这个）

这是最重要的一步，也是唯一能防止自嗨的一步。
**先别写产品，先拿真实消息量一遍：过去 30 天你答应过多少件事，多少件后来忘了？**

```bash
python3 -m inftime.cli audit samples/wechat_samples.txt --days 30 --out 审计报告.md
```

输入格式很随意，一行一条，可选带来源：

```
[项目群] 李工: 本周五下午两点前把材料发我
教务处通知：10月20日上午9:30在综合楼305会议室开会
```

报告会告诉你三件事：
1. **多少件完全没有时间** —— 这类最危险，它们会消失
2. **多少件时间很模糊** —— 需要的是排期决策，不是日历
3. **如果事件总数本来就很少** —— 说明这个痛点对你不够强，**这是好事，省下了三周**

样本数据在 `samples/wechat_samples.txt`，47 条模拟的真实群聊，可以直接跑。

---

## 对照实验（决定要不要为模型付钱）

配了大模型之后，**先用数字确认它值不值**，别凭感觉：

```bash
python3 -m inftime.cli compare samples/wechat_samples.txt --out 对照实验.html
```

输出暗色报告页：样本数、一致 / 不同条数、差异率、差异构成（条数不同 / 标题不同 /
时间不同）以及每条差异的左右对照。

跑得慢——每条一次模型调用，推理模型单条 2~15 秒，47 条约 5~10 分钟。

### 这个数字怎么算才是可信的

两条测量纪律，都是踩过坑才定下来的：

| 规则 | 为什么 |
| :--- | :--- |
| **置信度不参与差异判定** | `confidence` 是模型的自我报告，和规则层永远不相等。早期版本把它拼进对比字符串，结果 47 条里几乎每条都「有差异」，数字彻底失去意义。 |
| **模型调用失败单独计一类** | 模型不可用时 `extract()` 会静默降级回规则结果，两边输出一模一样。若不单独统计，就会把「调用失败」记成「结论一致」——**失败越多，越像「模型没用」**。成功率低于 80% 时报告会明确警告「结论不可采信」。 |

差异只按三个语义维度判定：抽了几条 / 标题是什么 / 时间是什么。

---

## 已知边界

诚实地说清楚哪些场景现在处理不好：

- **过去时态的事件**：Inftime 是前瞻性工具，「10月2号的聚餐」在 10 月 10 日说出来会被算到明年。
  审计历史记录时这一点会失真。
- **无时间的承诺**：「晚点回你」「我看看」会被标成「待澄清」而不是丢弃——这是有意的，
  但会产生噪音。配了大模型后噪音会少很多。
- **跨段落的指代**：「记得把这件事做好」里的「这件事」指什么，规则引擎猜不到。
- **微信自动读取**：做不到，也不该做。微信从未开放群消息接口，所以走的是
  「你转发给它」的路径。这是刻意选择。

---

## 测试

```bash
python3 -m unittest discover -s tests -v   # 83 个回归用例
python3 tests/mcp_smoke.py                 # MCP 协议冒烟测试（14 项）
```

**规矩：凡是修过的 bug，必须在测试里留一条断言。**
**而且新加的检查必须做变异测试**——把防线拆掉，确认断言真的失败。
没验证过的检查等于没有检查（踩过：验证脚本自己写错，产出了「看起来验证过了」的假结果）。

已经固化下来的坑（都在 `tests/test_inftime.py` 里）：
- 时区：本地时间被当成 UTC 写进 ICS → 所有事件静默偏移 8 小时
- span 切除：按倒序逐个删除导致下标错位，「东方艺术中心」被吃成「方艺术中心」
- 星期计算：以「今天」为锚点算周几，把「下周三」算多了一整周
- 地点抽取：泛化字符类把「请在六点前凭提货码至同济大学菜鸟驿站」整段当地点
- 单字时段词：「下周一**晚**七点」被当成上午 07:00
- 模型名：猜的 `deepseek-v4-flash` 根本不存在。DeepSeek 是先查余额后校验模型名，
  余额为 0 时错误码是 402 而不是「模型不存在」→ 错名字会长久潜伏。
  现在写入配置时会拿服务端 `/models` 清单核对。
- 推理模型探活：`max_tokens` 给 32 时，预算全被 reasoning token 吃掉，
  返回 `content: ""` 却 HTTP 200 → 看起来像「key 有问题」。已提到 512 并加空内容检测。
- 对照实验：假设模型每次都成功，会让「调用失败」伪装成「结论一致」，系统性地
  低估模型价值。现在失败单独计数，成功率 <80% 明确标注结论不可采信。
- 剪贴板凭据泄露（安全）：原过滤器只有「纯链接/纯数字/纯英文串」，
  密码、数据库连接串、API key、银行卡号全部会通过并被发到大模型服务器。
  现已加独立闸门，命中即丢弃且不上云。
- 企微业务错误被伪装成「没有新消息」：权限过期（850003）时收件箱脚本返回
  空列表 + 退出码 0，桥跟着报「没有新消息」。已加 `preflight()`
  单独发一次最小请求捞 `errcode`。
- 去重键不能含模型生成的字段：`fingerprint` 原先把 `title` 算进 basis，
  而 `title` 由大模型产出、天然会飘——同一条群通知跑 4 次得到
  「参加年级大会」「按时参加年级大会」「准时出席年级大会」「全员参加年级大会」
  四个版本 → 每次都是新指纹 → **去重完全失效，日程库堆重复项**。
  现在只用 `source_channel + raw_text + start` 这三样与模型无关的值。
- 哨兵前缀写两遍：`wecom_reply.py --group` 会自动补 `【助手】`，
  桥的 `_summary_text()` 又手写了一个 → 群里出现「【助手】【助手】已记下」。
  哨兵的职责只有一处（回复器），桥只管内容。
- 已读表不能人机共用：技能的 `seen` 是**人和 AI 共用**的一张表，
  对「轮流看同一个收件箱」是对的，对**常驻监听方**是致命的——
  人随手读一次消息，桥就永久漏掉那条（`--hours` 救不了，seen 过滤在
  时间窗口之外独立生效）。已给技能加 `--state-file`，桥用自己的状态文件；
  且 `inbox` 与 `reply` 必须指向同一个文件，否则回声挡不住会自环。
- ICS 的 UID 不能含随机数：原实现拼了 `uuid4()`，每次导出都不同。日历按 UID
  判事件身份 → 「改几条 → 重新导出全部 → 再导入」会让**日程翻倍**。
  已改为由 `fingerprint` 派生（与上一条同源：**进身份键的字段必须确定性**）。
- 写日历别用 AppleScript：从这条进程链访问 `Calendar` 会返回 `-10004`
  且**连授权弹窗都不弹**（根因：进程没有 app 身份）。改用
  `open -a Calendar <ics>` 让日历自己弹确认框。
- 别把「现象」当「设计」：我一度把 `open -a Calendar` 记成「静默导入、全自动」，
  唯一证据是「用户说没看到弹框」+ 前台 app 变成了「日历」——**这是过度推断**。
  用户随后纠正：是弹确认框的，只是那次没跳出来。教训见下方「协作纪律」。
- `--push` 只推新增、不推全库：若推整个日程库，每同步一次确认框里就重新冒出
  全部历史事件。新增语义由 `render(on_create=...)` 给出，
  断言 `test_on_create_fires_only_for_new_events` +
  `test_push_without_new_events_does_not_open_calendar` 守着。
- **「什么都没发生」是最难排查的输出**：桥原先把「没抽出事件」的消息直接跳过，
  屏幕只剩「读到 N 条消息」，于是「读到了但没识别出」和「压根没读到」
  长得一模一样 —— 用户只能来问「是不是没反应」。
  现在**每条消息都打印**，无事件的标注「（未识别出需要行动的事）」，
  末尾给「小结：N 条里 M 条有事件」（测试 `TestBridgeOutputTellsTheTruth`）。

---

## 协作纪律

写代码的纪律在上一节。这一条是**和人协作**的纪律，同样在 2026-10-10 付了代价：

> **现象 ≠ 设计。用户的第一手观察 > 我的间接推断。**

具体那次：用户说「没弹出确认框」，我用 `lsappinfo front` 看到前台 app 变成了「日历」，
就**把这一次观测直接拔高成「macOS 对单个事件的 .ics 设计上就是静默导入」**，
还主动去改了 6 处文档说明。用户随后纠正：「不，还是要点确认的，是刚才没跳出，
但确认后就成功了。」

两个教训：
1. **只有「我记得没看到」这类证据时，先问，别替系统下定义。**
   真正的设计事实需要证据，不能从一次现象外推。
2. ⚠️ **更危险的是我改的方向**——我是去**删掉用户原本正确的预期**。
   当我的推断和用户的第一手观察冲突时，**默认先信用户，再查证**。

---

## 目录

```
inftime/
├── inftime/
│   ├── schema.py          Event 数据模型（5W1H 结构化 + 置信度 + 待澄清）
│   ├── timeparse.py       中文时间解析（规则，不依赖模型）
│   ├── extract.py         抽取层：规则 + LLM 合并
│   ├── llm.py             OpenAI 兼容适配器（stdlib urllib，可插拔）
│   ├── ics.py             ICS 生成（RFC 5545，手写，零依赖）
│   ├── deliver.py         把事件送进系统日历（共用；cli 与企微桥都走这里）
│   ├── calendar_shortcut.py 通过快捷指令直写日历（全自动、零弹窗）
│   ├── store.py           本地 SQLite 存储
│   ├── audit.py           承诺审计
│   ├── compare.py         规则 vs 大模型的对照实验（含暗色 HTML 报告）
│   ├── wecom_bridge.py    企业微信群 → 日程的桥（含业务错误预检、--push 一条龙）
│   ├── cli.py             命令行
│   ├── mcp_server.py      MCP stdio 服务（手写协议，零依赖）
│   └── watch_clipboard.py 剪贴板监听
├── samples/               真实风格的中文消息样本
└── tests/                 回归测试 + MCP 冒烟测试
```

---

## 为什么不是一个 App

2018 年那份 BP 里最大的一个错误，是**「再造一个通知渠道，让活动方来我们这里发通知」**。
那等于在微信之外再建一个微信——冷启动死锁。

2026 年的做法反过来：**不抢入口，被调用。**
你不必说服任何人下载东西；用户只是在自己的 agent 配置里加一行。
分发从「拉新」变成了「安装」。

---

*让每一分钟都拥有永恒的意义。* — 2018 年 BP 结尾原句，留给这个版本。

---

## 参与

发现它把某句话理解错了？卡在某个平台的安装？先看 [CONTRIBUTING.md](CONTRIBUTING.md)
—— 里面写了欢迎什么、硬约束是什么、以及什么样的 PR 会被拒。

最有用的一种反馈：**一句它没处理好的原话**，加上你期望的结果。

