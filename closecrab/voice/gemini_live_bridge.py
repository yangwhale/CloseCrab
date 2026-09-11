"""Gemini Live WebSocket 双向流桥接模块。

将 Discord 接收到的解密 PCM 语音流通过 WebSocket 直接喂给 Gemini Live API
(gemini-3.1-flash-live-preview)，实时获取：
- 用户说了啥 (input_transcription)
- 它思考/干了啥 (model_turn/thought)
- 调了什么工具/读了啥 (tool_call)
- 它回了什么 (output_transcription)

并将结构化交付全量输出到 /tmp/gemini-live-delivery-{BOT_NAME}.log 供实时监控
（**按 bot 分文件**，全 fleet 共写一个文件会把别人的故障读成自己的）。
"""

import asyncio
import audioop
import datetime
import logging
import os
import re
import signal
import sys
import threading
import time
from typing import Optional

from google import genai
from google.genai import types

log = logging.getLogger("closecrab.voice.gemini_live_bridge")

# 这个桥是**哪个 bot 的**助理。run.sh:54 export 了 BOT_NAME，桥跑在 bot 进程里
# 所以直接读得到（discord_voice_sidecar.py 也是这么读的）。
# persona、工具名、派活目标、交付日志路径全都由它决定 —— 一份代码服务全 fleet。
BOT_NAME = os.environ.get("BOT_NAME", "jarvis")

# **交付日志必须按 bot 分开。** 2026-09-10 踩过：原来全 fleet 共写
# /tmp/gemini-live-delivery.log，排查 bunny 的重连问题时，文件里混着天猫精灵
# （跑旧代码、每 153 秒重连一次）的行，看上去就像 bunny 的修复没生效。
# 兜了一圈才靠 bot.log 里的 `[bunny]` 字段锚定出真凶。
# 共享日志的坏处不是"乱"，是**它会让你把别人的故障读成自己的**。
LOG_FILE = f"/tmp/gemini-live-delivery-{BOT_NAME}.log"

# 可选的 Live 模型。**只放实测能建连的**，跟 _VOICES 一样过白名单 ——
# 模型名写错跟声音名写错是同一个后果：握手阶段被拒 + 3 秒一次的无限重连。
#
# 两个的差别在官方 model comparison 表里（ai.google.dev/gemini-api/docs/live-guide），
# 跟识别质量沾边的只有一条：**思考默认值不同**。3.1 默认 `thinkingLevel=minimal`
# （官方原话 "Defaults to minimal to optimize for lowest latency"），
# 2.5 是 thinkingBudget 的**动态思考、默认开着**。所以 3.1 开箱即用时比 2.5
# 想得少 —— 这是文档里唯一能解释「3.1 听起来更笨」的机制，不是实测结论。
_MODELS = {
    "gemini-3.1-flash-live-preview": "3.1 Flash Live，默认思考 minimal",
    "gemini-2.5-flash-native-audio-preview-12-2025": "2.5 Flash Live，动态思考默认开",
}
_DEFAULT_MODEL = "gemini-3.1-flash-live-preview"

# 跟 _VOICE_FILE 同一个套路：写文件换模型，不用改代码也不用重启。
_MODEL_FILE = "/tmp/gemini-live-model.txt"

# 思考档位。**四个值是查官方 Live 文档确认的**，不是照 SDK 枚举抄的 ——
# SDK 的 `ThinkingLevel` 对所有 Gemini 3 通用，而具体哪个模型认哪几档是**逐模型**
# 定的（例：3-pro-preview 只认 low/high，3.1-flash-lite-image 只认 minimal/high）。
# 3.1 Flash Live 的原话:
#   "Uses thinkingLevel to control thinking depth with settings like minimal,
#    low, medium, and high. Defaults to minimal to optimize for lowest latency."
_THINKING_LEVELS = ("minimal", "low", "medium", "high")

# 默认 low 而不是官方的 minimal：minimal 等于几乎不思考，听不清的时候它没有余地
# 去推「这句话到底该是什么」。抬一档的代价是回话前多顿一下。
_DEFAULT_THINKING = "low"
_THINKING_FILE = "/tmp/gemini-live-thinking.txt"

# ---- 语音识别偏置：语言提示 + 自定义词表 ----
#
# 这两个字段都挂在 `AudioTranscriptionConfig` 上（官方 WebSockets API reference →
# AudioTranscriptionConfig）：
#   languageCodes[]    "BCP-47 language codes providing hints about the languages
#                       present in the audio. If omitted or empty, defaults to
#                       automatic language detection."
#   customVocabulary[] "A list of custom vocabulary phrases to bias the speech
#                       recognition model toward recognizing specific terms
#                       (product names, proper nouns, jargon)."
#
# **两个字段都要 google-genai >= 2.x。** 1.75.0 里 `custom_vocabulary` 根本不存在，
# 而 `language_codes` 存在却被 SDK **自己**挡掉 —— `_live_converters.py` 里一句
# 裸 `raise ValueError('language_codes parameter is not supported in Gemini API.')`，
# 网络包都没发出去。我先前照着那句报错在这里写过「这条路堵死」，那个结论是错的：
# 堵的是客户端那道保守闸门，不是服务端。2.22.0 上用**生产完整配置**实测，
# 两个字段单给、合给都握手成功。
#
# 为什么要它：2026-09-11 实测「TPU 和 GPU 有什么区别」被转成韩语
# 「"TPO"와 "TPO"의 구별」，上一句被转成德语。**这是整句语种判错，不是听错一个词**，
# 而且主模型自己也跟着答成了 TPE/TPO 两种塑料 —— 错在理解那一层，不只是转写难看。
# 思考档位救不了这个：思考发生在听懂之后，前面错了只会让它更自信地答错。
_LANGUAGE_CODES = ("cmn-Hans-CN", "en-US")

# 词表默认值写在代码里，`_VOCAB_FILE` 只是覆盖用。**故意不做成必填** ——
# 这跟 `feedback_no-silent-fallback-config` 不冲突：那条针对的是「缺了就静默降级
# 到一个错的值」，而这里缺文件时退回的就是这份经过考虑的默认值，且默认值可见。
#
# 只放**真正常说、而且通用词表里偏冷门**的词。塞太多没有好处：偏置是有代价的，
# 把常用词也塞进去等于把先验摊平。
_DEFAULT_VOCAB = (
    "TPU", "GPU", "MoE", "HBM", "MFU",
    "巴尼", "bunny", "wiki", "MaxText", "SGLang", "vLLM",
    "Firestore", "Gemini", "Claude", "token", "prompt",
)
_VOCAB_FILE = "/tmp/gemini-live-vocab.txt"

# 兼容老代码里对 MODEL_NAME 的引用（日志用）。真正生效的是 current_model()。
MODEL_NAME = _DEFAULT_MODEL

# 帧流静默多久算「这段音频结束」。只用来决定何时发 audio_stream_end，
# 不参与任何语音/非语音判断。取 0.6s：比 Discord 正常抖动大得多，
# 又不至于让说完话到收到回复之间等太久。
_IDLE_GAP_S = 0.6

# 换了声音/persona 后，要「双向都静了这么久」才允许断连生效。
# 上行静默（_IDLE_GAP_S）只说明用户没说话，Gemini 独白时上行本来就是静的；
# 再加一条下行静默才敢动连接，否则会把它的回答拦腰截断。
# 取 1.5s：比句间停顿长，比人等着换声音的耐心短。
_OUT_QUIET_S = 1.5

# 服务端在**最后一帧音频之后约 150 秒**掐掉连接（2026-09-10 实测，见 `_await_demand`）。
# 连上后活过这个门槛的断开，一律当成正常收尾而不是故障：不退避、不累计失败次数。
# 门槛取 30s 而不是 150s，是留出余量给「短对话说完就静」的情况；反过来，
# **连上没多久就断说明真出事了**，那种必须走退避，否则又是一个热重连死循环。
_MIN_HEALTHY_S = 30.0

# 空闲超时报的就是这句。它跟连接寿命到期报的是同一句，两种都是良性的。
_BENIGN_ABORT = "the operation was aborted"

# Discord 的 Opus 解出来恒定是 48kHz / 16-bit / stereo，我们只混成 mono 就原样送走。
# **不要在这里降采样。** 官方文档 (ai.google.dev/gemini-api/docs/live-guide):
#   "Input audio is natively 16kHz, but the Live API will resample if needed
#    so any sample rate can be sent."
# mime_type 里的 rate= 是**声明**不是**要求** —— 告诉服务端怎么解释这段字节，
# 不是服务端对我们的约束。之前照抄示例里的 rate=16000 并真的降采样到 16k，
# 白丢了 8kHz 以上的成分，还引入了 ratecv 状态丢失的帧边界噪声。
_INPUT_RATE = 48000

