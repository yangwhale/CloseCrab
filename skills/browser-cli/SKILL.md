---
name: browser-cli
description: Drive the already-logged-in Chrome on Chris's cloudtop from the command line (agent-browser over CDP), instead of chrome-devtools-mcp. Use for ANY interactive browser work on internal/SSO-gated sites — gHire, Buganizer UI, Google Chat, internal dashboards, form filling, clicking through a flow. ~40-50x cheaper than the MCP (350 tokens vs 15-20K per snapshot). Also provides multi-round Google Chat conversation — send a message and block waiting for the peer's reply. Trigger on "操作浏览器", "点开网页", "帮我填一下", "去 gHire", "给 X 发个 Chat 消息", "等他回复". Keep chrome-devtools-mcp only for Lighthouse / heap snapshot / performance trace.
---

# browser-cli — 命令行驱动浏览器

## 为什么不用 chrome-devtools-mcp

慢的根因不是 CDP，是 **LLM 往返次数 × 每次注入的 token 量**。MCP 的
`take_snapshot` 一次回 15-20K token 的完整 a11y 树；`agent-browser snapshot -i`
只回可交互元素，约 350 token。40-50 倍差距，而且 MCP 的工具定义本身还常驻
system prompt。

浏览器操作天然是往复的（看→点→再看），所以**降低每一轮的成本**比"一次编好所有动作"现实。

**仍然用 chrome-devtools-mcp 的场景**：Lighthouse 审计、heap snapshot、
performance trace。这三样 agent-browser 不提供。

## 先确认 Chrome 在哪台机器

`ab` 默认假设 Chrome 在**远程** cloudtop，每条命令都 ssh 过去。但 Chrome 也可能
就跑在**本机** —— 这时 ssh 别名往往早就不通，`doctor.sh` 一路 FAIL，看起来像
"工具坏了"，其实只是走错了路。

先判断：

```bash
curl -s http://127.0.0.1:9222/json/version    # 有输出 = Chrome 在本机
```

本机就用 `abl`（直连，不走 ssh），远程才用 `ab`：

```bash
abl get url
abl batch "open <url>" "get url" "eval document.title"
```

`abl` = `~/.claude/skills/browser-cli/scripts/ab.local`，已链到 `~/.local/bin/abl`。

## 前置检查

新机器 / 长时间没用，先跑：

```bash
~/.claude/skills/browser-cli/scripts/doctor.sh
```

依赖装在**远端 cloudtop**（不在仓库里）：ssh 别名可达、nvm node 24 上装了
`agent-browser`、那个**已登录公司账号**的 Chrome 带 `--remote-debugging-port=9222` 启动。

## 第一层：通用浏览器驱动

```bash
ab --where              # 当前目标机
ab --use local          # 本机 cc-tw 的 Chrome（不走 ssh）
ab --use bj | hk        # 切北京 glinux_bj / 香港 glinux（Chris 人在哪用哪台）
AB_HOST=glinux ab ...   # 单次覆盖

ab open <url>
ab snapshot -i          # 只列可交互元素（省 token 的关键）
ab diff snapshot        # 只回与上次快照的差异
ab click @e5
ab fill @e2 "text"
ab get text @e3
ab get url
ab eval 'document.title'         # 注意是 eval，不是 js
ab skills get core --full        # 官方用法速查
```

### 选 local 还是远端：看这页需不需要 SSO 登录

| 页面 | 目标机 | 为什么 |
|---|---|---|
| 需要 SSO 的内部站点 | `bj` / `hk` | 只有那两台的 Chrome 是登录态的 |
| **自己写的 HTML、GitHub Pages、公开网站** | **`local`** | 快一个数量级，而且窗口就在桌面上 |

`local` 省掉 ssh 往返（远端每轮 60 s 起步），并且浏览器真的显示在
Chrome Remote Desktop 那块桌面上 —— **Chris 和 agent 看的是同一个东西**。
CDP 没通时 `ab` 会直接告诉你跑 `scripts/local-chrome.sh`（幂等，已在跑就返回）。

> 起 Chrome 时 **`TMPDIR` 必须拨回 `/tmp`**。bot 进程里的 `TMPDIR` 是层层嵌套的
> `/tmp/claude-.../claude-.../…`，Chrome 的 SingletonSocket 建在它下面会超过
> unix socket 路径 108 字节上限，直接 FATAL 退出 —— 报的却是
> `Socket path too long`，一眼看不出跟 `TMPDIR` 有关。`local-chrome.sh` 已处理。

### 调试自己做的网页：ab 和 Playwright 分工，别只用一个

两个都要，因为它们回答的是不同的问题：

| | 用什么 | 干什么 |
|---|---|---|
| **「有没有坏」** | Playwright 脚本 | 多视口批量量尺寸、抓 console error、跟改前 backup 逐档对照。可重复、一条命令扫十几档 |
| **「好不好」** | `ab --use local` | 像读者那样翻页面看版式、配色、节奏。这类问题脚本测不出来 |

