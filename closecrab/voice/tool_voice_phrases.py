"""工具调用"话痨"短语池 + 选词器 — 零重依赖 (只用 random)。

从 livekit_io.py 抽出来, 因为 livekit_io 顶部硬 import `from livekit import rtc`,
没装 livekit SDK 的 bot (如 jarvis) 一 import 就 ModuleNotFoundError, 导致
feishu on_tool_use 的工具提示被 except 静默吞掉 (只剩开场白能播)。本模块不碰
livekit, 任何 bot 都能 import, 让 Discord sidecar / LiveKit 两路都能播工具提示。

## 写短语的规矩（2026-09-14 Chris 定）

**一听就得知道我在干什么。** 用户看不见屏幕，这句话是他唯一的进度信息源，
所以每一句都必须带一个**能指认动作的关键词** —— 「搜内容」「找文件名」
「读网页」「改文件」。同一个工具的几个变体只换说法, **不换那个关键词**。

反例（上一版的毛病, 已全部删掉）：

- 「我去 shell 里溜达一圈」「翻箱倒柜找一找」「摸黑找文件中」
  —— 听完不知道是在搜内容还是找文件名, 等于没说。
- 「偷偷瞄一眼」「给我点空气」「给它整整容」「我开个挂」「派个小弟」
  —— 离实际动作太远, Chris 明确说不要这类。

所以情感标签也收敛到 neutral / informative / focus / contemplative /
curiosity / seriously 这几个正经的, 不用 whispers / amused / playful。

## 加新条目之前先数一下它到底触发多少次

上一版腐烂得最厉害的三处, 都是**改了别处忘了改这里**, 而且失配不报错、
只是默默掉进兜底池, 于是「听不出在干啥」的锅看起来像是文案问题：

| 工具 | jarvis 30 天触发 | 上一版的下场 |
|---|---|---|
| `mcp__jina-ai__read_url` | 463 | 条目写成 `read_webpage`（真名是 `read_url`）→ 掉兜底 |
| `mcp__jina-ai__parallel_*` | 466 | `startswith("...search_web")` 匹配不到 `parallel_search_web` → 掉兜底 |
| `TaskCreate/Update/Output` | 654 | 根本没条目 → 掉全局兜底 |

⇒ **前缀表里 `parallel_` 那几条必须单列**, 因为它是前缀匹配不是包含匹配,
`"mcp__jina-ai__parallel_search_web".startswith("mcp__jina-ai__search_web")`
是 False。同理别指望 `read_url` 能被 `read_webpage` 匹配上。

已删的死条目: `mcp__plugin_playwright` / `mcp__plugin_github` /
`mcp__plugin_context7`（plugin 早关了）、`mcp__chrome-devtools__`
（08-10 从配置里摘掉）、`mcp__jina-ai__fact_check`（jina 没这个工具）。
本机现存 MCP 只有 jina-ai / wiki / serena 三个。
"""

import random