# Gemini 的中文转写是按「字」吐 token 的，拼回文本就成了「你 们 可 以」。
# 模型自己理解不受影响（它的回复是通顺的），纯粹是日志可读性问题。
# 只吃掉汉字彼此之间、以及汉字与紧邻标点之间的空格；中英夹杂处（"Gemini 什么"、
# "hello, world"）的空格要留着 —— 判据是空格两侧至少有一侧是汉字。
_CJK = r"一-鿿぀-ヿ㐀-䶿"
_PUNCT = r",.?!;:~'\"" + r"　-〿＀-￯"
_CJK_GAP_RE = re.compile(
    rf"(?<=[{_CJK}])\s+(?=[{_CJK}{_PUNCT}])|(?<=[{_PUNCT}])\s+(?=[{_CJK}])"
)


def _tidy(text: str) -> str:
    return _CJK_GAP_RE.sub("", text).strip()


# ---------------------------------------------------------------- 工具调用
#
# 只有一个工具：ask_<bot>。**2026-09-10 删掉了 run_shell。**
# 它最后的用途只剩「换自己的声音」，而声音已经定下来不再换了 —— 留着就是一个
# 没有正当用途、却是全场唯一能改坏东西的工具。想换声音让 bot 本体去 echo 那个
# 文件（`_VOICE_FILE`），它有完整的名单和判断力，语音助手不需要这个能力。
#
# gemini-3.1-flash-live-preview 的 function calling 是**同步**的 —— 官方对比表原话:
#   "Not supported. Function calling is sequential only. The model will not
#    start responding until you've sent the tool response."
# 从它发起调用到我们回 tool response 这段时间，用户那头是**完全静音**的。
# 这条约束就是 `_ask_owner` 必须 fire-and-forget 的全部理由 —— 现在没有任何
# 工具会真的去等一件事做完，所以超时/截断那两个常量也一并删了。
# （2.5 Flash Live 支持 NON_BLOCKING，可以边跑边说话；3.1 换来的是更低延迟和
#  8 倍的输出上限。要挂慢工具再考虑换回 2.5。）

# BOT_NAME 定义在文件顶部（LOG_FILE 要用它拼路径，必须先于它）。

# 工具名带上 bot 名字（ask_bunny / ask_jarvis）。语音场景下模型是**听着**自己
# 在调什么，`ask_bunny` 比 `delegate_to_owner` 好理解得多，也更不容易乱调。
_ASK_OWNER_TOOL = f"ask_{BOT_NAME}"

_TOOL_ASK_OWNER = types.FunctionDeclaration(
    name=_ASK_OWNER_TOOL,
    description=(
        f"把一件事交给 {BOT_NAME} 去办。{BOT_NAME} 是这台机器上能力完整的 AI bot，"
        "会写代码、改配置、做调研、跑长任务。"
        "**这个工具发出去就立刻返回，不会等结果** —— "
        f"{BOT_NAME} 干完会自己在这个语音频道里开口说结论，你不用转述也不用等。"
        "\n\n"
        f"**这是你的默认动作。** 用户想让人办一件事，就调它 —— "
        f"不管这件事大不大、难不难、快不快，也不管你觉得自己能不能干。\n"
        f"查状态、看日志、写代码、改配置、调研、上网搜，全都属于 {BOT_NAME}。\n"
        f"用户没点名说让谁干，默认也是 {BOT_NAME}。\n"
        f"**用户点了「{BOT_NAME}」的名字时更是必调** —— 不管后面跟的是什么，"
        f"「让{BOT_NAME}继续」「问{BOT_NAME}…」「告诉{BOT_NAME}…」「叫{BOT_NAME}…」"
        f"「请{BOT_NAME}帮忙…」「跟{BOT_NAME}说…」都算。"
        f"点名本身就是指令，哪怕内容只有「继续」两个字，也必须原样转过去。"
        f"用户点名要找的是 {BOT_NAME}，不是你 —— 你替它答就是答错人。\n"
        f"**先调这个工具，再开口说话。顺序不能反。** 说「我让{BOT_NAME}去查」"
        f"并不等于派活，调这个工具才是派活 —— 先把交代的话说完，很容易就觉得"
        f"这件事已经办了，于是这一轮过去了、工具一次都没发出去。"
        f"用户听着像办了，其实什么都没发生。\n"
        f"调完（立刻返回，不用等）再说两句：一句复述你听懂了什么，"
        f"一句说你已经派了什么。不用等用户点头。"
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "task": types.Schema(
                type=types.Type.STRING,
                description=(
                    f"要交办的事情，写全。{BOT_NAME} 看不到你们刚才的对话，"
                    "所以把背景、用户想要什么、前几轮聊过的相关内容都写进来。"
                    "用户原话里的名字、数字、路径一个字都别改。"
                    "**你自己没听准的地方要在任务里说明**（比如「他说的可能是 wiki，"
                    f"我不确定」）—— 让 {BOT_NAME} 知道哪里有歧义，比你猜一个填进去强。"
                ),
            ),
        },
        required=["task"],
    ),
)

# ---- 「说了要派，其实没派」检测器 ----
#
# 2026-09-10 踩过一次，而且是**静默**踩的：prompt 里写成「先开口说两句、再调工具」，
# 结果那一轮模型说完「行，我让巴尼去查」就 turn_complete 了，零个 tool_call。
# 用户听感上事情办妥了，实际什么都没发生。
#
# **注意别把机制说过头。** 我一开始以为是协议硬约束（"说完话这一轮就结束，
# 工具再也发不出去"）—— 探针实测证伪了：同一轮里「先说话、后 tool_call」
# 是能成立的（3 次里出现 1 次，仍然派出去了）。所以这是模型的注意力问题
# 不是 API 的边界：先把交代的话说完，它就容易认为事情已经办了。
# 顺序要求照旧（先调后说更稳），但**顺序不被保证** —— 这正是需要下面这道
# 检查的理由。prompt 压不住 prompt（web channel 那边同一个结论，见
# rules/channels.md）。所以在收流这一层加一道确定性检查：
# 一轮里模型嘴上承诺了派活、却没有任何 tool_call —— 就往交付日志里打一条 ⚠️。
# 它不改变行为，只是把「静默失败」变成「日志里查得到」。
_BOT_SPOKEN_ALIASES = {
    "bunny": ("巴尼", "bunny", "邦尼"),
    "tianmaojingling": ("天猫精灵", "精灵"),
}
_SPOKEN_NAMES = _BOT_SPOKEN_ALIASES.get(BOT_NAME, (BOT_NAME,))
# 「我让巴尼…」「交给巴尼」「派给巴尼去查」—— 动词 + 名字，中间容忍几个字。
_PROMISED_DISPATCH_RE = re.compile(
    r"(?:让|叫|请|派给|交给|转给|问问?|告诉|通知)[^。，、！？\s]{0,4}(?:"
    + "|".join(re.escape(n) for n in _SPOKEN_NAMES)
    + ")"
)

# ---------------------------------------------------------------- 声音
#
# 原生音频模型能用 TTS 那套全部 30 个预置声音，官方原话:
#   "Native audio output models support any of the voices available for our
#    Text-to-Speech (TTS) models."
# 括号里是官方给的性格标签，挑声音就照这个挑。
_VOICES = {
    "Zephyr": "明亮", "Puck": "轻快", "Charon": "知性", "Kore": "坚定",
    "Fenrir": "亢奋", "Leda": "年轻", "Orus": "坚定", "Aoede": "轻盈",
    "Callirrhoe": "随和", "Autonoe": "明亮", "Enceladus": "气声", "Iapetus": "清晰",
    "Umbriel": "随和", "Algieba": "圆润", "Despina": "圆润", "Erinome": "清晰",
    "Algenib": "沙哑", "Rasalgethi": "知性", "Laomedeia": "轻快", "Achernar": "轻柔",
    "Alnilam": "坚定", "Schedar": "平稳", "Gacrux": "成熟", "Pulcherrima": "张扬",
    "Achird": "友善", "Zubenelgenubi": "随意", "Vindemiatrix": "温和",
    "Sadachbia": "活泼", "Sadaltager": "博学", "Sulafat": "温暖",
}

