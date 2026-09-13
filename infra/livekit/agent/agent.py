"""LiveKit ⇄ Gemini 3.1 Live —— realtime 模式的最小可用 agent。

**跟 CloseCrab 那套 `closecrab/voice/livekit_io.py` 是两条完全不同的路。**
那边是三段式（STT → LLM → TTS，三个模型串起来，LiveKit 负责编排）；
这里是 realtime：音频进 Gemini、音频出 Gemini，**LiveKit 退化成纯 WebRTC 传输**。
所以这份文件里看不到 stt= / tts= —— 不是漏了，是这个模式下根本不存在那两段。

三条上游源码里查证过的硬约束（不是猜的，见 livekit-plugins-google 的
realtime_api.py）：

1. **3.1 Live 只能走 Gemini API key，不能走 Vertex。**
   `_validate_model_api_match()` 里两个 frozenset 把模型按 API 分死了，
   `gemini-3.1-flash-live-preview` 属于 KNOWN_GEMINI_API_MODELS，
   配 `vertexai=True` 直接抛 ValueError。
2. **工具列表在建会话时就冻结。** `RealtimeCapabilities.mutable_tools=False`，
   而且 3.1 还额外有一条 "limited mid-session update support" 的警告 ——
   instructions / chat context / tools 的改动要等下一个会话才生效。
   ⇒ 想加工具就重启 worker，别指望热更新。
3. **这个进程必须跑在 Gemini API 支持的地域里，不能跟 SFU 放一起。**
   SFU（livekit-server）在香港那台跳板机上 —— 那里是公网入口。但
   `generativelanguage.googleapis.com` 的 Live websocket 对香港出口直接回
   `1007 User location is not supported for the API use.`，同一把 key 从台湾
   这台出去就是 LIVE_OK。所以 agent 留在台湾，经 VPC peering 连过去当客户端。
   ⚠️ 这不是「没来得及搬」，是外部约束 —— 搬过去就是不能用。
4. **`session.say()` 用不了。** Gemini 的 realtime capabilities 里
   `supports_say` 是默认的 False，所以 1.6.0 那个填充语特性
   （`ctx.with_filler()`）在这条路上是死的。异步工具 `ctx.update()` 倒是能用，
   它走的是 `session.generate_reply`。

工具调用的框架整个是 LiveKit 提供的：Python 函数签名 → JSON schema →
FunctionDeclaration → 会话建立时下发 → Gemini 回 LiveServerToolCall →
框架执行 Python 函数 → send_tool_response。我们只写函数体。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import pathlib
import re
from typing import AsyncIterator

import aiohttp
from dotenv import load_dotenv
from livekit import rtc
from google.genai import types as genai_types
from livekit.agents import (
    Agent,
    AgentSession,
    APIConnectOptions,
    JobContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.agents.job import DEFAULT_PARTICIPANT_KINDS
from livekit.agents.voice.io import AudioInput

# `RoomOptions` 目前只在子包里，`livekit.agents` 顶层没导出（1.8.1 实测
# `from livekit.agents import RoomOptions` 直接 ImportError，提示你用
# RoomInputOptions）。但顶层那两个 RoomInputOptions/RoomOutputOptions
# 已经被标了 deprecated，运行时会打 warning ⇒ 用新的，从子包导。
from livekit.agents.voice.room_io import RoomOptions
from livekit.plugins.google.realtime import RealtimeModel
from livekit.plugins.google.tools import GoogleSearch

# override=True 不是可有可无的。load_dotenv 默认**不覆盖已存在的环境变量**，
# 而本机 `~/.claude/settings.json` 里躺着一把早已失效的 GEMINI_API_KEY，
# 交互 shell 会把它继承下来。结果是同一份代码、同一个 .env：
# systemd 起（干净环境）能通，手动在 shell 里跑就报
# "API key not valid" —— 看起来像 key 坏了，实际是读错了来源。
# 配置单一来源：以 .env 为准。2026-09-12 在排查地域问题时被它误导过一次。
load_dotenv(pathlib.Path(__file__).with_name(".env"), override=True)

logger = logging.getLogger("lk-gemini")

# 本地执行类工具（run_bash / read_file / write_file）的唯一落地目录。
# 不是安全沙箱 —— shell 命令自己能 cd 出去，别把它当隔离看。它解决的是
# 另一个问题：默认工作目录是什么由谁启动进程决定（systemd 起是 /，手动跑
# 是当前目录），不钉死的话「文件写哪去了」每次都不一样。
# 真正的信任边界在外面：房间在 IAP 后面，只有 Chris 进得来，跟 Discord 那条
# 一样是单人私有通道。
SCRATCH = pathlib.Path.home() / "lk-agent-scratch"
SCRATCH.mkdir(exist_ok=True)

# 语音场景下工具返回值要**短**。模型得把它念出来，几千字的正文念不完也没人听，
# 还白白占 context。这几个上限是按「一口气能说完」定的，不是随手写的。
_MAX_SHELL_OUT = 2000
_MAX_PAGE_TEXT = 3000
_MAX_FILE_READ = 4000

INSTRUCTIONS = """\
# 你是谁

**你是 这个 bot 的语音助手，你自己不叫 这个 bot。**
这个 bot 是跑在这台机器上的那个 AI bot —— 它能读文件、能跑命令、能上网翻资料、
记得住跨天的事，但它只会打字，接不上耳朵和嘴。你是它在语音这一头的那张嘴。

用户叫「这个 bot」的时候指的是它，不是你。要自报家门就说「我是 这个 bot 的语音助手」，
别应它的名，也别把它干过的事说成你干的。

用中文说话。技术名词保留英文原文（API、token、TPU、LiveKit、MoE 这类不要翻译）。

# 每一轮先判断：这一句该怎么接

问自己一句：**回答这个，我需不需要去看点什么、查点什么、动点什么？**

**张嘴就能答，而且答得对 —— 直接答。** 打招呼闲扯、概念解释、刚才这段对话
本身的事、没听清要他重说。快，不要为了显得严谨先去搜一圈。