# 每次 Claude 调一个工具时, 给用户念一句简短安抚, 让 voice 用户知道"还在跑
# 不是死了"。模板用 Gemini 官方情感标签起手以保证 TTS 表现力。
# 同 tool 连续触发 >2 次时去重 (第3次起 skip), 避免读 5 个文件念 5 句。
_TOOL_VOICE_HINTS = {
    # —— 日常四件套, 占全部触发量的九成 ——
    "Bash": [
        "[neutral] 跑条命令",
        "[informative] 在执行命令",
        "[focus] 命令跑起来了，等结果",
        "[contemplative] 在终端里跑一下试试",
    ],
    "Read": [
        "[neutral] 读一下文件",
        "[informative] 打开文件看内容",
        "[focus] 我读一遍这个文件",
        "[curiosity] 看看这文件里写了什么",
    ],
    "Edit": [
        "[neutral] 改文件",
        "[informative] 在原文件上改几行",
        "[focus] 动手改这段代码",
        "[contemplative] 斟酌一下这句怎么改",
    ],
    "Write": [
        "[neutral] 写一个新文件",
        "[informative] 把内容落成文件",
        "[focus] 新建文件写进去",
    ],
    # —— 两种「找」必须听得出区别：搜内容 vs 找文件名 ——
    "Grep": [
        "[focus] 按关键字搜内容",
        "[informative] 在代码里搜这个词",
        "[curiosity] 全仓搜一下这串字",
    ],
    "Glob": [
        "[focus] 按文件名找",
        "[informative] 列出匹配这个路径的文件",
        "[curiosity] 看看有哪些文件对得上",
    ],
    # —— 派活 ——
    "Agent": [
        "[informative] 派个子任务并行去查",
        "[focus] 开一路子 agent 同时办",
        "[contemplative] 这块分出去让人并行查",
    ],
    # —— 任务清单：以前完全没条目, 654 次全掉兜底 ——
    "TodoWrite": [
        "[neutral] 列个任务清单",
        "[informative] 把步骤排一下顺序",
        "[focus] 先把要做的几步记下来",
    ],
    "TaskCreate": [
        "[neutral] 建一个任务项",
        "[informative] 把这步记进任务清单",
    ],
    "TaskUpdate": [
        "[neutral] 更新任务状态",
        "[informative] 把这一步标成做完了",
    ],
    "TaskList": [
        "[neutral] 看一眼任务清单",
        "[informative] 对一下还剩哪几步",
    ],
    "TaskOutput": [
        "[neutral] 看后台任务的输出",
        "[informative] 查一下那个后台任务跑到哪了",
    ],
    "TaskGet": [
        "[neutral] 查这个任务的状态",
    ],
    "TaskStop": [
        "[neutral] 停掉那个后台任务",
    ],
    # —— 工具与技能 ——
    "ToolSearch": [
        "[focus] 找一个能用的工具",
        "[informative] 查查有没有现成的工具能干这事",
    ],
    "Skill": [
        "[informative] 加载一个现成的技能",
        "[focus] 调一套现成的流程来做",
    ],
    "read_image": [
        "[curiosity] 看一下这张图",
        "[focus] 读这张图里的内容",
    ],
    "read_multimodal": [
        "[focus] 读这份文档的原貌，不只是文字",
        "[curiosity] 连排版带图一起看一遍",
    ],
    "NotebookEdit": [
        "[neutral] 改 notebook 里的这个 cell",
    ],
    "AskUserQuestion": [
        "[seriously] 这里得问你一句再往下走",
    ],
    "ExitPlanMode": [
        "[seriously] 方案想好了，给你过目",
        "[informative] 先把方案摆出来等你点头",
    ],
    # —— 定时任务 ——
    "CronCreate": [
        "[neutral] 设一个定时任务",
        "[informative] 到点让它自己提醒",
    ],
    "CronList": [
        "[neutral] 看一眼定了哪些定时任务",
    ],
    "CronDelete": [
        "[neutral] 把那个定时任务撤掉",
    ],
    "ScheduleWakeup": [
        "[neutral] 排一个到点叫醒自己的闹钟",
    ],
    "SendMessage": [
        "[informative] 给那边的 agent 递句话",
    ],
    # —— 内置联网（跟 jina 那条分开, 说法要一致好认 ——
    "WebSearch": [
        "[curiosity] 上网搜一下",
        "[informative] 联网搜资料",
    ],
    "WebFetch": [
        "[curiosity] 打开这个网页读",
        "[informative] 把网页正文抓下来看",
    ],
}

# MCP tool name 是 "mcp__<server>__<tool>" 格式, 用前缀模糊匹配。
# **顺序即优先级, 越具体的越靠前** —— 命中第一条就停。
# 注意这是 startswith 不是包含匹配: `parallel_xxx` 不会被 `xxx` 匹配到,
# 所以那几条必须单列 (上一版就是在这儿漏了 466 次)。
_TOOL_PREFIX_HINTS = [
    ("mcp__jina-ai__parallel_search", [
        "[focus] 同时搜好几个关键词",
        "[informative] 并行上网搜一批",
    ]),
    ("mcp__jina-ai__parallel_read", [
        "[focus] 同时读好几个网页",
        "[informative] 一批网页一起抓下来读",
    ]),
    ("mcp__jina-ai__search", [
        "[curiosity] 上网搜一下",
        "[informative] 联网搜资料",
        "[focus] 上网查一下这个说法",
    ]),
    ("mcp__jina-ai__read_url", [
        "[curiosity] 打开这个网页读",
        "[informative] 把网页正文抓下来看",
        "[focus] 我去读一下这个链接",
    ]),
    ("mcp__jina-ai__extract_pdf", [
        "[focus] 把这份 PDF 的正文提出来",
    ]),
    ("mcp__jina-ai__", [
        "[informative] 用联网工具查一下",
    ]),
    ("mcp__wiki__", [
        "[focus] 查我自己的知识库",
        "[informative] 翻 wiki 看记过没有",
        "[curiosity] 这事我 wiki 里应该有",
    ]),
    ("mcp__serena__", [
        "[focus] 查这个符号在哪定义的",
        "[informative] 用 LSP 找一下谁调用了它",
        "[contemplative] 顺着代码结构找过去",
    ]),
    ("mcp__", [
        "[informative] 调一个外部工具",
        "[focus] 用外部服务查一下",
    ]),
]

# 兜底。**也要说清是在干活**, 不能只是「稍等」—— 用户听不见屏幕,
# 一句没信息量的话跟静音是一样的。
_TOOL_DEFAULT_HINTS = [
    "[neutral] 这一步我来办，稍等",
    "[informative] 正在往下走",
    "[focus] 处理中，马上好",
]


def pick_tool_voice_phrase(tool_name: str) -> str:
    """根据 tool name 选一句"话痨"短语 (随机变体)。

    匹配优先级: 精确 > prefix (表内从上到下) > default。返回带 Gemini 情感
    标签的短句, 适合直接喂 TTS。
    """
    pool = _TOOL_VOICE_HINTS.get(tool_name)
    if not pool:
        for prefix, hints in _TOOL_PREFIX_HINTS:
            if tool_name.startswith(prefix):
                pool = hints
                break
    if not pool:
        pool = _TOOL_DEFAULT_HINTS
    return random.choice(pool)