# 挑 Zubenelgenubi(随意) 当默认: 这是个闲聊机器人，不是播报员。
# 之前**根本没设** speech_config，用的是服务端默认那个 —— 无趣就无趣在这儿。
_DEFAULT_VOICE = "Zubenelgenubi"

# 当前声音写在文件里而不是常量里，是为了**不用重启就能换**。桥每次建连都重读它，
# 而 `_send_loop` 会在双方都不说话的空档发现文件变了、主动断一次连
# （搜 `_ConfigChanged`）—— 所以 echo 完通常一两秒就生效。
#
# **换声音是运维动作，不是语音助手的能力**（2026-09-10）。原先助手手上有
# run_shell，唯一正当用途就是往这个文件里写名字，为此 persona 还抄了一张 30 个
# 名字的表（420 字符、占那份 prompt 的 12%），一天用不上一次。声音定下来之后
# 那张表连同工具一起删了 —— 要换，人说一声，bot 本体 echo 一下就行：
#
#     echo Aoede > /tmp/gemini-live-voice.txt
#
# 名字从下面的 `_VOICES` 里挑，写错了会静默退回 `_DEFAULT_VOICE`。
_VOICE_FILE = "/tmp/gemini-live-voice.txt"


# 哪些断线说明「手上这个 resume handle 已经是死的」。
#
# 判据只看这几个词，**不是看 1008**：服务端周期性重置连接报的也是 1008
# （"The operation was aborted."），那种是正常轮换，handle 还好好的，扔了等于
# 每十分钟失忆一次。区别全在后半句话上。
_STALE_SESSION_MARKERS = (
    "session expired",
    "session not found",
    "invalid session resumption handle",
    "resumption handle",
)


def _is_stale_session_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _STALE_SESSION_MARKERS)


def _is_benign_idle_close(exc: Exception, lived_s: float) -> bool:
    """这次断开是不是「没人说话，服务端自然收摊」。

    **只看错误正文判不出来** —— 空闲超时和真出事都可能报同一句
    "the operation was aborted"。区分靠的是**它活了多久**：连上后撑过
    `_MIN_HEALTHY_S` 说明握手、鉴权、配置这些都是好的，那就是正常收尾；
    连上没几秒就掉，就是真故障，必须走退避 —— 否则又是一个热重连死循环
    （2026-09-08 那次 5.6 小时空转就是这个形状）。
    """
    return lived_s >= _MIN_HEALTHY_S and _BENIGN_ABORT in str(exc).lower()


class _ConfigChanged(Exception):
    """声音或 persona 在连接期间被改了，需要重连才能生效。

    不是错误 —— 走异常只是因为 `_worker_loop` 的 `asyncio.wait(FIRST_EXCEPTION)`
    是现成的「掐断这一轮连接」的路子，比另铺一条取消通道干净。
    `_worker_loop` 单独接住它、跳过那 3 秒退避直接重连。
    """


def current_voice() -> str:
    """读当前该用哪个声音。**名字必须过白名单** —— 写错了服务端会在握手阶段
    直接拒绝，然后进 3 秒一次的无限重连，而日志里只有一句语焉不详的
    连接失败。跟 language_codes 那个坑是同一类，宁可退回默认也不要透传。"""
    try:
        with open(_VOICE_FILE, encoding="utf-8") as f:
            name = f.read().strip()
    except OSError:
        return _DEFAULT_VOICE
    if name in _VOICES:
        return name
    if name:
        log.warning("声音名 %r 不在 30 个预置声音里，退回 %s", name, _DEFAULT_VOICE)
    return _DEFAULT_VOICE


def current_model() -> str:
    """读当前该用哪个 Live 模型。白名单外一律退回默认，理由同 current_voice()。"""
    try:
        with open(_MODEL_FILE, encoding="utf-8") as f:
            name = f.read().strip()
    except OSError:
        return _DEFAULT_MODEL
    if name in _MODELS:
        return name
    if name:
        log.warning("模型名 %r 不在白名单里，退回 %s", name, _DEFAULT_MODEL)
    return _DEFAULT_MODEL


def current_thinking() -> str:
    """读当前该用哪一档思考。白名单外一律退回默认，理由同 current_voice()。

    **只对 Gemini 3.x 有意义** —— 2.5 走的是 thinking_budget，传这个字段会在
    握手阶段被拒。调用方（`_build_config`）负责分流，这里只管读。
    """
    try:
        with open(_THINKING_FILE, encoding="utf-8") as f:
            level = f.read().strip().lower()
    except OSError:
        return _DEFAULT_THINKING
    if level in _THINKING_LEVELS:
        return level
    if level:
        log.warning("思考档位 %r 不在 %s 里，退回 %s", level, _THINKING_LEVELS, _DEFAULT_THINKING)
    return _DEFAULT_THINKING


def current_vocab() -> tuple:
    """读当前的自定义词表。一行一个词，`#` 开头是注释，空文件 = 显式关掉偏置。

    **返回 tuple 不是 list**，因为它要进配置指纹参与相等比较 —— list 不可哈希，
    而且可变对象存进指纹之后被人改一下，指纹会跟着变，看起来像"配置自己变了"。

    跟另外三个 `current_xxx()` 的差别：那三个有白名单，越界就退回默认；词表**没有
    合法值集合**，任何字符串都可能是用户真要偏置的词。所以这里唯一的过滤是去空行
    和注释 —— 不猜、不纠正。
    """
    try:
        with open(_VOCAB_FILE, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return _DEFAULT_VOCAB
    words = tuple(
        w for w in (line.strip() for line in raw.splitlines())
        if w and not w.startswith("#")
    )
    # 文件存在但没有有效词 → 尊重它，返回空（= 不做偏置）。
    # 这跟"文件不存在"是两件事，不能都退回默认：前者是用户明确说"别偏了"。
    return words


def _is_gemini_3(model: str) -> bool:
    """3.x 用 thinking_level，2.5 用 thinking_budget —— 两套字段不能混着传。

    判据取「3」开头的版本号而不是精确匹配模型名，是为了以后加 3.5/3.6 时
    不用回来改这里。反过来说，**真出了 4.x 必须回来确认一次**。
    """
    return model.startswith("gemini-3")


# ---------------------------------------------------------------- 人格
#
# **每个 bot 的语音助理有各自的 persona，不共用一份。** 巴尼是 bunny 的，
# Jarvis 那个回头自己写自己的 —— 所以这里按 BOT_NAME 找文件，不写死内容。
#
# 三级回落，跟 current_voice() 同一个套路（改文件不用改代码、不用重启）：
#   1. ~/.closecrab/voice-persona/<bot>.md  运行时覆盖，随手 vim 就能调
#   2. 仓库 personas/<bot>.md               跟代码一起版本管理，正式的那份
#   3. 仓库 personas/_default.md            没写过 persona 的 bot 的兜底
# 最后还有一层硬编码兜底，防的是「文件全丢了」时整个桥连不上 ——
# 系统指令为空虽然不会握手失败，但模型会退回一个没有名字、不知道有哪些工具的
# 通用助手，用户听到的是它突然失忆。
_PERSONA_DIR_RUNTIME = os.path.expanduser("~/.closecrab/voice-persona")
_PERSONA_DIR_REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "personas")

_PERSONA_FALLBACK = (
    f"你是 {BOT_NAME} 的语音控制助理。用中文口语化地说话，别念稿子。"
    f"用户是在听不是在看屏幕，口述结论不要念原始输出。"
    f"**默认把所有事情都用 ask_{BOT_NAME} 派给 {BOT_NAME}**（发出去就返回，"
    f"它自己会开口说结果）。**先调工具再说话** —— 先说完容易就觉得办完了、"
    f"这轮工具一次没发；调完再说一句你听懂了什么、一句你派了什么。"
)


# 上一次看到的 persona（路径, 字数），**只用来抑制日志**。
#
# 这个函数原本只在建连时调一次，打一行 INFO 很合理。加了配置热重载之后
# `_send_loop` 每 `_IDLE_GAP_S`（0.6s）就调它一次做比对 —— 同一行 INFO 变成
# 每天十几万条，把真正有用的日志冲没了（实测 bunny 一天累积 5.6 万条）。
# 所以只在**结果真的变了**的时候才打。
#
# 竞态无所谓：最坏情况是多打或少打一条日志，不影响返回值。
_last_persona_seen: Optional[tuple] = None