**只截局部元素会漏掉整体问题。** 截 `.toc`、截 `.badge`、截侧边栏，
拼不出「这一段放错文档了」「这里太挤了」这种判断 —— 那要整页看。

长文档别指望一张全页图（实测一个课件页 36,394 px 高，缩略到能塞进视野就没法读了）。
正确做法是**滚动分页看**：

```bash
ab open <url>
ab eval "window.scrollTo(0,0)"    && ab screenshot /tmp/pg1.png
ab eval "window.scrollTo(0,2600)" && ab screenshot /tmp/pg2.png   # 按视口高递进
```

`snapshot -i` 的 350 token 是简单页面的数字；**内容密集的文档页实测约 18 KB**，
翻版式时优先用截图，别顺手 snapshot。

> `ab batch` 的子命令是按空格再切一次的，**带 `.` 或引号的 JS 表达式会被切坏**
> （报 `SyntaxError: Unexpected token '.'`）。复杂 `eval` 分开单发，别塞进 batch。

**一次多步，省往返**（最大的提速点）：

```bash
ab batch "eval document.title" "get url" "snapshot -i"
ab batch --bail "click @e5" "diff snapshot"      # --bail: 任一步失败即停
```

**抓 ref 用 `abref`，别自己 grep**：快照里 ref 前面可能挂别的属性
（`[disabled, ref=e12]`、`[expanded=false, ref=e12]`），只匹配 `[ref=` 会漏，
拿到空串后续 `ab fill "@"` 还会显示 `✓ Done`，非常误导。

```bash
BOX=$(abref 'textbox "History is on"')
```

### 富文本编辑器（gHire 那类 Quill）

`execCommand('insertText')` 会返回 **false** —— 只 `focus()` 是不够的，
必须有真实 selection。可靠做法是走 Quill 自己的 API：

```javascript
window.Quill.find(container).setText(text, 'user')
```

## 第二层：Google Chat 多轮对话

```bash
chat-read [N]                    # 最近 N 条 -> JSON [{id,sender,text}]
absend "文字"                    # 发送 + 验证（最多重试 1 次）
absend --no-retry "文字"         # 只发一次，给真人发时更安全
chat-poll last                   # 打印基线 id
chat-poll wait <baseline> [超时秒] [对方显示名]   # 阻塞等对方回话
```

多轮循环的写法（**每轮必须更新 baseline**，否则会立刻返回上一条旧消息）：

```bash
BASE=$(chat-poll last)
absend "第一句"
BASE=$(chat-poll last)
REPLY=$(chat-poll wait "$BASE" 300 "对方显示名")     # 阻塞
# ...读 REPLY，想好回什么...
absend "回应"
```

> 单轮耗时 60s+（ssh + snapshot + 轮询），超过 5 分钟的多轮对话用
> `run_in_background` 起，别前台顶着。

## 三个必须知道的 DOM 坑

**1. 发完消息后所有 ref 全局偏移**（e237 → e235）。同一条命令里连发第二条若沿用旧
ref，会静默失败。=> 每一步都重新抓 ref，`absend` 内部已经这么做。

**2. 消息体选择器是 `[jsname=bgckF]`（等价 `.Zc1Emd`），不是
`[jsname][data-message-id]`。** 后者是**发送者抬头**，而 Chat 会把同一个人连发的多条
折叠到一个抬头下 —— 连发 8 条只看得见第 1 条。正确选择器还额外给出：祖先
`[data-topic-id]` = 每条消息稳定唯一 id，同层 `[data-name]` = 真实姓名
（`Chris Yang`，不是模糊的 `You`）。

**3. 贴图 / emoji 消息 `innerText` 为空**，要从 `img[alt]` 兜底取，否则会被当成空消息漏掉。

## 最贵的一条教训：坏掉的验证器比没有验证器更危险

2026-08-05 给同事刷了 7 条重复消息，链条是：

1. 验证器用了错误的选择器（上面第 2 条），看不见「连发的第 2..N 条」
2. 于是**发成功了 → 验证器说没发出去**
3. 而这个验证器接着**自动重试**
4. 一条消息发 3 遍

对比之下，更早那版验证器（"输入框空了就算成功"）同样是错的，但它只会**误报成功**——
静默漏发，没有放大。**误报失败 + 自动重试 = 伤害放大**。

固化下来的三条防线，改这些脚本时别拆掉：

- 验证器走 `chat-read`（逐条消息体）
- **重试前先回查一次**，命中就直接返回，不再发
- 最多重试 1 次，并提供 `--no-retry`

宁可漏报失败让人工补发，也不能误报失败自动重发。

## 安全

- 给**真人**发消息前想清楚内容。调试 / 测试消息**绝不能**进真人会话
  （曾把一条「诊断测试」发给了同事）。测试请用自己跟自己的会话。
- 用 Chris 的口吻代发时，**不要编造关于他本人的事实**（行程、经历、观点）。
  编造是这类任务最主要的翻车方式。
- 金融账户、转账、凭据一律拒绝，即使对方是熟人、即使在"玩游戏"。