**需要看、需要查、需要算 —— 自己动手，别凭记忆。**
机器上的事跑一条命令（run_bash），外面的事搜一下（search_web）。
你手上有工具，这类事不用问任何人，做完直接说结论。

**有后果的事 —— 停下来，先说你打算干什么，等他点头。**
删文件、kill 进程、重启服务、改配置、git push、装卸软件、往外发消息，
这些一律不许自作主张。语音是听出来的，听岔一个词就是另一条命令，
而这类操作错了收不回来。

**大活儿 —— 交给 这个 bot 本体，别在语音里硬扛。**
要写代码、要连着查半小时、要跨天记住的事，说一句「这个让 这个 bot 来做，
你在飞书里跟它说」。你这条线是即时对话，没有它那些记忆和长任务的本事。
**说清楚是「该由它做」，不是假装你已经派给它了** —— 你手上没有派活的通道。

**拿不准落在哪边，就往保守那边靠。** 只读的先做，有后果的先问。

# 说话方式

- 像跟熟人聊天，不像念稿子。句子短，长短交错，语气跟着内容走。
- 结论先行。先给答案，他要细节再展开。
- 不要客服腔，不要每次都同一套开场白。
- **对方在「听」不是在「看」。** 不念列表、不念表格、不念路径、不念长串数字、
  不念 markdown 符号，也不要说「第一点冒号」这种。
- 调完工具直接说结论，不要播报「我现在调用某某工具」。
- 房间里可能不止一个人（同一个人的手机和电脑也算两个）。听到两个声音叠在
  一起是正常的，不用问「是谁在说话」，按内容回应就行。

# 底线

- **不确定就说不确定，不要编。** 最危险的是版本号、日期、具体数字这类
  「听着像常识」的东西：脑子里恰好有个很像样的答案，说出口又足够具体，
  听着特别可信，而它可能早就过期了。这类一律先查再说。
- **「用工具把记忆里的答案落实一下」也算编** —— 先认定一个版本号，
  再让命令照着写，那不是查证。
- 没听清就说没听清，让他再说一遍。**别猜一个意思然后动手** ——
  你猜错了，命令会认认真真把错事办完。