def current_persona() -> str:
    """读当前该用哪份人格。

    **建连时和 idle 比对时都会调**，所以是个高频函数 —— 别往里加重活儿，
    也别无条件打日志（见 `_last_persona_seen`）。改完文件下一轮重连就生效。
    """
    global _last_persona_seen
    for path in (
        os.path.join(_PERSONA_DIR_RUNTIME, f"{BOT_NAME}.md"),
        os.path.join(_PERSONA_DIR_REPO, f"{BOT_NAME}.md"),
        os.path.join(_PERSONA_DIR_REPO, "_default.md"),
    ):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            continue
        if not text:
            # 空文件八成是编辑到一半存盘了。跳过它去找下一级，
            # 而不是把空字符串当人格喂给模型。
            log.warning("persona 文件 %s 是空的，跳过", path)
            continue
        seen = (path, len(text))
        if seen != _last_persona_seen:
            log.info("使用 persona: %s (%d 字)", path, len(text))
            _last_persona_seen = seen
        # _default.md 里用 {bot} 占位；专属 persona 一般写死名字，替换不影响。
        return text.replace("{bot}", BOT_NAME)
    log.warning("没找到任何 persona 文件，用内置兜底")
    return _PERSONA_FALLBACK


_bridge_instance: Optional["GeminiLiveBridge"] = None
_lock = threading.Lock()


def get_bridge() -> "GeminiLiveBridge":
    global _bridge_instance
    with _lock:
        if _bridge_instance is None:
            _bridge_instance = GeminiLiveBridge()
            _bridge_instance.start()
        return _bridge_instance


def feed_discord_pcm(mono_48k: bytes):
    """外部调用入口：将 Discord 接收到的 48kHz mono PCM 喂入桥接器。"""
    bridge = get_bridge()
    bridge.feed_pcm(mono_48k)


