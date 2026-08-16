---
globs: closecrab/channels/*.py
---

# Channel 开发规则

## 三平台一致性
修改任何一个 channel 的逻辑时，必须检查另外两个是否需要同步：
- `discord.py` — py-cord, 消息限 2000 字符
- `feishu.py` — lark-oapi, 支持富文本卡片
- `dingtalk.py` — dingtalk-stream, 纯文本为主

## _format_interactive_prompt()
每个 channel 都有这个函数，处理 Claude 的控制请求：
- `ExitPlanMode` — **必须**从 `inp.get("plan", "")` 提取 plan 内容并展示给用户。绝不能只发"方案已就绪"而不展示内容
- `AskUserQuestion` — 展示 `inp.get("questions", [])` 列表
- 新增控制类型时，三个 channel 都要加

### AskUserQuestion.multiSelect 行为
工具协议层每个 question 支持 `multiSelect: bool`（缺省 false）。各 channel 的处理：

| Channel | multiSelect=False (单选) | multiSelect=True (多选) |
|---|---|---|
| feishu | 渲染按钮 (`tag: action`)，点一个 resolve future | **不渲染按钮**，把选项列成 `1. label — desc` 文本，hint 提示「请用文字回复，例如 `1,3,4`」，用户文字消息走 `_pending_input` 路径（`feishu.py` 里搜 `_pending_input`，别记行号 —— 它漂过一次了） |
| discord | 渲染按钮（未实现 multi） | 未实现，目前跟单选一样 |
| dingtalk | 纯文本列出 | 未实现 |

**为什么飞书多选走文字回复**：飞书卡片没有「checkbox + 独立提交按钮」的简洁原生组件，要做得在 `tag: form` 容器里嵌 `tag: checker`，复杂度高。而 `_pending_input` 早已承接文字消息，多选直接复用文字路径只需 ~15 行改动。文字消息比 form 提交更灵活（LLM 能理解 `234` / `1,3,4` / `A、C、D` / `第一个和第三个`）。

**测试**：`test_feishu_card.py` 覆盖 single/multi/mixed/empty/default 5+ 个场景。改动 `_build_ask_question_card` 前先跑一遍，改完再跑一遍。

## Channel 基类
继承 `closecrab/channels/base.py` 的 `Channel` ABC，必须实现：
- `start()` — 连接平台，开始监听
- `stop()` — 优雅断开
- `send_message(target, text)` — 发送到指定频道/群
- `send_to_user(user_key, text)` — 私信用户

## 消息转换
所有平台消息必须转为 `UnifiedMessage`（定义在 `closecrab/core/types.py`）再交给 BotCore：
```python
UnifiedMessage(channel_type="discord", user_id=str(uid), content=text, reply=callback, metadata={})
```

## 语音消息
语音在 Channel 层完成 STT 转换（调用 `closecrab/utils/stt.py` 的 `STTEngine`），转成文字后再构造 `UnifiedMessage`。BotCore 不处理音频。

## Discord 特有
- `_LogBuffer` 类：5 秒批量合并日志输出，避免 rate limit
- slash commands 注册在 `DiscordChannel.start()` 里
- 进度 emoji 映射（📖 reading, ✏️ writing, ⚡ running 等）
- 紧急停止关键词（"停", "stop", "取消"）在消息处理层拦截

## 飞书特有
- WebSocket 长连接（lark_ws），不是 webhook
- 卡片消息用 interactive message card JSON
- 动画进度 header（螃蟹动画 + AI 段子）
- 日志频道（log_chat_id）单独输出

## Web channel（`web.py`）—— 跟另外三个不是一类

前三个 channel 是**平台适配器**：SDK 建长连接、平台推消息进来。
`web` 没有平台 —— 它自己 `aiohttp` 起一个 HTTP server，交互是**请求–响应**：
前端 POST 一条消息，`handle_message` 跑完才返回。给 tpuguru 这类
「只有网页、没有 IM」的对外 bot 用。

改它之前记住三条差异：

1. **没有主动推送。** `send_message()` 只能把文本塞进 `_history` 等前端轮询
   （`GET /api/progress` / `/api/history`）。别指望像飞书那样随时 push 卡片。
2. **交互式工具一律 auto-continue。** 网页上没有按钮，也没有「挂着等用户点」
   的长连接。`_make_input_callback` 把 plan / 问题作为普通消息落进 history，
   然后按默认项返回（ExitPlanMode → `approved`，AskUserQuestion → 第一个选项）。
   **不这么做控制请求会一直挂到 BotCore 的 user lock 超时** —— 跟 dingtalk 那个
   `is_inbox` fast-path 是同一个病因。
3. **出站文本必须走 `_emit()`**，它调 `sanitize_outbound()`。理由见下。

### 为什么清洗放在 channel 层而不是只写进 prompt

这个 channel 面向**外部用户**，跟另外三个的受众根本不同：

- `★ Insight` 那种装饰块是 `explanatory-output-style` plugin 的 session-start
  hook 注入的，**跟 system prompt 是两个来源**。实测在 style.md 里明令禁止后
  仍然三问漏一 —— prompt 压不住另一个 prompt。
- 内部地址泄露是事故级的，不能只靠模型自觉。system prompt 里同时还有
  「引用 Wiki」这类**方向相反**的指令（已按 channel 分叉，见 `main.py`
  的 Wiki 知识感知段），但配置一改就可能又冲突。

所以 `sanitize_outbound()` 做确定性删除：insight 块、内网域名、`go/` 链接、
工单号、`/home/...` 与 `~/...` 本机路径。**新增对外 channel 时复用这个函数。**

### 配置

Firestore `bots/{name}.channels.web = {host, port}`，`active_channel = "web"`。
`config-manage.py create <bot> --channel web --web-port <p>`。
风格指南在 `channels/web_static/style.md`，前端单文件 `web_static/index.html`。