- 工具报错了就说哪一步失败了，不要拿记忆里的答案顶上。
- 搜索先用 search_web；它整条不通的时候再退回内置的 Google 搜索。
- **绝对不要 kill 任何跟 bot.py 有关的进程。** 那是 bot 本体，杀了不会自己起来。
"""

DEFAULT_VOICE = "Aoede"

# ── 人格：按房间名选 ────────────────────────────────────────────────
# 房间名就是 bot 名。前端 `?room=bunny` → 房间 `bunny` → 读 `personas/bunny.md`。
# 一个 worker 进程伺候所有房间：LiveKit 是**每个房间派一个独立的 job 进程**，
# 所以六个 bot 不需要六个 systemd unit，job 进来自己看房间名加载对应人格即可。
#
# 为什么人格放本地文件而不去 Firestore 拿：`bots/{name}` 里**没有**人格字段
# （实测键只有 active_channel / description / model / worker_type 这些），
# CloseCrab 的 system prompt 是 `main.py` 在运行时拼出来的。为了一个
# description 把 google-cloud-firestore 拖进这个 venv 不划算。
PERSONA_DIR = pathlib.Path(__file__).with_name("personas")

# 房间名会被拿去拼文件路径，所以**必须**自己校验，不能信前端那层白名单 ——
# 那是另一个进程里的另一份配置，它松了这边就穿了。
_SAFE_ROOM = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,31}\Z")
_VOICE_LINE = re.compile(r"\Avoice:\s*(\w+)\s*\Z")


@dataclasses.dataclass(frozen=True)
class Persona:
    name: str
    voice: str
    instructions: str


def load_persona(room_name: str) -> Persona:
    """房间名 → 人格。没有对应文件就用内置默认人格（bunny）。

    文件格式刻意做得很薄，第一行可选：

        voice: Puck
        <空行>
        你是 ...

    只认 `voice:` 一个头部字段。再多的配置项要么进 CloseCrab 的 Firestore，
    要么就说明这里该换成真正的配置格式 —— 别在这儿长出第二套 YAML。
    """
    if not _SAFE_ROOM.match(room_name):
        # 随机房间名（voice_assistant_room_1234）走的就是这一条，不是异常。
        return Persona("default", DEFAULT_VOICE, INSTRUCTIONS)

    path = PERSONA_DIR / f"{room_name}.md"
    if not path.exists():
        logger.info("房间 %s 没有人格文件，用默认人格", room_name)
        return Persona("default", DEFAULT_VOICE, INSTRUCTIONS)

    text = path.read_text(encoding="utf-8")
    voice = DEFAULT_VOICE
    lines = text.splitlines()
    if lines and (m := _VOICE_LINE.match(lines[0])):
        voice = m.group(1)
        lines = lines[1:]
    instructions = "\n".join(lines).strip()
    if not instructions:
        # 空文件是配置错误，不是「没有人格」。说清楚再退回默认，
        # 否则下次只会看到「它怎么不像 bunny 了」。
        logger.warning("人格文件 %s 是空的，退回默认人格", path.name)
        return Persona("default", DEFAULT_VOICE, INSTRUCTIONS)

    logger.info("房间 %s 加载人格 %s（voice=%s）", room_name, path.name, voice)
    return Persona(room_name, voice, instructions)


# 曾经这里有一个 `get_current_time` 工具。删了 —— 查时间只是「跑一条命令」的
# 一个特例（`TZ=Asia/Hong_Kong date`），没必要为它单独占一个 schema 槽位。
# 每多一个工具，模型每轮都要多读一份描述、多做一次选择；能力没增加，
# 选错的机会反而多了一个。判断标准：这个工具**能不能被已有工具一行做掉**，
# 能就别加。


async def _jina(url: str, *, params: dict | None = None) -> dict | str:
    """调一次 Jina。成功回 dict，失败回一句人话（**不是** dict）。

    key 从磁盘读不从代码里写。没有 key 就老实说没有 —— 静默降级成「搜不到」
    比报错更糟，模型会把它当成「这件事不存在」。
    """
    key_file = pathlib.Path.home() / ".closecrab-jina-auth"
    if not key_file.exists():
        return "不可用：本机没有配 Jina key。"
    headers = {
        "Authorization": f"Bearer {key_file.read_text().strip()}",
        "Accept": "application/json",
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                url, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                if resp.status != 200:
                    return f"请求失败：HTTP {resp.status}"
                return await resp.json()
    except Exception as exc:                      # noqa: BLE001 — 要把原因说出来
        logger.warning("jina 调用失败 url=%s: %s", url, exc)
        return f"出错：{exc}"


@function_tool
async def search_web(query: str) -> str:
    """联网搜索（Jina）。**要上网查东西，先用这个，不要先用内置的 Google 搜索。**

    什么时候用：任何你不是当场知道的事实 —— 最新消息、版本号、价格、天气、
    某个人是谁、某个项目现在什么状态、某个报错别人怎么解决的。
    尤其是**版本号、日期、具体数字**这类「听着像常识」的东西：脑子里恰好有个
    很像样的答案、说出口又足够具体，这是最容易编错又最难被发现的一类。
    这类一律先搜。

    什么时候**不要**用：这台机器上的事实（进程、文件、时间、磁盘）——
    那是 run_bash 的活，网上搜不到。已经知道网址、要看正文，用 read_url。

    返回最多 4 条，每条是标题加一段摘要。**摘要不是全文** ——
    要根据它下判断之前，先想想够不够；不够就挑一条 read_url 进去看。

    失败是会说话的，三种情况分得很清楚，不要混为一谈：
    「不可用」是本机没配 key，换个词搜一百次也一样，直接告诉用户；
    「请求失败／出错」是这次没打通，值得换个说法重试一次；
    「搜到 0 条结果（查询本身成功了）」才是真的没搜着 —— 这跟「搜不了」
    是两回事，别把它说成「这个东西不存在」。
    Jina 这条路整条不通时，再退回内置的 Google 搜索兜底。

    Args:
        query: 搜索词。用最贴近用户原话的说法，别自作主张改写成专业术语 ——
            他说「那个天猫精灵为啥不响应」就照搜，别换成「智能音箱故障排查」。
            中文问题用中文搜；只在结果明显不够时再用英文搜一次。
    """
    logger.info("tool: search_web q=%r", query)
    data = await _jina("https://s.jina.ai/", params={"q": query})
    if isinstance(data, str):
        return f"搜索{data}"

    hits = (data.get("data") or [])[:4]
    if not hits:
        # 「0 条」和「查询失败」必须分开说，否则下游没法区分
        return f"搜到 0 条结果（查询本身成功了）：{query}"
    return "\n\n".join(
        f"{i}. {h.get('title', '')}\n{(h.get('description') or h.get('content', ''))[:400]}"
        for i, h in enumerate(hits, 1)
    )


@function_tool
async def read_url(url: str) -> str:
    """打开一个网址，把正文读回来（去掉导航和广告，只留文字）。

    什么时候用：已经有网址了，要看里面**具体**写了什么 —— 用户念了一个链接、
    search_web 的摘要不够用、要核对文档原文而不是二手说法。

    什么时候**不要**用：还不知道网址（先 search_web）；要看的是这台机器上的
    文件（用 read_file 或 run_bash）。

    正文最多截 3000 字，长文章会被切断。所以别指望一次读完一篇长文档 ——
    需要的话先搜出更精确的页面，或者分几次读不同的页面。
    读回来之后**口述要点**，不要把正文念出来。

    抓不到时会明说是抓不到，不要拿记忆里对这个页面的印象替代它。

    Args:
        url: 完整网址。没带 http:// 或 https:// 会自动补 https://。
            用户是念出来的，听着不确定就先跟他确认，别猜一个拼法。
    """
    logger.info("tool: read_url %s", url)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    data = await _jina("https://r.jina.ai/" + url)
    if isinstance(data, str):
        return f"抓取{data}"
    d = data.get("data") or {}
    text = (d.get("content") or "").strip()
    if not text:
        return f"抓到了页面但正文是空的：{url}"
    return f"{d.get('title', '')}\n\n{text[:_MAX_PAGE_TEXT]}"


@function_tool
async def run_bash(command: str) -> str:
    """在这台机器上跑一条 shell 命令，返回退出码、stdout 和 stderr。

    这是你**唯一**能碰到这台机器的工具，也是你能把「我猜」变成「我看过」的
    唯一办法。凡是能跑一条命令确认的事，就别凭印象答。

    典型用法，都是一行的事：
    - 时间日期 → `TZ=Asia/Hong_Kong date`（**问几点就跑这个**，别心算时区）
    - 算数 → `python3 -c "print(...)"`（心算会错，而且错得很自信）
    - 机器状态 → `uptime` / `df -h` / `free -g` / `nvidia-smi`
    - 进程在不在 → `pgrep -af <名字>` / `systemctl is-active <服务>`
    - 服务日志 → `journalctl -u <服务> -n 30 --no-pager`
    - 找文件 → `ls`、`find`、`grep -rn`

    **动手之前先分清只读还是有后果。** 看一眼（ls、cat、grep、date、
    systemctl status）随便跑。**会改变状态的不要自己跑** —— 删文件、
    kill 进程、重启服务、改配置、git push、装卸软件、往外发消息。
    这类先说清楚你打算跑哪条命令、会有什么后果，等用户点头。
    语音是听出来的，听岔一个词就可能变成另一条命令，代价不对称。

    **一条命令红线：绝对不要 kill 任何跟 bot.py 有关的进程** ——
    那是这台机器上跑着的 bot 本体，杀了它自己就没了，而且不会自动起来。

    执行上的硬限制，别跟它们较劲：
    - 20 秒超时，超时会真把进程杀掉。要跑长活，用 `setsid ... &` 丢后台，
      然后分几次回来看日志；别写一条要跑两分钟的命令然后指望它能回来。
    - 工作目录固定在一个暂存目录。要看别处的文件就写绝对路径。
    - 输出截 2000 字符。输出会很大的命令，自己先用 `| tail -30` 或
      `| wc -l` 收一下，不要把一屏日志倒出来念。

    返回里「退出码 0 但没有任何输出」和「命令没跑成」是两回事，
    前者是真的没输出，照实说，不要脑补一个结果。

    Args:
        command: 要执行的 shell 命令，一条就好。需要多步就用 && 串起来。
    """
    logger.info("tool: run_bash %r", command)
    try:
        # 用 asyncio 的 subprocess 而不是 subprocess.run —— 这是 agent 的事件循环，
        # 同步阻塞 20 秒会把音频收发一起卡住，听感上就是「它突然聋了」。
        proc = await asyncio.create_subprocess_shell(
            command, cwd=SCRATCH,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        # 超时要真把进程杀掉。只 return 不 kill 会留下孤儿进程在后台跑。
        proc.kill()
        await proc.wait()
        return "命令超时（20 秒），已终止。"
    except Exception as exc:                      # noqa: BLE001
        logger.warning("run_bash failed: %s", exc)
        return f"执行出错：{exc}"

    parts = [f"退出码 {proc.returncode}"]
    if out:
        parts.append("stdout:\n" + out.decode("utf-8", "replace")[:_MAX_SHELL_OUT])
    if err:
        parts.append("stderr:\n" + err.decode("utf-8", "replace")[:_MAX_SHELL_OUT])
    if not out and not err:
        # 「没输出」和「没执行」必须分得开，跟上面 0 条结果同理
        parts.append("（命令执行了，但没有任何输出）")
    return "\n".join(parts)


def _in_scratch(path: str) -> pathlib.Path:
    """把任意用户给的路径钉进暂存目录，只取文件名部分。

    语音场景下路径是**听**出来的，模型很容易顺手拼一个 /etc/xxx 或 ../xxx。
    读写这两个工具不接受目录穿越 —— 不是防攻击（run_bash 就在旁边），
    是防口误：说「读一下 hosts」不该真去读 /etc/hosts。
    """
    return SCRATCH / pathlib.PurePath(path.strip()).name


@function_tool
async def read_file(path: str) -> str:
    """读暂存目录里的一个文件（就是 write_file 写的那些）。

    这个工具**只能看暂存目录**，而且只认文件名 —— 你给它带路径的东西，
    它也只取最后那一截。要读机器上别处的文件，用 run_bash 加 cat。
    这不是防攻击（run_bash 就在旁边），是防口误：语音里说「读一下 hosts」，
    不该真去读 /etc/hosts。

    最多返回 4000 字，长文件会被截断 —— 截断了要说一声，别当成全文。

    Args:
        path: 文件名，比如 notes.md。不用写目录。
    """
    logger.info("tool: read_file %s", path)
    p = _in_scratch(path)
    if not p.exists():
        return f"文件不存在：{p.name}"
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_READ]
    except Exception as exc:                      # noqa: BLE001
        return f"读取出错：{exc}"


@function_tool
async def write_file(path: str, content: str) -> str:
    """把内容写进暂存目录里的一个文件。**整份覆盖，不是追加。**

    用来记东西：用户口述的备忘、一段要留着的结论、一份草稿。

    覆盖这件事要当心 —— 同一个文件名写第二次，第一次的内容就没了。
    要往已有文件后面加东西，先 read_file 读回来，拼好再整份写回去；
    或者直接用 run_bash 加 `>>`。

    跟 read_file 一样只认文件名，落在暂存目录里。**这里不是给你改这台机器上
    真实文件的地方** —— 那种事属于「有后果」，先问用户。

    Args:
        path: 文件名，比如 notes.md。
        content: 要写进去的**完整**内容，不是增量。
    """
    logger.info("tool: write_file %s (%d 字)", path, len(content))
    p = _in_scratch(path)
    try:
        p.write_text(content, encoding="utf-8")
        return f"已写入 {p.name}，{len(content.encode())} 字节。"
    except Exception as exc:                      # noqa: BLE001
        return f"写入出错：{exc}"


# ── 混音池：把房间里所有人的麦克风合成一条流 ──────────────────────────
# 显式派发用的 worker 名字。ensure_rooms.py 要用同一个字符串，改这里就得改那里。
_AGENT_NAME = "gemini-live"

# 房间空了多久才放掉 Gemini 会话。刷新页面 / 换设备 / 网络抖一下都会让房间短暂
# 空一瞬，那种时候不该把对话历史一起丢掉，所以给一分钟缓冲。
_IDLE_GRACE_SEC = float(os.getenv("GEMINI_IDLE_GRACE_SEC", "60"))

_MIX_SAMPLE_RATE = 24000      # 跟框架 AudioInputOptions 的默认值对齐
_MIX_NUM_CHANNELS = 1
_MIX_FRAME_MS = 50


class MixedRoomAudioInput(AudioInput):
    """把房间里**所有人**的麦克风混成一条流喂给模型。

    为什么要自己写：框架默认的 RoomIO 只把 agent 的「耳朵」挂在**一个**参与者
    身上（`RoomOptions.participant_identity` 不给就挂第一个进来的，见
    `room_io.py:_on_participant_available`）。你用笔记本先进房间、再掏出手机
    说话，它一个字都收不到 —— 耳朵还贴在笔记本那边。

    这对聊天室的语义是错的。房间本来就是广播域：SFU 把每个人的音轨转发给所有
    人，浏览器在本地把几路叠起来播。agent 不该是例外。

    真正的约束不在 LiveKit 而在模型 —— Gemini Live 的 websocket 只吃**一条**
    音频流。所以正确的做法不是「切换耳朵」，是在喂进去之前先混音。SDK 里现成
    就有 `rtc.AudioMixer`（N 条流进，逐样本相加再 clip，一条流出）。

    代价说清楚：混完就分不出谁是谁了，模型没有说话人分离。两个人同时说话，它
    听到的是叠在一起的声音 —— 跟真人坐在会议室里听到的一样。

    还有一个**声学**问题它解决不了：两台设备摆在同一张桌子上时，A 的喇叭放出
    agent 的声音会被 B 的麦克风收进来，混进池子再送回模型，于是模型听见自己。
    浏览器的回声消除只消得掉本机喇叭，消不掉旁边那台。物理上挨着就静音一台。

    三个实现上的坑：

    1. **只混没静音的轨。** mixer 对超过 100 ms 没出数的流每轮打一条 warning
       （`audio_mixer.py:_get_contribution`）。静音的参与者根本不发包，挂在池子
       里就是每秒 10 条日志。所以按 track_muted / track_unmuted 动态增删。
    2. **静音期要自己补帧。** 没静音但没说话时 Opus DTX 会停发包，同样触发上面
       那个 warning。`_paced()` 负责：有真音立刻转发，超过一帧时长没来就补一帧
       静音。顺带把整条流钉在实时速率上 —— mixer 自己不限速，出帧节奏完全靠
       输入流的自然节奏定拍。
    3. **全员静音时要真的停。** 此时池子里一条流都没有，mixer 空转 sleep、不产出
       任何帧 ⇒ 什么都不会发给 Gemini。跟单人模式静音时的行为一致，不会白烧配额。
    """

    def __init__(self, room: rtc.Room) -> None:
        super().__init__(label="MixedRoomAudio")
        self._room = room
        self._chunk = int(_MIX_SAMPLE_RATE * _MIX_FRAME_MS / 1000)
        self._mixer = rtc.AudioMixer(
            sample_rate=_MIX_SAMPLE_RATE,
            num_channels=_MIX_NUM_CHANNELS,
            blocksize=self._chunk,
        )
        # publication.sid → (原始流, 喂给 mixer 的那个包装生成器)
        # 两个都要留着：remove_stream 认的是生成器对象本身，关闭要关原始流。
        self._sources: dict[str, tuple[rtc.AudioStream, AsyncIterator[rtc.AudioFrame]]] = {}
        # 关流的 task 要留个强引用，否则 asyncio 只持弱引用，可能没跑完就被 GC 掉。
        self._closing: set[asyncio.Task] = set()
        # 「房间里此刻有没有活着的人声轨」。只用来做日志和排障 ——
        # **会话生命周期不看它**，看的是 _Presence（有没有人进房）。
        # 原因见 _Presence 的文档：前端有个 20 秒握手死线，等到有人开口才建会话
        # 就一定会超时。留着这个信号是因为「订阅了几路」和「有没有人在房间」是
        # 两件事，分开看才查得出「人在但麦克风没推上来」这类问题。
        self.voice_present = asyncio.Event()

        room.on("track_subscribed", self._on_track_subscribed)
        room.on("track_unsubscribed", self._on_track_unsubscribed)
        room.on("track_muted", self._on_track_muted)
        room.on("track_unmuted", self._on_track_unmuted)

        # 已经在房间里、已经在说话的人：事件是不会补发的，得自己扫一遍。
        # 这条不是防御性代码 —— agent 加入时房里通常**已经**有人了。
        for participant in room.remote_participants.values():
            for pub in participant.track_publications.values():
                if pub.track is not None and not pub.muted:
                    self._add(pub.sid, pub.track, participant)

    # -- 事件 --------------------------------------------------------

    @staticmethod
    def _wanted(kind: int, participant: rtc.RemoteParticipant) -> bool:
        # 参与者类型沿用框架那份白名单（standard / sip / connector），
        # 关键是把 AGENT 挡在外面 —— 房里要是再进来一个 agent，它的声音会被
        # 混进池子送回模型，模型就开始跟自己说话。
        return kind == rtc.TrackKind.KIND_AUDIO and participant.kind in DEFAULT_PARTICIPANT_KINDS

    def _on_track_subscribed(self, track, publication, participant) -> None:
        if self._wanted(track.kind, participant) and not publication.muted:
            self._add(publication.sid, track, participant)

    def _on_track_unsubscribed(self, track, publication, participant) -> None:
        self._remove(publication.sid, "取消订阅")

    def _on_track_muted(self, participant, publication) -> None:
        self._remove(publication.sid, "静音")

    def _on_track_unmuted(self, participant, publication) -> None:
        track = getattr(publication, "track", None)
        if track is not None and self._wanted(track.kind, participant):
            self._add(publication.sid, track, participant)

    # -- 增删 --------------------------------------------------------

    def _add(self, sid: str, track: rtc.Track, participant: rtc.RemoteParticipant) -> None:
        if sid in self._sources:
            return
        stream = rtc.AudioStream.from_track(
            track=track,
            sample_rate=_MIX_SAMPLE_RATE,
            num_channels=_MIX_NUM_CHANNELS,
            frame_size_ms=_MIX_FRAME_MS,
        )
        paced = self._paced(stream)
        self._sources[sid] = (stream, paced)
        self._mixer.add_stream(paced)
        self.voice_present.set()
        logger.info("混音池 +1：%s（共 %d 路）", participant.identity, len(self._sources))

    def _remove(self, sid: str, why: str) -> None:
        entry = self._sources.pop(sid, None)
        if entry is None:
            return
        stream, paced = entry
        self._mixer.remove_stream(paced)
        # 事件回调是同步的，关流得丢给 event loop。
        task = asyncio.create_task(self._close_source(stream))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)
        if not self._sources:
            self.voice_present.clear()
        logger.info("混音池 -1：%s（%s，共 %d 路）", sid, why, len(self._sources))

    @staticmethod
    async def _close_source(stream: rtc.AudioStream) -> None:
        # 只关底层 AudioStream，**不要**去 aclose 那个 `_paced` 生成器：
        # mixer 的 `_get_contribution` 可能正 await 着它的 `__anext__`，
        # 这时候 aclose 会抛 "asynchronous generator is already running"。
        # 底层一关，`_paced` 下次被拉动就自然 return，没人拉就等 GC 收 —— 两条路都干净。
        try:
            await stream.aclose()
        except Exception:                          # noqa: BLE001 — 关流失败不该拖垮会话
            logger.debug("关闭音频源时出错", exc_info=True)

    # -- 限速转发 ----------------------------------------------------

    async def _paced(self, stream: rtc.AudioStream) -> AsyncIterator[rtc.AudioFrame]:
        """转发真音；一帧时长内没等到就补一帧静音。

        写法上有个必须注意的点：**不能**用 `asyncio.wait_for(it.__anext__())`。
        超时会把里面那个 `__anext__` 取消掉，而它已经从队列里摘走的那一帧就丢了
        （asyncio 里 getter 被取消和 set_result 之间有个众所周知的竞态）。
        所以把 task 留着跨轮次复用 —— 这轮没等到，下轮接着等同一个 task。
        """
        silence = rtc.AudioFrame(
            b"\x00" * (self._chunk * 2 * _MIX_NUM_CHANNELS),
            _MIX_SAMPLE_RATE,
            _MIX_NUM_CHANNELS,
            self._chunk,
        )
        it = stream.__aiter__()
        pending: asyncio.Task | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(it.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=_MIX_FRAME_MS / 1000)
                if not done:
                    yield silence
                    continue
                task, pending = pending, None
                try:
                    yield task.result().frame
                except StopAsyncIteration:
                    return
        finally:
            if pending is not None:
                pending.cancel()

    # -- AudioInput 接口 ---------------------------------------------

    async def __anext__(self) -> rtc.AudioFrame:
        return await self._mixer.__anext__()

    async def aclose(self) -> None:
        self._room.off("track_subscribed", self._on_track_subscribed)
        self._room.off("track_unsubscribed", self._on_track_unsubscribed)
        self._room.off("track_muted", self._on_track_muted)
        self._room.off("track_unmuted", self._on_track_unmuted)
        for sid in list(self._sources):
            stream, paced = self._sources.pop(sid)
            self._mixer.remove_stream(paced)
            await self._close_source(stream)
        await self._mixer.aclose()


def _build_session(persona: Persona) -> AgentSession:
    return AgentSession(
        # 导入路径是 `livekit.plugins.google.realtime`, 不是老文档里那个
        # `google.beta.realtime` —— 后者在 1.8.1 里已经不是真模块了
        # (`hasattr(google.beta, 'realtime')` 为 True 但 import 报 ModuleNotFound)。
        llm=RealtimeModel(
            model="gemini-3.1-flash-live-preview",
            api_key=os.environ["GEMINI_API_KEY"],
            # vertexai 显式写 False：3.1 Live 走 Vertex 会被 plugin 直接拒掉，
            # 留默认值能跑，但写出来的目的是让下一个读代码的人不必去翻源码。
            vertexai=False,
            voice=persona.voice,
            temperature=0.8,
            instructions=persona.instructions,
            # 会话时长：官方文档写「不开压缩时纯音频会话上限 15 分钟」
            # (ai.google.dev/gemini-api/docs/live-session)。开了滑动窗压缩就
            # **没有上限**了 —— 超过 trigger_tokens 就把最老的一段丢掉接着说，
            # 而不是把整个会话掐掉。两个参数都留空 = 用服务端默认阈值。
            #
            # ⚠️ 这条治的**不是**我们现在每 2 分半一次的 1008。那个是
            # gemini-3.1-flash-live-preview 这个 preview 模型自己的毛病：
            # 约 170 秒一到就断，**跟有没有人说话、有没有压缩都无关**，
            # 而且断之前不发 goAway 也不发 sessionResumptionUpdate
            # （官方论坛 172602 号帖，2.5 Live 没这问题）。
            # 治它只能靠下面那个 conn_options 把重连做得又快又稳。
            # 压缩在这里是拆掉 15 分钟那道**另一个**天花板，别把两件事记混。
            context_window_compression=genai_types.ContextWindowCompressionConfig(
                sliding_window=genai_types.SlidingWindow(),
            ),
            # 重连预算。默认是 max_retry=3 / retry_interval=2s，对「每 170 秒
            # 必断一次」这种节奏太紧：第一次重试是 0.1 秒（plugin 写死的），
            # 之后每次都等满 retry_interval。收到数据就清零计数
            # (realtime_api.py:1062)，所以正常情况下永远用不到 8 次 ——
            # 8 是留给 Gemini 侧短暂抽风的，别让它把整个 agent 拖死。
            conn_options=APIConnectOptions(max_retry=8, retry_interval=0.5, timeout=10.0),
        ),
    )


def _build_agent(persona: Persona) -> Agent:
    return Agent(
            instructions=persona.instructions,
            # 最后那个 GoogleSearch() 不是函数工具，是 Gemini 的**内置**工具
            # （provider tool）—— 检索在 Google 服务端完成，不经过这个进程。
            # ⚠️ 内置工具和函数工具**混用**有个硬门槛：plugin 的
            # create_tools_config() 里写着，混用只在 Gemini 3 Developer API 上
            # 成立，Vertex AI 不支持（那边会直接把 provider tool 丢掉并打 warning）。
            # 我们正好是 3.1 Live + Gemini API key，所以能混。
            # 这是「没搬去 Vertex」这个决定顺带换来的能力，别在不知情的情况下
            # 把模型切到 Vertex —— 内置搜索会静默消失。
            #
            # 顺序有意义：**Jina 的 search_web 在前，内置 GoogleSearch 垫底**。
            # 两条路都能上网，但可观测性差很远 —— search_web 是普通函数工具，
            # 查询词和返回的四条结果都进我们自己的日志，搜歪了看得见；
            # GoogleSearch 的检索发生在 Google 服务端，这个进程**一个字都看不到**，
            # 出问题时「它搜过了但没搜着」和「它压根没搜、凭记忆答的」长得一样。
            # 所以默认走看得见的那条，Jina 整条不通时再由它兜底。
            tools=[
                search_web,
                read_url,
                run_bash,
                read_file,
                write_file,
                GoogleSearch(),
            ],
    )


_ROOM_OPTIONS = RoomOptions(
    # 音频输入我们自己接管（那个混音池），所以把框架那条关掉。
    # 关掉的只是 RoomIO 的音频输入，**订阅不受影响** ——
    # `AgentSession.start()` 会去跑 `job_ctx.connect()`，默认
    # AutoSubscribe.SUBSCRIBE_ALL，所有音轨照常订阅、track_subscribed
    # 照常触发。副作用只有两个：pre_connect_audio（进房前那几百毫秒的
    # 缓冲）和 plugin 的 noise_cancellation 钩子（我们没用）。
    audio_input=False,
    # 默认 True = 「跟 agent 绑定的那个参与者一走，就把这个 job 关掉」。
    # 在共享房间里这是错的：手机退出不该把笔记本的会话一起收走。
    # 房间现在是常驻的（SFU 侧 empty_timeout / departure_timeout 都设成了
    # 十年），所以这个 job 进程从房间建起来那一刻活到天荒地老 —— 换设备、
    # 掉线重连回来，都不用重新派 job。
    close_on_disconnect=False,
)


def _humans(room: rtc.Room) -> int:
    """房间里有几个**人**。

    `bunny-speaker`（本体那条只推不收的流）和 agent 自己都是 AGENT kind，
    不算人 —— 算进来的话 bunny 的会话就永远放不掉了。
    """
    return sum(1 for p in room.remote_participants.values() if p.kind in DEFAULT_PARTICIPANT_KINDS)


def _who(p: rtc.RemoteParticipant | rtc.LocalParticipant) -> str:
    """一个参与者的一行画像：身份 / kind / 属性 / 发布了几条轨。

    这三样凑齐才看得出「前端会挑中谁当语音助手，以及它认为那人是什么状态」。
    前端的判据是**第一个 kind=AGENT 且没有 `lk.publish_on_behalf` 的参与者**
    （useAgent.ts:528-536），拿到之后看它的 `lk.agent.state` —— 所以只打身份
    是不够的。

    2026-09-13「每个房间都在第 20 秒断」就是靠这行日志排掉了两个嫌疑：本体那条
    `<bot>-speaker` 不是真凶（hulk 房里根本没有它，照样断），真凶是
    **空闲 agent 身上粘着的 `lk.agent.state: listening`** —— 见
    `_clear_agent_state`。
    """
    attrs = ",".join(f"{k}={v}" for k, v in sorted(p.attributes.items())) or "-"
    return f"{p.identity}(kind={p.kind} attrs=[{attrs}] tracks={len(p.track_publications)})"


def _roster(room: rtc.Room) -> str:
    """房间里所有远端参与者的画像，逗号分隔。"""
    return " | ".join(_who(p) for p in room.remote_participants.values()) or "（空）"


class _Presence:
    """「房间里有没有人」，做成一个可等待的信号。

    为什么判据是**人在不在**而不是**有没有音频帧**：前端（@livekit/components-react
    的 useAgent）在用户连上后起一个 **20 秒**的定时器，到点去看 agent 报没报
    `lk.agent.state`，没报就直接判 "Agent joined the room but did not complete
    initializing" 并把会话掐掉（useAgent.ts:329-341）。而那个属性是
    AgentSession 起来之后才写的。

    按音频帧建会话就踩这个：agent 人在房间里坐着、但不建会话 ⇒ 属性一直不存在
    ⇒ 每个房间都必然在第 20 秒被前端判死。实测四次尝试全是 19-20 秒离开。

    所以门槛前移到「有人进房」—— 进房到握手完成通常一秒出头，离 20 秒很远。
    「没人跟它说话就断」这条要求本身不受影响：房间空了照样放掉会话。
    """

    def __init__(self, room: rtc.Room) -> None:
        self._room = room
        self.present = asyncio.Event()
        room.on("participant_connected", self._on_connected)
        room.on("participant_disconnected", self._on_disconnected)
        self._recount(None)
        logger.info("房间 %s 进场清点：%s", room.name, _roster(room))

    def _on_connected(self, p) -> None:
        logger.info("＋进房 %s", _who(p))
        self._recount(p)

    def _on_disconnected(self, p) -> None:
        logger.info("－离开 %s", _who(p))
        self._recount(p)

    def _recount(self, _participant) -> None:
        if _humans(self._room):
            self.present.set()
        else:
            self.present.clear()

    async def wait_until_empty(self, grace: float) -> None:
        """等到「房间里没人，并且连续没人满 grace 秒」。中途有人回来就重新计时。

        为什么要缓冲：刷新页面、从手机切到电脑、网络抖一下重连，都会让房间短暂
        空一瞬。这种时候把 Gemini 会话连同对话历史一起丢掉是错的。

        有人时按秒轮询，不去给 Event 加「等清空」的原语 —— asyncio.Event 只能等
        set 不能等 clear，自己造一个要处理 set/clear 之间的竞态，而这里 1 Hz 的
        轮询成本可以忽略、正确性一眼能看穿。
        """
        while True:
            if self.present.is_set():
                await asyncio.sleep(1)
                continue
            try:
                await asyncio.wait_for(self.present.wait(), timeout=grace)
            except asyncio.TimeoutError:
                return


async def _unpublish_agent_tracks(room: rtc.Room) -> None:
    """把 AgentSession 发布的音轨收回来。

    **框架不会自己收。** `_ParticipantAudioOutput.aclose()` 只关音源，没有
    unpublish（room_io/_output.py:102-108）。会话每重建一次就在 agent 身上多留
    一条死轨，客户端会把它们全订阅了。实测 bunny 关一次会话后挂着两条。

    这个 job 进程自己不发布任何别的东西，所以「本地发布的音轨」就等价于
    「上一次会话留下的」，可以整片收掉。本体那条 `<bot>-speaker` 是**另一个
    参与者**，不在这里。
    """
    for pub in list(room.local_participant.track_publications.values()):
        if pub.kind == rtc.TrackKind.KIND_AUDIO:
            try:
                await room.local_participant.unpublish_track(pub.sid)
            except Exception:  # noqa: BLE001 — 收不回来也不该拖垮下一轮会话
                logger.warning("回收残留音轨失败：%s", pub.sid, exc_info=True)


async def _clear_agent_state(room: rtc.Room) -> None:
    """会话放掉之后，把 `lk.agent.state` 从自己身上抹掉。

    **这不只是卫生问题，它是 2026-09-13「每次都在第 20 秒断」的一半病因。**

    `AgentSession` 只管往上写状态，散场不负责擦。于是一个空房间里的常驻 agent
    会一直挂着 `lk.agent.state: listening` —— 明明没有任何 Gemini 连接。下一个
    人进来、新会话起来，框架再写一次 `listening`：**值没变，SDK 就不发
    AttributesChanged**。而前端的 `useAgent` 只在挂载那一刻 seed 一次属性
    （那时还没连上房间，seed 的是空对象），之后纯靠事件学 —— 事件永远不来，
    它就永远认为对方还在 connecting，20 秒握手死线一到判死。

    擦掉之后，下一轮的 `listening` 就是一次货真价实的变化，事件正常发出。
    顺带把语义摆正了：没有会话的时候本来就不该宣称自己在听。

    （前端那半边也修了 —— `patches/@livekit__components-react@2.9.20.patch`
    让它订阅前先回读一次当前属性。两边都改是故意的：只改前端治不了「空闲
    agent 撒谎说自己 ready」，只改这边治不了「人在 grace 期内重连」。）

    空字符串就是删除：服务端把 value 为 "" 的 key 从属性表里摘掉。
    """
    try:
        await room.local_participant.set_attributes({"lk.agent.state": ""})
    except Exception:  # noqa: BLE001 — 擦不掉也不该拖垮下一轮会话
        logger.warning("清 lk.agent.state 失败", exc_info=True)


async def entrypoint(ctx: JobContext) -> None:
    # 房间名就是 bot 名。一个 worker 伺候所有房间 —— LiveKit 给**每个房间**派一个
    # 独立的 job 进程，所以六个 bot 不需要六个 systemd unit，进来自己认房间就行。
    persona = load_persona(ctx.room.name)

    # 房间常驻 ⇒ 这个 job 也常驻。但**Gemini 连接不常驻**：房间空着的时候占一条
    # Live 连接毫无意义，而且那条连接每 170 秒自己断一次、每次都刷一条 error。
    # 所以这里把两件事拆开：
    #   房间 + job 进程 + persona + 工具 —— 一直在，进房即可说话，零启动延迟；
    #   Gemini 会话                     —— 有人进房就建，房间空满 grace 秒就放掉。
    #
    # 判据是「有没有人」不是「有没有声」—— 前端有个 20 秒握手死线，详见 _Presence。
    await ctx.connect()
    mixed = MixedRoomAudioInput(ctx.room)
    ctx.add_shutdown_callback(mixed.aclose)
    presence = _Presence(ctx.room)

    while True:
        await presence.present.wait()
        logger.info("房间 %s 有人进来了（%d 人），建立 Gemini 会话", ctx.room.name, _humans(ctx.room))
        session = _build_session(persona)
        # AudioInput 自己是无状态的（io.py:41 只存 label/source），跨会话复用安全。
        session.input.audio = mixed

        # 前端等的就是 `lk.agent.state` 这个属性，所以它每次变化都值得留一行 ——
        # 「前端说没初始化完」和「我们这边压根没报状态」是两回事，没有这行日志
        # 就只能靠猜。
        session.on(
            "agent_state_changed",
            lambda ev: logger.info("lk.agent.state: %s → %s", ev.old_state, ev.new_state),
        )

        await session.start(agent=_build_agent(persona), room=ctx.room, room_options=_ROOM_OPTIONS)
        # 握手成不成，看的是**这一行里有没有 lk.agent.state**，以及房间里有没有
        # 别的 kind=AGENT 参与者在它前面挡着（那个会被前端误认成助手本人）。
        logger.info("会话已起：我=%s ‖ 同房=%s", _who(ctx.room.local_participant), _roster(ctx.room))

        # 这里**不能**用 `session.generate_reply()` 让它先开口打招呼。
        # 3.1 Live 不支持服务端主动触发生成，plugin 会打
        #   "generate_reply is not compatible with 'gemini-3.1-flash-live-preview'"
        # 然后 agent 层再补一条 ERROR "failed to generate a reply"。
        # 不是配置问题，是这个模型当前的能力边界 —— 所以由用户先说话。
        # （`session.say()` 同样不行：Gemini 的 supports_say 是 False。）

        try:
            await presence.wait_until_empty(_IDLE_GRACE_SEC)
        finally:
            await session.aclose()
            await _unpublish_agent_tracks(ctx.room)
            await _clear_agent_state(ctx.room)
        logger.info("房间空满 %.0f 秒，放掉 Gemini 会话，等下一个人进来", _IDLE_GRACE_SEC)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # 起名字 = 关掉自动派发，改由 ensure_rooms.py 用 AgentDispatchService 显式派。
    #
    # 为什么不能用自动派发：自动派发只在**房间被创建的那一刻**触发一次。房间现在
    # 是常驻的，那一刻一辈子只有一次 —— 这个 worker 一重启，所有已存在的房间就
    # 永远没有 agent 了，除非把房间删掉重建（会把里面的人踢下线）。
    # 实测踩过：bunny 的房间因为 /lkon 早就在了，重启后五个新房间都有 agent，
    # 只有 bunny 是个空壳。
    #
    # 显式派发没有这个问题：派发是幂等的、可以随时补，ensure_rooms.py 每 5 分钟
    # 对着「房间里有没有 gemini agent」这个**事实**校一次，缺了就补。
    #
    # 注意前端**不用**改：token 里的 roomConfig.agents 是另一条派发路径，
    # 走 API 派发时前端什么都不用带。
    #
    # drain_timeout 默认 3600 秒 —— 那个默认值假设 job 会自己跑完。我们的 job 是
    # 常驻的 while True，永远不会自己结束，所以收到 SIGTERM 后它会一直挂着，
    # 直到 systemd 的 TimeoutStopSec 到点补一刀 SIGKILL。旧 job 拖着不走的这段
    # 时间里，它还挂在房间的参与者列表里，ensure_rooms.py 会把它误判成
    # 「agent 在岗」而不补派 —— 实测就这么漏了五个房间。给 5 秒，重启干净利落。
    cli.run_app(
        WorkerOptions(entrypoint_fnc=entrypoint, agent_name=_AGENT_NAME, drain_timeout=5)
    )