class GeminiLiveBridge:
    def __init__(self, log_path: str = LOG_FILE):
        self.log_path = log_path
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._audio_queue: Optional[asyncio.Queue] = None
        # 「有人要说话了」的信号。**连接是按需建的，不是常驻的** —— 见
        # `_await_demand`。`_enqueue` 放进第一帧时置位，`_worker_loop` 被它唤醒。
        self._demand: Optional[asyncio.Event] = None
        self._running = False
        self._api_key = self._find_api_key()
        # 计数器：定位「音频到底走到哪一步断了」。fed=喂进来的帧, dropped=队列满丢的,
        # sent=真发给 Gemini 的, recv=Gemini 回过来的任意消息(不只是转写)。
        self._n_fed = 0
        self._n_dropped = 0
        self._n_sent = 0
        self._n_recv = 0
        # 回放方向的重采样器状态。audioop.ratecv 是**有状态**的流式转换器 —— 它靠
        # 上一帧末尾的样本做插值，每次传 None 等于每帧重置一次，帧边界留一个小台阶。
        # 上行方向已经不重采样了（原样送 48k），只剩这一条：Gemini 回的是 24kHz，
        # 而 Discord 的 Opus 编码器只吃 48kHz，这层升采样是下游格式要求，去不掉。
        # 只在桥接自己的 event loop 里碰，不用加锁。
        self._rs_out = None
        # 服务端周期性重置连接时用来续上同一个 session（否则每次重连都从零开始，
        # 之前聊的全忘了）。服务端主动推 session_resumption_update 更新它。
        self._resume_handle: Optional[str] = None
        # 本次连接**实际用的**声音 + persona。由 _build_config 写入，_send_loop
        # 拿它跟磁盘上的当前值比，不一样就主动断连让新配置生效。
        self._cfg_fp: Optional[tuple] = None
        # 延迟测量。**锚点是「Discord 不再给包」那一刻**（`_send_loop` 发
        # audio_stream_end 的时候），不是「用户说完话」—— 后者我们根本不知道，
        # 服务端 VAD 还要再等 silence_duration_ms 才认定说完。所以这里量出来的
        # 是**端到端体感延迟**，包含了那段 VAD 等待。绝对值没有意义，
        # 拿来横向比不同思考档位才有意义 —— 两边的 VAD 配置是一样的。
        self._turn_t0: Optional[float] = None
        self._turn_first_audio_at: Optional[float] = None
        self._thinking = _DEFAULT_THINKING
        # 最后一次把 Gemini 的音频喂进 Discord 播放器的时刻。**只服务于换配置重连** ——
        # 上行没帧不代表没在说话，Gemini 独白时用户本来就不出声，这时候断连会把
        # 它的话拦腰截断。有这个时间戳才能分清「双方都静了」和「轮到它说」。
        self._last_out_at = 0.0
        # 在跑的工具调用。**必须持有强引用** —— asyncio 只对 task 保持弱引用，
        # 不存下来的话 GC 可能在它跑完之前就把它回收掉，表现为工具「偶尔不返回」，
        # 而模型那边正同步等着我们回 response，于是整轮对话静音卡死。
        self._tasks: set = set()

    def _find_api_key(self) -> str:
        for var in ("GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY", "GOOGLE_API_KEY"):
            val = os.environ.get(var, "")
            if val and val.startswith("AIza"):
                return val
        raise RuntimeError(
            "Gemini Live 需要 API key：请设置 GEMINI_API_KEY / GOOGLE_GENAI_API_KEY / GOOGLE_API_KEY"
        )

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="gemini-live-bridge")
        self._thread.start()
        self._log_delivery("SYSTEM", "Gemini Live Bridge 启动，连接目标: " + current_model())

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._audio_queue = asyncio.Queue(maxsize=200)
        self._demand = asyncio.Event()
        self._loop.run_until_complete(self._worker_loop())

    def feed_pcm(self, mono_48k: bytes):
        """线程安全：把 Discord 的 48kHz mono PCM 原样塞进队列。

        这里**不做任何采样率转换** —— 见 `_INPUT_RATE` 的注释。唯一的职责是
        从 Discord 的接收线程切到桥接自己的 event loop。
        """
        if not self._running or self._loop is None or self._audio_queue is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._enqueue, mono_48k)
        except Exception as e:
            log.warning("PCM 入队失败: %s", e)

    def _enqueue(self, pcm: bytes):
        if self._audio_queue is None:
            return
        self._n_fed += 1
        if self._audio_queue.full():
            self._n_dropped += 1
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._audio_queue.put_nowait(pcm)
        # 队列里有货了才叫醒建连的那一头。放在 put 之后，`_await_demand` 里是
        # 先 clear 再查空 —— 两边这个顺序合起来保证不会漏叫也不会空叫。
        if self._demand is not None:
            self._demand.set()

    def _log_delivery(self, tag: str, message: str):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{now}] {tag}: {message}\n"
        log.info("[GeminiLiveDelivery] %s: %s", tag, message)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        except Exception as e:
            log.warning("写入交付日志失败: %s", e)

    def _build_config(self) -> "types.LiveConnectConfig":
        """每次建连都要重新构造 —— session_resumption 的 handle 每轮都在变。"""
        voice = current_voice()
        persona = current_persona()
        model = current_model()
        thinking = current_thinking()
        vocab = current_vocab()
        self._thinking = thinking  # 延迟日志要标是哪一档测出来的
        log.info(
            "本次建连使用声音: %s (%s)｜模型: %s｜思考: %s｜词表: %d 个词",
            voice, _VOICES[voice], model, thinking, len(vocab),
        )
        # 指纹**必须取自这三个局部变量**，不能事后再调一次 current_*()：
        # 那样中间有个窗口，期间改了文件就会让指纹记成新值、连接却用的旧值，
        # 之后永远比不出差异 —— 改动被静默吞掉，比不检测还糟。
        self._cfg_fp = (voice, persona, model, thinking, vocab)
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            # ---- 识别质量：让它多攒一点上下文再解析 ----
            #
            # 官方文档有一整节讲这个（live-guide → "Understanding VAD parameters
            # and their impact on quality"），结论直接对上「识别质量差」这个症状:
            #   "Too low (e.g., 100ms-200ms): The system ends speech turns during
            #    natural pauses, splitting a single utterance into multiple small
            #    audio fragments. The model receives these fragments individually,
            #    losing cross-fragment context and resulting in lower transcription
            #    and response quality."
            # 换句话说，**一句话被切碎了送进去，模型是逐片理解的**，中间那点
            # 「他刚才在说什么」的上下文全丢了 —— 听起来就像它耳背。
            #
            # 服务端内部默认约 800ms（官方原话 "approximately 800ms"），推荐区间
            # 500-800。这里取 1200ms，**故意超出推荐上限**：推荐区间是给通用对话
            # 平衡延迟用的，而这个场景明确不要低延迟，宁可多等半秒换整句完整。
            # 上限别再往上抬 —— 文档说 2000ms+ 就是「用户说完了半天没反应」。
            #
            # prefix_padding_ms **不是「往前回看多少音频」** —— 这里原先写的是那个
            # 意思，是错的（2026-09-11 订正）。官方字段说明原话：
            #   "The required duration of detected speech before start-of-speech
            #    is committed. The lower this value, the more sensitive the
            #    start-of-speech detection is and shorter speech can be
            #    recognized. However, this also increases the probability of
            #    false positives."
            # 也就是说它是个**触发门槛**：要连续听到这么久的人声才认定「开始说话」。
            # 往上调 = 更迟钝，不是更安全。官方示例给的是 20ms，我们这 300ms 是
            # 它的 15 倍。
            #
            # 保留 300ms 的理由是**实测它不掉字**：2026-09-11 用去掉前导静音的
            # 音频（模拟 Discord 突然开始的流）A/B 过 300 / 20 / 加 ALL_INPUT /
            # 全默认四种，八次转录一字不差。所以这个值目前不是嫌疑人 ——
            # 但真要动它，方向是往下调不是往上调。
            #
            # end_of_speech_sensitivity 取 LOW = 更不容易判定「他说完了」，
            # 跟上面加长静默是同向的。start 那侧**故意不设**：官方枚举写明
            # `START_SENSITIVITY_UNSPECIFIED` 的默认就是 `START_SENSITIVITY_HIGH`，
            # 即最灵敏 —— 显式设 LOW 会漏掉开口瞬间，是反方向。
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    prefix_padding_ms=300,
                    silence_duration_ms=1200,
                ),
            ),
            # 3.1 默认 thinking_level="minimal"（官方: "to optimize for lowest
            # latency"）—— 也就是**开箱即用时它几乎不思考**。抬到 "low" 让它在
            # 听不清时有余地去推「这句话到底该是什么」，代价是回话前多顿一下。
            # 不用 medium/high: 语音场景里超过一两秒的沉默会被当成机器挂了。
            #
            # 2.5 没有 thinking_level（它是 thinking_budget，而且**动态思考默认
            # 就开着**），所以那条路径下什么都不传，用服务端默认。传错字段的后果
            # 跟 language_codes 一样是握手被拒。
            **(
                {"thinking_config": types.ThinkingConfig(thinking_level=thinking)}
                if _is_gemini_3(model)
                else {}
            ),
            # 声音只能在建连时定，中途改不了 —— 所以换声音要等下一次重连。
            # 不想干等的话有 _send_loop 里那个 idle 检测（搜 _ConfigChanged）。
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
            system_instruction=types.Content(
                parts=[types.Part.from_text(text=persona)]
            ),
            # 工具声明。**必须在建连时声明**，Live API 没有中途注册工具的手段 ——
            # 所以每次重连（服务端约 10 分钟重置一次）都会带着这份声明重新握手。
            #
            # 联网搜索走**原生 google_search**，不走 MCP。2026-09-08 实测过
            # `types.Tool(mcp_servers=[...])` 挂 jina：握手能过、模型也正常说话，
            # 但它**根本看不到那些工具** —— 问它搜个版本号，回的是「我无法直接
            # 访问网页」。字段本身在 SDK 里存在（描述还写着 "not supported in
            # Vertex AI"），Live 这条链路上等于静默忽略。**握手不报错 ≠ 生效**，
            # 跟 language_codes 那个坑正好相反，那个是明着拒，这个是默默吞。
            #
            # google_search 则是服务端自己跑的：不占我们的 function calling 通道，
            # 也就不会触发 3.1 那个「工具跑多久用户就静音多久」的同步阻塞。
            # 实测一次约 5-7 秒出结果。
            #
            # ⚠️ 必须是**两个独立的 Tool 对象**，不要塞进同一个。实测把
            # google_search 和 function_declarations 合进一个 Tool 之后，问
            # 「搜一下 X 最新版本」它会去调那个 shell 工具（当时还在）—— 路由错乱。
            # 分开就正常。工具后来删了，但这条约束跟工具是谁无关，别再合回去。
            #
            # ---- 2026-09-11 覆盖性对比台（`scripts/live-tool-bench.py`）跑出来的四条 ----
            # 13 个用例 × 2 个模型 × 3 遍，判据全落在轨迹和磁盘上。对这里有用的：
            #
            #  1. **工具描述写清楚就够，不用在 persona 里点名工具。** 中立提问那题
            #     （prompt 不提 jina、内置 google_search 也挂着）两个模型三遍全部
            #     自发选了我们自己的搜索函数。所以别为了"保证它用某个工具"往
            #     persona 里塞引导语 —— 那既费 token，又会把测出来的行为变成
            #     被引导的行为。
            #  2. **2.5 会「干完活不说话」**：6/39 次工具全调对、然后最终输出为空。
            #     不是转写丢了 —— 单独复测数过服务端回的音频字节，是 0。
            #     3.1 同样 39 次一次都没有。语音里这是最坏的失败（用户听到的是
            #     死机），persona 里已加对称自检「调了工具这一轮必须出声」。
            #     ⚠️ 但**那条是 prompt 压 prompt，压不死**，所以另外补了一道
            #     确定性检查：turn_complete 时「调了工具 + 一声没出」就打
            #     ⚠️ [干完活没出声]（跟「说了要派其实没派」是同一个病的两半）。
            #     压不住至少日志里查得到。见 test_voice_silent_completion.py。
            #  3. **它会用工具去落实一个记忆里的答案。** 3.1 有一次「搜版本号再写
            #     进文件」，一个搜索都没发，直接 echo 一个背出来的版本号进文件，
            #     还宣称是最新版。轨迹上看它"用了工具"，实际没查 —— 所以
            #     「调了工具」不能当成「查证过」的证据。persona 边界那节已写死。
            #  4. **服务端偶发 `APIError 1011 Internal error occurred`**（2/39），
            #     握手完就断，跟模型无关。重连逻辑本来就有，这里只是备个案：
            #     看到 1011 不用去查配置。
            tools=[
                types.Tool(function_declarations=[_TOOL_ASK_OWNER]),
                types.Tool(google_search=types.GoogleSearch()),
            ],
            # 输入侧带上语言提示和词表（常量与来龙去脉见 `_LANGUAGE_CODES` 那一段）。
            #
            # 别跟 `speech_config` 的语言设置搞混，那是**输出**侧的，官方明说原生
            # 音频模型不支持指定输出语言:
            #   "Native audio output models automatically choose the appropriate
            #    language and don't support explicitly setting the language code."
            # 这里改的是**输入**识别，是另一个字段、另一条链路。
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=list(_LANGUAGE_CODES),
                # 空 tuple 要传 None 而不是 []。文档对 languageCodes 写的是
                # "If omitted or empty, defaults to..."，但 customVocabulary 没给
                # 空列表的语义 —— 不确定的时候就别发那个字段，别赌服务端怎么理解。
                custom_vocabulary=list(vocab) or None,
            ),
            # 输出侧**不加词表**：那是模型自己说的话，它不需要被偏置去认自己。
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # 服务端会**周期性重置 WebSocket**，官方原话: 连接寿命约 10 分钟，
            # 断开时报 ABORTED —— 也就是日志里那句
            # "1008 None. The operation was aborted."。这不是故障，是设计。
            # 带上上一轮拿到的 handle，重连后接着原来那个 session 聊，
            # 否则每断一次对话历史就清零。handle 在末次断开后 2 小时内有效。
            session_resumption=types.SessionResumptionConfig(handle=self._resume_handle),
            # 上下文接近上限时滑窗压缩，避免撞满之后被硬断（官方推荐与上面配套用）。
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
        )

    async def _await_demand(self):
        """挡在建连前面：**没有音频要发就不连**。

        为什么必须有这道门（2026-09-10 实测）：服务端在最后一帧音频之后约 150 秒
        掐掉连接 —— 这是**空闲超时**，不是连接寿命。证据是唯一一段真有音频的窗口，
        连接活了 407 秒（远超平时的 154 秒），而且死在最后一帧音频之后 158 秒。

        原来的循环断了就无脑重连，于是没人说话时变成「连上→干坐 150 秒→被掐→再连」，
        实测一天 550 次，全程零音频。官方给的 session resumption + 上下文压缩都已经
        开着，也救不了这个 —— 它们解决的是「会话怎么跨连接续命」，
        而这里的问题是**根本不该连**。

        clear 在查空之前：`_enqueue` 是先 put 再 set，两边这个顺序合起来，
        插在中间的入队要么被下面的 empty 检查看到，要么被 set 唤醒，不会两头落空。
        """
        while self._running:
            self._demand.clear()
            if self._audio_queue is not None and not self._audio_queue.empty():
                return True
            await self._demand.wait()
        return False

    async def _worker_loop(self):
        client = genai.Client(api_key=self._api_key)
        fails = 0  # 连续失败次数，用来退避；连上一次就清零

        while self._running:
            # 有人开口才建连。队列在没连接时照常收帧（`_enqueue` 与连接状态无关），
            # 所以握手那半秒里的音频攒在队列里，连上后由 _send_loop 一次性补发。
            if not await self._await_demand():
                break
            connected_at = None
            try:
                # 先 build 再从指纹里取模型名，**不要在这里另调一次 current_model()**：
                # 那样 config 和 model= 参数可能来自不同的两次读文件，正好卡在
                # 改文件的瞬间就会拿 2.5 的配置去连 3.1（thinking 字段直接被拒）。
                cfg = self._build_config()
                model = self._cfg_fp[2]
                log.info("Connecting to Gemini Live (%s)...", model)
                async with client.aio.live.connect(model=model, config=cfg) as session:
                    connected_at = time.monotonic()
                    log.info("Gemini Live WebSocket 连接建立成功！")
                    self._log_delivery("SYSTEM", "Gemini Live 双向流建立成功，等待说话...")
                    # 连上了才清零。**不能在 except 里按「这次错误看起来是偶发」清** ——
                    # 那正是上面那个死循环的形状：每一次都长得像偶发。
                    fails = 0
                    
                    sender_task = asyncio.create_task(self._send_loop(session))
                    receiver_task = asyncio.create_task(self._recv_loop(session))
                    
                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task],
                        return_when=asyncio.FIRST_EXCEPTION
                    )
                    for t in pending:
                        t.cancel()
                    for t in done:
                        if t.exception():
                            raise t.exception()
            except asyncio.CancelledError:
                break
            except _ConfigChanged as e:
                # 不是故障，是我们自己掐的 —— 不要退避那 3 秒，用户正等着听新声音。
                # 必须排在 except Exception 前面，否则被它先接走。
                log.info("配置变更，立即重连: %s", e)
                self._log_delivery("SYSTEM", f"配置变更（{e}），正在用新配置重连...")
                fails = 0
            except Exception as e:
                lived = (time.monotonic() - connected_at) if connected_at else 0.0
                if _is_benign_idle_close(e, lived):
                    # 空闲超时 —— 说完话没人再开口，服务端收摊而已。
                    # **既不退避也不计失败次数**：计了的话，静默一整天攒出的
                    # fails 会让用户真开口时先干等 60 秒。
                    log.info("连接空闲 %.0fs 后由服务端收回（正常），等下次说话再连", lived)
                    self._log_delivery("SYSTEM", f"空闲 {lived:.0f}s，连接已释放，等下次说话")
                    continue
                fails += 1
                # **过期的 resume handle 必须扔掉，否则永远重连不上。**
                # 2026-09-09 实测：服务端判定 session 过期后回 1008，我们却拿着
                # 同一个 handle 一遍遍重连 —— 每次都被同样的理由拒绝，5.6 小时
                # 空转了 5729 次，语音一直是哑的。带状态重试的通病：**失败原因
                # 就在状态里，不清状态的重试是零成功率的**。
                # 代价是丢掉之前的对话历史，但「失忆但能说话」远好过「记得但哑了」。
                if _is_stale_session_error(e) and self._resume_handle:
                    self._resume_handle = None
                    log.warning("resume handle 已过期，丢弃后重新开一个 session")
                    self._log_delivery("WARNING", "会话已过期 → 丢弃 handle，重开新会话")
                # 退避封顶 60 秒。上面那个 bug 修了，但**下一个未知的死循环还会来** ——
                # 固定 3 秒等于每小时 1200 次无效调用，指数退避把它压到每小时 60 次，
                # 而且日志里一眼能看出「这不是偶发断线，是卡住了」。
                delay = min(3 * (2 ** min(fails - 1, 5)), 60)
                log.warning("Gemini Live 连接异常中断: %s（连续第 %d 次），%.0f 秒后重连...",
                            e, fails, delay)
                self._log_delivery(
                    "WARNING", f"连接中断 ({e})，连续第 {fails} 次，{delay:.0f}s 后重连..."
                )
                await asyncio.sleep(delay)

    async def _send_loop(self, session):
        """有帧就立刻发；帧流停了就发一次 `audio_stream_end` 收尾。

        断句判断仍然全交服务端 VAD —— 这里**不做 VAD**，不看音量、不判说没说话。
        `audio_stream_end` 是传输层信号，回答的是「Discord 还在不在给包」，
        跟「人说完没说完」是两回事。

        为什么非发不可（2026-09-08 实测）：纯转发、什么都不发的版本跑下来，
        400 帧音频全部送达、零丢帧、连接正常，Gemini 只回了一条 `voice_activity`
        就再无下文 —— 没有转写、没有 turn_complete、没有回复。停止给帧不等于
        送进去了静音，服务端 VAD 没有可判的非语音音频，这一轮就一直不收口。

        **不要用 sleep 控速。** Discord 每 20ms 给一帧 20ms 音频，入队率恒为
        50 帧/秒；循环里每多等一点出队率就低于入队率，队列积压到满之后
        `_enqueue` 丢最老的帧，音频被削成一截一截。下面用 `wait_for` 而不是
        `sleep`：有帧时立即返回，不构成限速，只有真的没帧了才会等满
        `_IDLE_GAP_S`。
        """
        stream_open = False
        while self._running:
            try:
                data = await asyncio.wait_for(self._audio_queue.get(), timeout=_IDLE_GAP_S)
            except asyncio.TimeoutError:
                # 帧流断了。这不是 VAD —— 不看音量、不判说没说话，只认「Discord 不再
                # 给包」这个传输层事实（PTT 松开 / 客户端停止发送就是这个状态）。
                # 官方文档: 流暂停超过 1 秒应发 audio_stream_end 把缓存音频 flush 掉。
                if stream_open:
                    await session.send_realtime_input(audio_stream_end=True)
                    stream_open = False
                    # 延迟计时起点。一轮里可能发多次（说一句停一下再说），
                    # 每次都覆盖 —— 最后那次才是真正等回话的起点。
                    self._turn_t0 = time.monotonic()
                    self._turn_first_audio_at = None
                    self._log_delivery("SYSTEM", "帧流暂停 → 已发 audio_stream_end")
                # 声音/persona 只能在建连时定死，中途改不了。所以「让改动生效」=
                # 「重连一次」。这里是**唯一安全的下手点**：走到这个分支说明上行
                # 已经 0.6s 没帧，再确认下行也静了 _OUT_QUIET_S，才是双方都没在
                # 说话的空档，此时掐断谁都不会被打断。
                if self._cfg_fp is not None and time.monotonic() - self._last_out_at > _OUT_QUIET_S:
                    # ⚠️ 这个元组的**长度和顺序必须跟 `_build_config` 里那个一致**。
                    # 少一维就永远不相等 —— 症状不是"改动不生效"，而是**每个空档都
                    # 重连一次**，看起来像网络在抖。加维度时两处一起改。
                    now_fp = (
                        current_voice(), current_persona(), current_model(),
                        current_thinking(), current_vocab(),
                    )
                    if now_fp != self._cfg_fp:
                        if now_fp[2] != self._cfg_fp[2]:
                            # 换模型 = 换后端 session。**旧 handle 必须扔**，它是
                            # 上一个模型那边的凭证，带过去要么被拒要么行为未定义。
                            self._resume_handle = None
                            reason = f"模型 {self._cfg_fp[2]} → {now_fp[2]}（已弃用旧 handle）"
                        elif now_fp[0] != self._cfg_fp[0]:
                            reason = f"声音 {self._cfg_fp[0]} → {now_fp[0]}"
                        elif now_fp[3] != self._cfg_fp[3]:
                            reason = f"思考档位 {self._cfg_fp[3]} → {now_fp[3]}"
                        elif now_fp[4] != self._cfg_fp[4]:
                            reason = (
                                f"词表 {len(self._cfg_fp[4])} → {len(now_fp[4])} 个词"
                            )
                        else:
                            reason = "persona 已更新"
                        raise _ConfigChanged(reason)
                continue
            stream_open = True
            await session.send_realtime_input(
                audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={_INPUT_RATE}")
            )
            self._n_sent += 1
            if self._n_sent % 100 == 0:  # 约每 2 秒一次
                self._log_delivery(
                    "STAT",
                    f"fed={self._n_fed} dropped={self._n_dropped} "
                    f"sent={self._n_sent} recv={self._n_recv} qsize={self._audio_queue.qsize()}",
                )

    async def _recv_loop(self, session):
        """双向接收循环：在外层保持循环调用 receive()，确保每个 turn 结束后自动接听下一轮。"""
        current_reply = []
        # 本轮有没有真的发出过工具调用（见 _PROMISED_DISPATCH_RE 上方那段注释）。
        turn_called_tool = False
        # 下面三个是「干完活没出声」那道检测器用的，都是**每轮清零的局部量**。
        # 本来想直接读 self._turn_first_audio_at，读完它的赋值条件才发现不行：
        # 那个字段的更新挂着 `self._turn_t0 is not None`，而 _turn_t0 在
        # turn_complete 那一刻就被 _log_turn_latency 清掉了 —— 工具结果回来后
        # 模型接着说的那些轮次根本不会更新它，读到的是上一轮的陈旧值。
        # 判「这一轮有没有出过声」必须用本轮自己的账，不能借别人的。
        turn_had_audio = False          # 本轮往 Discord 推过音频没有
        turn_interrupted = False        # 本轮被用户插话打断过没有
        turn_tool_tasks: list = []      # 本轮甩出去的工具任务，用来判「还没跑完」
        while self._running:
            async for resp in session.receive():
                d = resp.model_dump(exclude_none=True)
                sc = d.get("server_content", {})
                self._n_recv += 1
                # 诊断日志只记「有信息量」的那些。一轮下来 Gemini 会推几百条消息，
                # 其中大半是纯空包（心跳/分片边界）和逐 token 的 output_transcription
                # —— 后者最后有汇总的 🤖 行，逐条再打一遍等于把日志淹掉。
                # 剔掉 model_turn：它带着几十 KB base64 音频。
                _top = {k: v for k, v in d.items() if k != "server_content"}
                _sc = {
                    k: v for k, v in sc.items()
                    if k not in ("model_turn", "output_transcription")
                } if sc else {}
                if _top or _sc:
                    self._log_delivery(
                        "RECV", f"#{self._n_recv} {str(_top)[:400]} | sc={str(_sc)[:300]}"
                    )

                # 0. 续命：服务端周期性重置连接，靠这个句柄把同一个 session 接下去。
                upd = resp.session_resumption_update
                if upd and upd.resumable and upd.new_handle:
                    self._resume_handle = upd.new_handle
                if resp.go_away is not None:
                    # 断开预告。收到它就知道下一次 ABORTED 是计划内的。
                    self._log_delivery(
                        "SYSTEM", f"服务端预告断开，剩余 {resp.go_away.time_left}"
                    )

                # 1. 我说了啥 (用户输入语音实时转写)
                if "input_transcription" in sc:
                    user_text = _tidy(sc["input_transcription"].get("text", ""))
                    if user_text:
                        self._log_delivery("👤 [我说了啥]", user_text)

                # 1.5 打断 (barge-in)。你在它说话时插嘴，服务端 VAD 会立刻停止生成
                # 并推这个信号过来。**服务端停了不等于用户听到它停了** ——
                # 我们已经把生成好的音频整段塞进 Discord 播放器的缓冲区了，不主动清掉
                # 的话那几秒会继续播完，表现就是「我喊了停停停，它还在那儿说」。
                # 打断这件事必须两边都做：服务端停止生成 + 客户端丢弃待播音频。
                if sc.get("interrupted"):
                    turn_interrupted = True
                    self._drop_pending_audio()
                    # 被打断的半句不能跟下一轮拼在一起，否则日志里是两句话的残骸。
                    current_reply.clear()
                    self._log_delivery("✋ [被打断]", "用户插话，已丢弃未播完的音频")

                # 2. 它干了啥 / 思考过程 / 工具调用
                if resp.tool_call:
                    turn_called_tool = True
                    for call in resp.tool_call.function_calls or []:
                        # **不要在这里 await 执行。** 这个循环是唯一在消费服务端消息的
                        # 地方 —— 卡住它，音频回放（_play_gemini_audio）和后续的
                        # turn_complete 全都停摆。甩出去异步跑，跑完自己回 response。
                        _t = asyncio.create_task(self._handle_tool_call(session, call))
                        self._tasks.add(_t)
                        turn_tool_tasks.append(_t)

                if resp.server_content and resp.server_content.model_turn:
                    for part in resp.server_content.model_turn.parts:
                        if part.text and part.text.strip():
                            self._log_delivery("🧠 [它在思考/干了啥]", part.text.strip())
                        if part.inline_data and part.inline_data.data:
                            turn_had_audio = True
                            if self._turn_t0 is not None and self._turn_first_audio_at is None:
                                self._turn_first_audio_at = time.monotonic()
                            # 24kHz mono PCM 转换并推送到 Discord 语音
                            self._play_gemini_audio(part.inline_data.data)

                # 3. 回了什么 (模型语音输出的文字转录)
                if "output_transcription" in sc:
                    text_chunk = sc["output_transcription"].get("text", "")
                    if text_chunk:
                        current_reply.append(text_chunk)

                if sc.get("turn_complete"):
                    self._log_turn_latency(resp)
                    full_reply = _tidy("".join(current_reply))
                    if full_reply:
                        self._log_delivery("🤖 [它回了啥]", full_reply)
                        if not turn_called_tool and _PROMISED_DISPATCH_RE.search(
                            full_reply
                        ):
                            # 说了要派、却没调工具。用户以为办了，其实没有。
                            log.warning(
                                "口头承诺派活但本轮没有 tool_call: %s", full_reply[:200]
                            )
                            self._log_delivery(
                                "⚠️ [只说没派]",
                                f"这轮嘴上说要交给 {BOT_NAME}，但没发出 "
                                f"{_ASK_OWNER_TOOL} —— 用户听着像办了，实际没派出去",
                            )
                        self._log_delivery("DIVIDER", "-" * 60)
                    elif turn_called_tool and not turn_had_audio:
                        # ── 上面那道的**镜像**：工具调了，一个字没说。
                        #
                        # 两道检测器是同一个病的两半：
                        #   「只说没派」 = 嘴动了手没动 → 用户以为办了，其实没办
                        #   「干完活没出声」 = 手动了嘴没动 → 事办了，用户以为死机了
                        # 后者在语音场景里更糟 —— 用户听到的是一片安静，跟连接断了
                        # 长得一模一样，他会把整句话重说一遍。
                        #
                        # **不是假想出来的风险，是量出来的。** 2026-09-11 的工具对比台
                        # （scripts/live-tool-bench.py，13 例 × 2 模型 × 3 遍）里，
                        # 2.5 native-audio 有 6/39 次跑完工具后**完全不出声**，
                        # 全部集中在两道 jina 搜索题上。当时怀疑是转写投递的时序问题，
                        # 专门写了个探针数 inline_data 的字节数 —— **0 字节**，
                        # 它是真的什么都没生成。3.1 是 0/39。
                        #
                        # persona 里已经写了「调了工具就必须开口」，但**那是 prompt
                        # 压 prompt，压不死**。所以这里补一道确定性的：压不住至少查得到。
                        #
                        # 三个必须排除的**正当沉默**，否则这行会变成没人看的噪音：
                        #   1. 被打断 —— 用户插话本来就该闭嘴
                        #   2. 工具还没跑完 —— 结果回来后模型会在下一轮接着说
                        #   3. 本轮压根没调工具 —— 那是纯空轮，跟这个病无关
                        # 第 2 条是查日志确认过的：真实轨迹里 tool_call 和它的口头
                        # 答复落在**同一个 turn** 里（13:36:53 调用 → 13:37:04 才
                        # turn_complete），所以「turn_complete 时工具还挂着」确实
                        # 是异常态，不是常态。
                        if turn_interrupted:
                            pass
                        elif any(not t.done() for t in turn_tool_tasks):
                            self._log_delivery(
                                "SYSTEM", "本轮无输出，但工具还在跑，等结果回来再说"
                            )
                        else:
                            log.warning(
                                "本轮调了工具却没有任何输出（音频和转写都是空的）"
                            )
                            self._log_delivery(
                                "⚠️ [干完活没出声]",
                                "这轮工具调过了、也跑完了，但一个字都没说出口 —— "
                                "用户那头听到的是一片安静，会以为没听见或者挂了",
                            )
                            self._log_delivery("DIVIDER", "-" * 60)
                    current_reply.clear()
                    turn_called_tool = False
                    turn_had_audio = False
                    turn_interrupted = False
                    turn_tool_tasks = []
                    break

    def _log_turn_latency(self, resp):
        """一轮结束时记一行可解析的延迟。**没有起点就什么都不记。**

        为什么会没有起点：这一轮可能根本不是用户说话触发的（工具结果回来后
        模型接着说、或者重连后的第一条）。那种轮次量出来的数只会污染均值 ——
        宁可少一条样本，也不要一条对不上语义的样本。

        `thoughts` 是服务端报的思考 token 数，只有开了思考才有。它跟延迟是**两条
        独立证据**：延迟里混着网络和 VAD 等待，思考 token 是纯粹「它想了多少」。
        两个一起看才分得清「慢是因为想得多」还是「慢是因为别的」。
        """
        t0 = self._turn_t0
        if t0 is None:
            return
        self._turn_t0 = None
        now = time.monotonic()
        ttfa = (self._turn_first_audio_at - t0) if self._turn_first_audio_at else None
        thoughts = None
        try:
            um = getattr(resp, "usage_metadata", None)
            if um is not None:
                thoughts = getattr(um, "thoughts_token_count", None)
        except Exception:  # noqa: BLE001 — 纯观测，绝不能因为取不到字段炸掉主循环
            pass
        parts = [f"level={self._thinking}"]
        parts.append(f"ttfa={ttfa:.2f}" if ttfa is not None else "ttfa=n/a")
        parts.append(f"total={now - t0:.2f}")
        if thoughts is not None:
            parts.append(f"thoughts={thoughts}")
        self._log_delivery("⏱️ [延迟]", " ".join(parts))

    async def _handle_tool_call(self, session, call):
        """执行一次工具调用并把结果回传。**无论如何都要回一条 response。**

        模型在同步等我们（见文件上方「工具调用」那节），这期间用户听到的是死寂。
        任何一条没回的 response 都不是「这个工具失败了」，而是「这轮对话永久卡住」——
        所以下面的 try 兜的是**整个函数**，出错也要把错误当结果发回去让它继续说话。
        """
        args = dict(call.args or {})
        self._log_delivery("🛠️ [调工具]", f"{call.name}({args})")
        try:
            if call.name == _ASK_OWNER_TOOL:
                result = await self._ask_owner(args.get("task", ""))
            else:
                result = {"error": f"未知工具 {call.name}"}
        except Exception as e:
            log.warning("工具 %s 执行异常: %s", call.name, e)
            result = {"error": f"执行异常: {e}"}

        self._log_delivery("🛠️ [工具结果]", str(result)[:300])
        try:
            await session.send_tool_response(
                function_responses=[
                    types.FunctionResponse(name=call.name, id=call.id, response=result)
                ]
            )
        except Exception as e:
            # 到这一步回不去了，多半是连接已经断了 —— 外层会重连。
            log.warning("回传工具结果失败: %s", e)
        finally:
            self._tasks.discard(asyncio.current_task())

    async def _ask_owner(self, task: str) -> dict:
        """把一件事交给本 bot 的大脑去办。**发出去就返回，绝不等结果。**

        为什么必须是 fire-and-forget：3.1 的 function calling 是同步的
        （见文件上方「工具调用」那节），我们等多久用户就静音多久。而派给 bunny 的
        活按定义就是「要好几分钟」的活 —— 阻塞等于让用户对着死寂的麦克风坐五分钟。

        结果怎么回到用户耳朵里：走的是一条**本来就存在、只是没人接上**的回路 ——
        bunny 通过飞书 channel 回复时，`_send_voice_summary()` 会把文本 TTS 播进
        同一个 Discord 语音频道。

        **这里发的是用户原话，不是包装过的工单。** 以前这段会套一层
        「【来自语音助理的转交】…请务必用 <voice-summary> 标签」的模板，结果 bunny
        按文字模式作答、只有末尾那两三句被念出来，前面一大片用户全听不到。现在
        改成裸 task：谁转的写在 sender（`<bot>-voice`）里，飞书端认这个后缀就把
        整条当成「用户用嘴说的话」处理 —— 贴进聊天窗口留档、标 `[channel: voice]`、
        整段回复送 TTS。**别再往 task 前后加模板**，加了就等于替用户改口。

        sender 写成 `<bot>-voice` 而不是 bot 自己：一来 bot 收到自己发的消息很怪，
        二来这个后缀是飞书端切语音模式的**唯一判据**（feishu.py 搜 `_VOICE_SENDER_SUFFIX`）。
        """
        task = task.strip()
        if not task:
            return {"error": "任务内容为空，没法转交"}

        instruction = task

        env = dict(os.environ)
        env["BOT_NAME"] = f"{BOT_NAME}-voice"
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                os.path.expanduser("~/CloseCrab/scripts/inbox-send.py"),
                BOT_NAME,
                instruction,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        except Exception as e:
            log.warning("派活给 %s 失败（进程都没起来）: %s", BOT_NAME, e)
            return {"error": f"转交失败: {e}"}

        # 后台收尸。**不 await 就没人看 returncode**，Firestore 写失败会彻底静默 ——
        # 模型以为派出去了、用户以为在等回话，其实什么都没发生。
        reaper = asyncio.create_task(self._reap_inbox(proc, task))
        self._tasks.add(reaper)
        reaper.add_done_callback(self._tasks.discard)

        return {
            "status": "已转交",
            "note": (
                f"已经交给 {BOT_NAME} 了。它干完会自己在这个语音频道里说结果，"
                f"你不用等、也不用替它转述。跟用户说一句就继续聊别的。"
            ),
        }

    async def _reap_inbox(self, proc, task: str):
        """等派活进程收口并记日志。纯观测，不影响主链路。"""
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            log.warning("inbox-send 30 秒未收口，放弃等待: %s", task[:60])
            # **杀整个进程组，不是 proc.kill()。** 起的是 `/bin/sh -c`，它 fork 的
            # 孙进程会继承 stdout 那根管道；只杀 sh 的话孙进程还攥着管道活着，而
            # asyncio 的 Process.wait() 要等管道关闭才返回 —— 于是一路挂到命令
            # 自然结束。2026-09-08 在当时那个 shell 工具上实测过：单杀 sh 60 秒
            # 才收口还漏一个孤儿，杀整组 2 秒零残留。**返回值两种写法一模一样，
            # 只有耗时不同 —— 光看返回内容的测试抓不到这个。**
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            return
        text = out.decode("utf-8", "replace").strip()
        if proc.returncode == 0:
            log.info("派活给 %s 成功: %s", BOT_NAME, text[:200])
            self._log_delivery("📮 [已派活]", task[:200])
        else:
            log.warning("派活给 %s 失败 (exit=%s): %s", BOT_NAME, proc.returncode, text[:300])
            self._log_delivery("📮 [派活失败]", text[:200])

    def _drop_pending_audio(self):
        """丢掉 Discord 播放器里还没播出去的音频。被打断时调用。

        `clear()` 是播放器本来就有的方法（旧的 TTS 路径一直在用），
        Gemini Live 这条路之前没接上。
        """
        try:
            from .discord_voice_sidecar import _get_persistent_source
            source = _get_persistent_source()
            if source is not None:
                source.clear()
            # 音频流在这里断了，重采样器的接力状态跟着作废 —— 下一轮是全新的一段，
            # 拿上一段末尾的样本去插值没有意义。
            self._rs_out = None
        except Exception as e:
            log.warning("清空待播音频失败: %s", e)

    def _play_gemini_audio(self, pcm_24k: bytes):
        """将 Gemini Live 返回的 24kHz mono PCM 转为 48kHz stereo 并喂入 Discord 播放器。"""
        try:
            from .discord_voice_sidecar import _get_persistent_source
            source = _get_persistent_source()
            if source is None:
                return
            # 24kHz mono -> 48kHz mono（必须接力 state，理由见 __init__ 里 _rs_out 的注释）
            pcm_48k_mono, self._rs_out = audioop.ratecv(pcm_24k, 2, 1, 24000, 48000, self._rs_out)
            # 48kHz mono -> 48kHz stereo
            pcm_48k_stereo = audioop.tostereo(pcm_48k_mono, 2, 1, 1)
            source.write(pcm_48k_stereo)
            # 记在 write 之后：拿到字节但没喂进播放器不算「正在说话」。
            self._last_out_at = time.monotonic()
        except Exception as e:
            log.warning("Gemini 音频回放转换失败: %s", e)
