"""工具调用"话痨"短语池 + 选词器 — 零重依赖 (只用 random)。

从 livekit_io.py 抽出来, 因为 livekit_io 顶部硬 import `from livekit import rtc`,
没装 livekit SDK 的 bot (如 jarvis) 一 import 就 ModuleNotFoundError, 导致
feishu on_tool_use 的工具提示被 except 静默吞掉 (只剩开场白能播)。本模块不碰
livekit, 任何 bot 都能 import, 让 Discord sidecar / LiveKit 两路都能播工具提示。
"""

import random

# 每次 Claude 调一个工具时, 给用户念一句简短安抚, 让 voice 用户知道"还在跑
# 不是死了"。模板用 Gemini 官方情感标签起手以保证 TTS 表现力。
# 同 tool 连续触发 >2 次时去重 (第3次起 skip), 避免读 5 个文件念 5 句。
_TOOL_VOICE_HINTS = {
    "Bash": [
        "[neutral] 命令跑起来啦",
        "[contemplative] 嗯 shell 转着呢",
        "[informative] 在执行命令",
        "[playful] 我去 shell 里溜达一圈",
        "[amused] 又得敲键盘了",
        "[whispers] 偷偷跑个命令",
        "[focus] 让终端飞一会儿",
        "[contemplative] 给 bash 一个发挥的机会",
    ],
    "Read": [
        "[neutral] 翻一下文件",
        "[focus] 看一眼文件",
        "[informative] 读着呢",
        "[playful] 让我啃一下这个文件",
        "[amused] 一目十行扫一眼",
        "[curiosity] 这文件里写了啥",
        "[contemplative] 沉浸式阅读中",
        "[whispers] 偷偷瞄一眼源码",
    ],
    "Write": [
        "[neutral] 写文件中",
        "[informative] 落盘呢",
        "[focus] 笔尖落下中",
        "[playful] 让我码点字",
        "[amused] 字节排队进硬盘",
        "[contemplative] 字斟句酌往里写",
    ],
    "Edit": [
        "[neutral] 改一下文件",
        "[informative] 修着呢",
        "[focus] 拿起手术刀",
        "[playful] 给它整整容",
        "[contemplative] 斟酌一下改哪句",
        "[amused] 微调一下措辞",
    ],
    "Grep": [
        "[focus] 搜下代码",
        "[informative] 找一下",
        "[playful] 翻箱倒柜找一找",
        "[curiosity] 这玩意儿藏哪了",
        "[focus] 大海捞针中",
        "[contemplative] 让 ripgrep 跑两步",
    ],
    "Glob": [
        "[focus] 翻翻路径",
        "[informative] 找文件",
        "[playful] 我去文件树探险",
        "[curiosity] 看看仓库里有啥",
        "[contemplative] 沿着路径摸过去",
        "[amused] 摸黑找文件中",
    ],
    "Agent": [
        "[informative] 派个小弟去办",
        "[playful] 找个帮手去搞",
        "[amused] 这种活让小弟干",
        "[informative] 召唤一个并行 worker",
        "[whispers] 我背后还有人",
        "[playful] 喊个分身去办",
        "[contemplative] 派外援去查",
    ],
    "WebSearch": [
        "[curiosity] 上网搜搜",
        "[informative] 搜索中啊",
        "[playful] 出门遛一圈",
        "[amused] 让我去打听打听",
        "[contemplative] 翻翻互联网的角落",
        "[focus] 上网兜一圈",
    ],
    "WebFetch": [
        "[curiosity] 抓个网页",
        "[informative] 上网读一下",
        "[playful] 摘点网上的果子",
        "[focus] 把页面扒下来",
        "[amused] 隔着网络瞄一眼",
    ],
    "TodoWrite": [
        "[neutral] 记一下任务",
        "[informative] 列个清单",
        "[playful] 这就记小本本",
        "[contemplative] 让我先排个顺序",
        "[whispers] 别忘了别忘了",
        "[focus] 把要点钉墙上",
        "[amused] 怕你忘所以我记着",
    ],
}

# MCP tool name 是 "mcp__plugin_xxx__yyy" 格式, 用前缀模糊匹配
_TOOL_PREFIX_HINTS = [
    ("mcp__plugin_playwright", [
        "[curiosity] 开浏览器看看",
        "[informative] 浏览器跑起来啦",
        "[playful] 让浏览器跑两步",
        "[focus] 隔着浏览器瞄一眼",
        "[amused] 我去网页里点点点",
    ]),
    ("mcp__jina-ai__read_webpage", [
        "[curiosity] 抓个网页",
        "[informative] 上网读一下",
        "[playful] 摘点网上的果子",
        "[focus] 把页面扒下来读",
        "[contemplative] 让我去读读这个链接",
    ]),
    ("mcp__jina-ai__search_web", [
        "[curiosity] 上网搜搜",
        "[informative] 网上翻一翻",
        "[playful] 我去趟搜索引擎",
        "[focus] 让我查一下",
        "[contemplative] 容我搜搜",
        "[curiosity] 上网瞄一眼",
    ]),
    ("mcp__jina-ai__fact_check", [
        "[focus] 验证一下事实",
        "[contemplative] 这话我得核实下",
        "[seriously] 让我求证一下",
        "[curiosity] 真的假的, 查一查",
        "[whispers] 这事我得求证",
    ]),
    ("mcp__plugin_github", [
        "[informative] 看下 GitHub",
        "[curiosity] 翻翻 repo",
        "[playful] 我去 GitHub 转一转",
        "[focus] 拉下代码瞄一眼",
    ]),
    ("mcp__plugin_context7", [
        "[focus] 查个文档",
        "[informative] 翻一下官方手册",
        "[contemplative] 让我对一下文档",
        "[curiosity] 文档怎么说",
    ]),
    ("mcp__wiki__", [
        "[focus] 查下知识库",
        "[informative] 翻翻 wiki",
        "[contemplative] 让我去 wiki 里找找",
        "[curiosity] 这事 wiki 记了没",
        "[playful] 翻我自己的小本本",
    ]),
    ("mcp__chrome-devtools__", [
        "[curiosity] 开 Chrome 看看",
        "[focus] 让浏览器跑起来",
        "[playful] 我去网页里点点",
        "[informative] 隔着浏览器干活",
    ]),
    ("mcp__", [
        "[playful] 让我翻翻百宝箱",
        "[amused] 借个外挂使一下",
        "[curiosity] 让我去打听打听",
        "[whispers] 偷偷查一下",
        "[amused] 我开个挂",
        "[contemplative] 调用一下外援",
        "[playful] 借个外部工具凑活下",
    ]),
]

_TOOL_DEFAULT_HINTS = [
    "[neutral] 嗯让我处理一下",
    "[contemplative] 稍等",
    "[playful] 给我点空气",
    "[amused] 别催别催",
    "[focus] 这就来",
    "[whispers] 让我先动动手",
]


def pick_tool_voice_phrase(tool_name: str) -> str:
    """根据 tool name 选一句"话痨"短语 (随机变体)。

    匹配优先级: 精确 > prefix > default。返回带 Gemini 情感标签的短句,
    适合直接喂 TTS。
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
