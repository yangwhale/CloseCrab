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
import datetime
import logging
import os
import pathlib
import zoneinfo

import aiohttp
from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli, function_tool
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

HKT = zoneinfo.ZoneInfo("Asia/Hong_Kong")

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
你是 Chris 的语音助手，名字叫 bunny。用中文说话，技术名词保留英文原文
（API、token、TPU、LiveKit 这类不要翻译）。

说话方式：
- 短句，一次说清一件事，别念长列表和表格 —— 对方是在「听」不是在「看」。
- 结论先行。先给答案，需要展开再展开。
- 不确定就说不确定，不要编。数字、版本号、出处这类，拿不准就明说。
- 不要念 markdown 符号，不要说「第一点冒号」这种。

工具：
- 要查实时信息、版本号、新闻，就去搜，别凭记忆答。内置的 Google 搜索和
  search_web（Jina）都能用，随便挑一个；第一个没搜到就换另一个再试一次。
- 算数、查本机状态、跑命令用 run_bash —— 心算容易错，能跑就跑。
- 调完工具直接说结论，不要播报「我现在调用某某工具」。
- 工具报错了就如实说哪一步失败了，不要拿记忆里的答案顶上。
"""


@function_tool
async def get_current_time() -> str:
    """获取当前时间（香港时区 HKT）。用户问「现在几点」「今天几号」时调用。"""
    now = datetime.datetime.now(HKT)
    # 这行不是调试残留。工具调用整条链路（schema 下发 → LiveServerToolCall →
    # 框架执行 → send_tool_response）都在 plugin 内部，**默认一个字都不打**，
    # 从外面看「它回答了时间」和「它凭记忆编了个时间」长得一模一样。
    logger.info("tool: get_current_time")
    return now.strftime("%Y-%m-%d %H:%M:%S HKT (%A)")


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
    """用 Jina 搜索引擎联网检索，返回标题和摘要。

    需要最新消息、版本号、实时信息，或者你不确定的事实时调用。
    跟内置的 Google 搜索是两条独立的路，都可以用。

    Args:
        query: 搜索关键词，用最贴近原文的说法，别自己改写成术语。
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
    """抓取一个网页，返回它的正文文本。

    已经知道网址、要看里面具体写了什么时用这个；只是想找网址用 search_web。

    Args:
        url: 完整网址，要带 http:// 或 https://
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
    """在本机执行一条 shell 命令，返回 stdout、stderr 和退出码。

    算数、查文件、看系统状态、跑脚本都用它。工作目录是一个固定的暂存目录。

    Args:
        command: 要执行的 shell 命令
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
    """读取暂存目录里一个文件的内容。

    Args:
        path: 文件名
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
    """把内容写进暂存目录里的一个文件，覆盖已有内容。

    Args:
        path: 文件名
        content: 要写入的完整内容
    """
    logger.info("tool: write_file %s (%d 字)", path, len(content))
    p = _in_scratch(path)
    try:
        p.write_text(content, encoding="utf-8")
        return f"已写入 {p.name}，{len(content.encode())} 字节。"
    except Exception as exc:                      # noqa: BLE001
        return f"写入出错：{exc}"


async def entrypoint(ctx: JobContext) -> None:
    session = AgentSession(
        # 导入路径是 `livekit.plugins.google.realtime`, 不是老文档里那个
        # `google.beta.realtime` —— 后者在 1.8.1 里已经不是真模块了
        # (`hasattr(google.beta, 'realtime')` 为 True 但 import 报 ModuleNotFound)。
        llm=RealtimeModel(
            model="gemini-3.1-flash-live-preview",
            api_key=os.environ["GEMINI_API_KEY"],
            # vertexai 显式写 False：3.1 Live 走 Vertex 会被 plugin 直接拒掉，
            # 留默认值能跑，但写出来的目的是让下一个读代码的人不必去翻源码。
            vertexai=False,
            voice="Aoede",
            temperature=0.8,
            instructions=INSTRUCTIONS,
        ),
    )
    await session.start(
        agent=Agent(
            instructions=INSTRUCTIONS,
            # 最后那个 GoogleSearch() 不是函数工具，是 Gemini 的**内置**工具
            # （provider tool）—— 检索在 Google 服务端完成，不经过这个进程。
            # ⚠️ 内置工具和函数工具**混用**有个硬门槛：plugin 的
            # create_tools_config() 里写着，混用只在 Gemini 3 Developer API 上
            # 成立，Vertex AI 不支持（那边会直接把 provider tool 丢掉并打 warning）。
            # 我们正好是 3.1 Live + Gemini API key，所以能混。
            # 这是「没搬去 Vertex」这个决定顺带换来的能力，别在不知情的情况下
            # 把模型切到 Vertex —— 内置搜索会静默消失。
            tools=[
                get_current_time,
                search_web,
                read_url,
                run_bash,
                read_file,
                write_file,
                GoogleSearch(),
            ],
        ),
        room=ctx.room,
    )
    # 这里**不能**用 `session.generate_reply()` 让它先开口打招呼。
    # 3.1 Live 不支持服务端主动触发生成，plugin 会打
    #   "generate_reply is not compatible with 'gemini-3.1-flash-live-preview'"
    # 然后 agent 层再补一条 ERROR "failed to generate a reply"。
    # 不是配置问题，是这个模型当前的能力边界 —— 所以由用户先说话。
    # （`session.say()` 同样不行：Gemini 的 supports_say 是 False。）


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # 不传 agent_name → 自动派发：房间一建起来 worker 就进去。
    # 传了名字就变成显式派发，前端签 token 时必须带同一个名字，多一处能配错的地方。
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
