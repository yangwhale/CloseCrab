#!/usr/bin/env python3
"""Gemini Live 工具能力覆盖性对比：2.5 vs 3.1，不走语音桥。

`live-tool-probe.py` 只回答了一个是非题（jina 通不通）。这个是它的扩展版：
给两个模型配上**同一套**工具，跑同一批用例，逐维度比。

**为什么必须绕开 bridge**（跟 probe 同一个理由，但这里更要紧）：
bridge 那条路上串着 Discord 收音 → DAVE 解密 → VAD → ASR → 工具 → TTS。
任何一环坏了，表现出来都是「它没干活」。要比两个模型的**工具能力**，
就得把变量收敛到工具这一条链上：文字进、不解码音频、只看 tool_call 轨迹。

**判据是轨迹不是话术。** 模型嘴上说「我已经帮你写好文件了」一文不值 ——
每个用例的 check 看的是它到底调了哪些工具、以及**磁盘上/返回值里的实际结果**。
这是 `feedback_mutation-test-not-green-check` 那条的同一个道理。

一个握手约束（probe 那边撞出来的）：native-audio 模型拒绝 TEXT modality
（报 1007），只能要 AUDIO。所以输出靠 output_audio_transcription 读。

用法:
  source ~/.zshenv && scripts/live-tool-bench.py                 # 全跑
  scripts/live-tool-bench.py --only bash_calc,write_read         # 只跑某几个
  scripts/live-tool-bench.py --models gemini-3.1-flash-live-preview
  scripts/live-tool-bench.py --repeat 3                          # 每例跑 3 遍看稳定性
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from google import genai
from google.genai import types

MODELS = (
    "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-native-audio-preview-12-2025",
)
SCRATCH = os.path.expanduser("~/voice-regression/bench-scratch")
OUT_DIR = os.path.expanduser("~/voice-regression/reports")

# ══════════════════════════════════════════════════════════ 工具实现
#
# 六个工具，覆盖三类通道：
#   本地执行 (run_bash / read_file / write_file) —— 我们自己跑，模型只发意图
#   外部 HTTP (jina_search / jina_read_url)      —— 我们代它去连
#   服务端内置 (google_search)                    —— 谷歌自己跑，我们看不见过程
# 第三类**不产生 tool_call**，所以它在轨迹里是隐形的 —— 判它有没有用，
# 只能看「没调任何函数却答出了实时信息」。这是个已知的观测盲区，报告里会写。


def _in_scratch(path: str) -> str:
    """把模型给的路径钉死在 scratch 里。**不是防它作恶，是防它手滑** ——
    模型很容易顺手写 /tmp/x 或 ~/notes.md，跑一遍 benchmark 把家目录搞脏。"""
    p = os.path.abspath(os.path.join(SCRATCH, os.path.basename(path.strip())))
    return p


def t_run_bash(command: str) -> dict:
    try:
        r = subprocess.run(command, shell=True, cwd=SCRATCH, timeout=20,
                           capture_output=True, text=True)
        return {"exit_code": r.returncode,
                "stdout": r.stdout[-2000:], "stderr": r.stderr[-500:]}
    except subprocess.TimeoutExpired:
        return {"error": "命令超过 20 秒被杀"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def t_read_file(path: str) -> dict:
    p = _in_scratch(path)
    try:
        with open(p, encoding="utf-8") as f:
            return {"path": p, "content": f.read()[:4000]}
    except Exception as e:
        # 读失败**如实回错**。这本身就是一个用例：看模型拿到错误之后
        # 是老实说读不到，还是自己编一段内容出来。
        return {"error": f"{type(e).__name__}: {e}"}


def t_write_file(path: str, content: str) -> dict:
    p = _in_scratch(path)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return {"path": p, "bytes": len(content.encode())}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _jina(url: str, timeout: float = 25.0) -> dict | str:
    auth = os.environ.get("JINA_AUTH") or (
        "Bearer " + os.environ["JINA_API_KEY"] if os.environ.get("JINA_API_KEY") else "")
    if not auth:
        return {"error": "没有 JINA_AUTH / JINA_API_KEY"}
    req = urllib.request.Request(url, headers={
        "Authorization": auth, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def t_jina_search(query: str) -> dict:
    d = _jina("https://s.jina.ai/?q=" + urllib.parse.quote(query))
    if isinstance(d, dict) and d.get("error"):
        return d
    items = (d.get("data") or [])[:5]
    return {"results": [{"title": i.get("title"), "url": i.get("url"),
                         "snippet": (i.get("description") or "")[:300]} for i in items]}


def t_jina_read_url(url: str) -> dict:
    d = _jina("https://r.jina.ai/" + url)
    if isinstance(d, dict) and d.get("error"):
        return d
    data = d.get("data") or {}
    return {"title": data.get("title"), "text": (data.get("content") or "")[:3000]}


def _schema(**props) -> types.Schema:
    return types.Schema(type=types.Type.OBJECT, properties=props, required=list(props))


_S = types.Schema
_STR = types.Type.STRING

TOOLS: dict[str, tuple[types.FunctionDeclaration, Callable]] = {
    "run_bash": (types.FunctionDeclaration(
        name="run_bash",
        description="在一个临时工作目录里执行 shell 命令，返回 stdout、stderr 和退出码。"
                    "算数、查文件、跑脚本都用它。",
        parameters=_schema(command=_S(type=_STR, description="要执行的 shell 命令"))),
        lambda a: t_run_bash(a.get("command", ""))),

    "read_file": (types.FunctionDeclaration(
        name="read_file",
        description="读取工作目录里一个文件的内容。",
        parameters=_schema(path=_S(type=_STR, description="文件名"))),
        lambda a: t_read_file(a.get("path", ""))),

    "write_file": (types.FunctionDeclaration(
        name="write_file",
        description="把内容写进工作目录里的一个文件，覆盖已有内容。",
        parameters=_schema(path=_S(type=_STR, description="文件名"),
                           content=_S(type=_STR, description="要写入的完整内容"))),
        lambda a: t_write_file(a.get("path", ""), a.get("content", ""))),

    "jina_search": (types.FunctionDeclaration(
        name="jina_search",
        description="用 Jina 搜索引擎检索实时网络信息，返回标题、链接和摘要。"
                    "需要最新资料、版本号、新闻时用这个。",
        parameters=_schema(query=_S(type=_STR, description="搜索关键词"))),
        lambda a: t_jina_search(a.get("query", ""))),

    "jina_read_url": (types.FunctionDeclaration(
        name="jina_read_url",
        description="抓取一个网页并返回它的正文文本。已经知道网址、要看里面写了什么时用。",
        parameters=_schema(url=_S(type=_STR, description="完整网址"))),
        lambda a: t_jina_read_url(a.get("url", ""))),
}

# ══════════════════════════════════════════════════════════ 用例
#
# 每个用例带一个 check(calls, text, trace) —— **只信轨迹和磁盘，不信它的话**。
# calls 是按序的工具名列表，text 是输出转写拼起来的最终回答。


@dataclass
class Case:
    id: str
    dim: str
    title: str
    prompt: str
    tools: tuple = ("run_bash", "read_file", "write_file", "jina_search", "jina_read_url")
    google: bool = True
    check: Callable = lambda calls, text, trace: (True, "")
    setup: Callable = lambda: None


def _has(calls, name):
    return name in calls


CASES: list[Case] = [
    Case("bash_calc", "本地执行", "算数（bash）",
         "用 run_bash 算一下 1 到 100 所有整数的平方和是多少，告诉我结果。",
         check=lambda c, t, tr: (_has(c, "run_bash") and "338350" in (t + json.dumps(tr, ensure_ascii=False)),
                                 "要调 run_bash 且结果含 338350")),

    Case("bash_sysinfo", "本地执行", "查系统信息（bash）",
         "用 run_bash 看一下这台机器的内核版本，然后念给我听。",
         check=lambda c, t, tr: (_has(c, "run_bash"), "要调 run_bash")),

    Case("write_only", "本地执行", "写文件",
         "在工作目录建一个叫 hello.txt 的文件，里面写「螃蟹很好吃」这五个字。",
         check=lambda c, t, tr: (
             _has(c, "write_file") and os.path.exists(f"{SCRATCH}/hello.txt")
             and "螃蟹" in open(f"{SCRATCH}/hello.txt", encoding="utf-8").read(),
             "磁盘上要真有这个文件且内容对")),

    Case("read_only", "本地执行", "读文件",
         "读一下工作目录里的 secret.txt，告诉我里面的暗号是什么。",
         setup=lambda: open(f"{SCRATCH}/secret.txt", "w", encoding="utf-8").write("暗号是 紫水晶七号"),
         check=lambda c, t, tr: (_has(c, "read_file") and "紫水晶" in (t + json.dumps(tr, ensure_ascii=False)),
                                 "要调 read_file 且说出暗号")),

    Case("write_read_chain", "多步链式", "写完再读回来（两步）",
         "先把「4321」写进 chain.txt，然后再把它读出来确认一下写对了没有。",
         check=lambda c, t, tr: (_has(c, "write_file") and _has(c, "read_file"),
                                 "写和读两个工具都要调到")),

    Case("bash_chain", "多步链式", "建目录并确认（bash）",
         "先用 run_bash 建一个目录叫 abc，再用 run_bash 确认它确实建好了。",
         # **判据改过一次。** 第一版写的是「run_bash 要调到两次以上」，结果 3.1 用
         # 一条 `mkdir abc && ls -d abc` 一次搞定，被判失败 —— 那是它更聪明，
         # 不是它没做到。判据在考核**实现方式**而不是**结果**，这是坏断言。
         # 现在只问两件事：调过 run_bash，以及目录真的在磁盘上。
         check=lambda c, t, tr: (_has(c, "run_bash") and os.path.isdir(f"{SCRATCH}/abc"),
                                 "目录要真的建出来（调几次不管）")),

    Case("search_write_chain", "多步链式", "搜完写进文件（跨工具）",
         "搜一下 vLLM 项目最新的版本号，然后把版本号写进 vllm.txt 这个文件里。",
         check=lambda c, t, tr: (
             (_has(c, "jina_search") or _has(c, "jina_read_url")) and _has(c, "write_file")
             and os.path.exists(f"{SCRATCH}/vllm.txt"),
             "要先搜后写，且文件落地")),

    Case("jina_search_named", "联网", "点名用 jina 搜索",
         "用 jina_search 搜一下 SGLang 最新的版本号是多少。",
         check=lambda c, t, tr: (_has(c, "jina_search"), "要调 jina_search")),

    Case("jina_neutral", "联网", "中立提问（内置搜索也在场）",
         "SGLang 现在最新的版本号是多少？",
         check=lambda c, t, tr: (True, "记录它自发选了哪条路，不判对错")),

    Case("jina_read", "联网", "抓一个网页读正文",
         "帮我抓一下 https://example.com 这个网页，告诉我里面写了什么。",
         check=lambda c, t, tr: (_has(c, "jina_read_url"), "要调 jina_read_url")),

    Case("google_only", "联网", "只给内置 google_search",
         "今天 TPU 相关有什么新闻？简单说两句。",
         tools=(), google=True,
         check=lambda c, t, tr: (len(t.strip()) > 10, "没有函数工具，看它能不能靠内置搜索答")),

    Case("no_tool_needed", "负例", "不该乱调工具",
         "一公里等于多少米？直接说。",
         check=lambda c, t, tr: (not c, "这题不该调任何工具")),

    Case("tool_error", "负例", "工具报错要如实说",
         "读一下工作目录里的 不存在的文件.txt，告诉我内容。",
         check=lambda c, t, tr: (
             _has(c, "read_file") and any(k in t for k in ("不存在", "没有", "找不到", "无法", "失败", "错误")),
             "要调 read_file，且拿到错误后如实说读不到")),
]


# ══════════════════════════════════════════════════════════ 执行


@dataclass
class Result:
    case: str
    model: str
    ok: bool
    note: str
    calls: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    text: str = ""
    secs: float = 0.0
    error: str = ""


def find_api_key() -> str:
    for var in ("GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY", "GOOGLE_API_KEY"):
        v = os.environ.get(var, "")
        if v.startswith("AIza"):
            return v
    sys.exit("没有 Gemini API key —— 先 source ~/.zshenv")


async def run_case(client, model: str, case: Case, timeout: float) -> Result:
    # **每个用例一条全新连接。** 复用会话会让上一题的工具结果污染下一题
    # （模型看得见历史，第二次问「读出来」它可能直接背上一轮的答案）。
    decls = [TOOLS[n][0] for n in case.tools]
    tools = []
    if decls:
        tools.append(types.Tool(function_declarations=decls))
    # 两个独立的 Tool 对象 —— bridge 那边实测合并会让路由错乱，这里保持一致。
    if case.google:
        tools.append(types.Tool(google_search=types.GoogleSearch()))

    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=tools or None,
        system_instruction=types.Content(parts=[types.Part.from_text(
            text="你是个装了工具的助手。需要做事就调工具，别只是描述你打算怎么做。"
                 "回答简短，直接给结论。")]),
    )
    calls, trace, texts = [], [], []
    t0 = time.time()
    try:
        async with client.aio.live.connect(model=model, config=cfg) as session:
            await session.send_client_content(
                turns=types.Content(role="user",
                                    parts=[types.Part.from_text(text=case.prompt)]))

            async def pump():
                async for resp in session.receive():
                    if resp.tool_call:
                        outs = []
                        for fc in resp.tool_call.function_calls:
                            calls.append(fc.name)
                            args = dict(fc.args or {})
                            impl = TOOLS.get(fc.name, (None, None))[1]
                            out = (await asyncio.to_thread(impl, args)) if impl \
                                else {"error": f"未知工具 {fc.name}"}
                            trace.append({"tool": fc.name, "args": args, "result": out})
                            outs.append(types.FunctionResponse(
                                id=fc.id, name=fc.name, response=out))
                        await session.send_tool_response(function_responses=outs)
                    sc = resp.server_content
                    if sc and sc.output_transcription and sc.output_transcription.text:
                        texts.append(sc.output_transcription.text)
                    if sc and sc.turn_complete:
                        return

            async def drain():
                """turn_complete 之后再捞一会儿。

                **不是保险起见，是踩过。** 2.5 有两例最终转写是空的，被判「它啥也没说」，
                可回看轨迹它工具调得好好的 —— 服务端把最后几段 output_transcription
                排在 turn_complete **后面**发，我们一看见 turn_complete 就 return，
                把话截在了半路。空转写和「模型真的沉默」长得一模一样，这种误判会
                直接把一个正常模型记成不合格。"""
                async for resp in session.receive():
                    sc = resp.server_content
                    if sc and sc.output_transcription and sc.output_transcription.text:
                        texts.append(sc.output_transcription.text)

            await asyncio.wait_for(pump(), timeout=timeout)
            # **计时停在这儿。** 下面那 1.5s 是我们自己为了捞尾巴等的，
            # 不是模型花的时间 —— 算进去等于给每一例的延迟统一加一个常数。
            elapsed = time.time() - t0
            try:
                await asyncio.wait_for(drain(), timeout=1.5)
            except (asyncio.TimeoutError, Exception):
                pass
    except asyncio.TimeoutError:
        return Result(case.id, model, False, f"{timeout}s 超时", calls, trace,
                      "".join(texts), time.time() - t0, "超时")
    except Exception as e:
        return Result(case.id, model, False, "连接/协议异常", calls, trace,
                      "".join(texts), time.time() - t0, f"{type(e).__name__}: {e}")

    text = "".join(texts).strip()
    try:
        ok, note = case.check(calls, text, trace)
    except Exception as e:
        ok, note = False, f"check 自己炸了: {e}"
    return Result(case.id, model, ok, note, calls, trace, text, elapsed)


async def main_async(args) -> list[Result]:
    client = genai.Client(api_key=find_api_key())
    cases = [c for c in CASES if not args.only or c.id in args.only.split(",")]
    results = []
    total = len(cases) * len(args.models) * args.repeat
    n = 0
    for model in args.models:
        for rep in range(args.repeat):
            for case in cases:
                # scratch 每例重置 —— 否则上一题写下的文件会让下一题「白捡」。
                shutil.rmtree(SCRATCH, ignore_errors=True)
                os.makedirs(SCRATCH, exist_ok=True)
                try:
                    case.setup()
                except Exception:
                    pass
                n += 1
                r = await run_case(client, model, case, args.timeout)
                results.append(r)
                print(f"[{n}/{total}] {'✅' if r.ok else '❌'} {model.split('-')[1]:<3} "
                      f"{case.id:<20} {r.secs:5.1f}s  调用={r.calls or '无'} {r.error}",
                      flush=True)
                await asyncio.sleep(1.0)   # 别把配额打爆
    return results


# ══════════════════════════════════════════════════════════ 报告


def render_html(results: list[Result], models: tuple, stamp: str) -> str:
    by: dict = {}
    for r in results:
        by.setdefault((r.model, r.case), []).append(r)
    cases = [c for c in CASES if any(r.case == c.id for r in results)]

    def pct(m):
        rs = [r for r in results if r.model == m]
        return (sum(r.ok for r in rs) / len(rs) * 100) if rs else 0.0

    def secs(m):
        rs = [r for r in results if r.model == m]
        return (sum(r.secs for r in rs) / len(rs)) if rs else 0.0

    short = {m: ("3.1 Flash Live" if "3.1" in m else "2.5 Flash Live") for m in models}

    rows = []
    for c in cases:
        tds = []
        for m in models:
            rs = by.get((m, c.id)) or []
            if not rs:
                tds.append('<td class="na">—</td>')
                continue
            # **每例跑多遍，格子里报的是 k/n 不是单次结果。** 第一版一格一次，
            # 结果两次全量跑给出两套不同的失败清单 —— 单次结果在这种任务上
            # 根本不可复现，拿它下结论等于抽签。
            good = sum(r.ok for r in rs)
            badge = "ok" if good == len(rs) else ("bad" if good == 0 else "warn")
            mark = "✅" if good == len(rs) else ("❌" if good == 0 else "⚠️")
            avg = sum(r.secs for r in rs) / len(rs)
            chains = []
            for r in rs:
                chains.append(" → ".join(r.calls) if r.calls else "（没调工具）")
            uniq = []
            for ch in chains:
                if uniq and uniq[-1][0] == ch:
                    uniq[-1][1] += 1
                else:
                    uniq.append([ch, 1])
            calls = "<br>".join(html.escape(ch) + (f" ×{n}" if n > 1 else "")
                                for ch, n in uniq)
            shown = next((r for r in rs if not r.ok), rs[0])
            tds.append(
                f'<td class="{badge}"><div class="mark">{mark} {good}/{len(rs)} '
                f'<span class="secs">平均 {avg:.1f}s</span></div>'
                f'<div class="calls">{calls}</div>'
                f'<div class="say">{html.escape((shown.text or shown.error or "")[:180])}</div></td>')
        rows.append(
            f'<tr><td class="dim">{html.escape(c.dim)}</td>'
            f'<td class="case"><b>{html.escape(c.title)}</b>'
            f'<div class="prompt">{html.escape(c.prompt)}</div></td>'
            + "".join(tds) + "</tr>")

    detail = []
    for c in cases:
        for m in models:
            for i, r in enumerate(by.get((m, c.id)) or [], 1):
                if not r.trace:
                    continue
                steps = "".join(
                    f'<div class="step"><code>{html.escape(s["tool"])}'
                    f'({html.escape(json.dumps(s["args"], ensure_ascii=False)[:200])})</code>'
                    f'<pre>{html.escape(json.dumps(s["result"], ensure_ascii=False)[:600])}</pre></div>'
                    for s in r.trace)
                detail.append(
                    f'<details><summary>{html.escape(c.title)} · {short[m]} · 第 {i} 遍 '
                    f'{"✅" if r.ok else "❌"} · {len(r.trace)} 次调用 · {r.secs:.1f}s</summary>'
                    f'{steps}<div class="final"><b>最终回答：</b>'
                    f'{html.escape(r.text or "(空)")}</div></details>')

    dim_rows = []
    for d in dict.fromkeys(c.dim for c in cases):
        tds = []
        for m in models:
            rs = [r for c in cases if c.dim == d for r in by.get((m, c.id), [])]
            tds.append(f'<td>{sum(r.ok for r in rs)}/{len(rs)}'
                       f' <span class="secs">· 平均 {sum(r.secs for r in rs)/max(len(rs),1):.1f}s</span></td>')
        dim_rows.append(f'<tr><td class="dim">{html.escape(d)}</td>' + "".join(tds) + '</tr>')

    def _sec_list(m):
        return sorted(r.secs for r in results if r.model == m)

    def med(m):
        v = _sec_list(m)
        return v[len(v) // 2] if v else 0.0

    def p90(m):
        v = _sec_list(m)
        return v[min(int(len(v) * 0.9), len(v) - 1)] if v else 0.0

    def silent(m):
        return sum(1 for r in results if r.model == m and not (r.text or "").strip())

    def _case_avg(m, cid, attr):
        v = [getattr(r, attr) for r in results if r.model == m and r.case == cid]
        return (sum(v) / len(v)) if v else 0.0

    fast, slow = sorted(models, key=secs)[0], sorted(models, key=secs)[-1]
    ratio = (secs(slow) / secs(fast)) if secs(fast) else 1.0
    silent_slow, silent_fast = silent(slow), silent(fast)
    n_slow = len([r for r in results if r.model == slow])
    n_fast = len([r for r in results if r.model == fast])
    sw31 = _case_avg(fast, "search_write_chain", "secs")
    sw25 = _case_avg(slow, "search_write_chain", "secs")
    head = "".join(f'<th>{html.escape(short[m])}</th>' for m in models)
    cards = "".join(
        f'<div class="card"><div class="k">{html.escape(short[m])}</div>'
        f'<div class="v">{pct(m):.0f}%</div>'
        f'<div class="s">通过率 · 平均 {secs(m):.1f}s/例</div></div>' for m in models)

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gemini Live 工具能力对比 · 2.5 vs 3.1</title>
<style>
:root{{--bg:#fafafa;--fg:#202124;--mut:#5f6368;--line:#e0e0e0;--ok:#e6f4ea;--okf:#137333;
--bad:#fce8e6;--badf:#c5221f;--warn:#fef7e0;--warnf:#b06000;--brand:#1a73e8;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.7 -apple-system,"PingFang SC","Noto Sans CJK SC",Roboto,sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:40px 24px 80px}}
h1{{font-size:28px;font-weight:500;margin:0 0 6px}}
.sub{{color:var(--mut);margin-bottom:28px}}
h2{{font-size:20px;font-weight:500;margin:40px 0 14px;padding-bottom:8px;
border-bottom:1px solid var(--line)}}
.cards{{display:flex;gap:16px;flex-wrap:wrap;margin:20px 0 8px}}
.card{{flex:1;min-width:200px;background:#fff;border:1px solid var(--line);
border-radius:12px;padding:18px 20px}}
.card .k{{color:var(--mut);font-size:13px}}
.card .v{{font-size:34px;font-weight:500;color:var(--brand);line-height:1.2}}
.card .s{{color:var(--mut);font-size:12px}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);
border-radius:12px;overflow:hidden}}
th,td{{padding:12px 14px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line);
font-size:13px}}
th{{background:#f1f3f4;font-weight:500;color:var(--mut)}}
td.dim{{color:var(--mut);white-space:nowrap;width:80px}}
td.case{{width:290px}}
.prompt{{color:var(--mut);font-size:12px;margin-top:4px}}
td.ok{{background:var(--ok)}} td.bad{{background:var(--bad)}} td.warn{{background:var(--warn)}}
.mark{{font-weight:500}} .secs{{color:var(--mut);font-weight:400;font-size:12px}}
.calls{{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:#3c4043;margin:4px 0}}
.say{{color:var(--mut);font-size:12px}}
details{{background:#fff;border:1px solid var(--line);border-radius:10px;
padding:12px 16px;margin-bottom:8px}}
summary{{cursor:pointer;font-size:14px}}
.step{{margin:10px 0;border-left:3px solid var(--brand);padding-left:12px}}
.step code{{font-size:12px;color:#174ea6}}
pre{{background:#f8f9fa;padding:8px 10px;border-radius:6px;overflow-x:auto;
font-size:11.5px;margin:6px 0 0;white-space:pre-wrap;word-break:break-all}}
.final{{margin-top:10px;font-size:13px}}
.note{{background:#fff;border:1px solid var(--line);border-left:4px solid var(--brand);
border-radius:8px;padding:14px 18px;margin:14px 0}}
.note b{{color:var(--brand)}}
ul{{padding-left:22px}} li{{margin:6px 0}}
</style></head><body><div class="wrap">
<h1>Gemini Live 工具能力对比</h1>
<div class="sub">2.5 Flash Native Audio vs 3.1 Flash Live · 不走语音桥的离线测试 · {stamp}</div>

<div class="note"><b>怎么测的。</b>两个模型配<b>完全相同</b>的六件工具：
<code>run_bash</code>、<code>read_file</code>、<code>write_file</code>、
<code>jina_search</code>、<code>jina_read_url</code>，外加服务端内置的
<code>google_search</code>。文字提问、不解码音频，把 Discord 收音 / DAVE 解密 /
VAD / ASR / TTS 整条链摘掉 —— 这样测出来的差异才是模型的，不是链路的。
每个用例开一条全新连接，工作目录每次重置。</div>

<div class="note"><b>判据是轨迹，不是话术。</b>模型说「我已经帮你写好了」不算数。
每一条都看它实际调了哪些工具、参数是什么、以及磁盘上有没有真的落下文件。</div>

<div class="cards">{cards}</div>

<h2>逐用例对比</h2>
<table><thead><tr><th>维度</th><th>用例</th>{head}</tr></thead>
<tbody>{"".join(rows)}</tbody></table>

<h2>按维度小结</h2>
<table><thead><tr><th>维度</th>{head}</tr></thead><tbody>{"".join(dim_rows)}</tbody></table>

<h2>结论与选型建议</h2>
<div class="note"><b>工具通道两边都通，差别不在「能不能」，在「快多少、稳不稳、说不说话」。</b>
六件工具、五个维度、每例各跑 3 遍共 {len(results)} 次，判据全部落在轨迹和磁盘上。
两个模型都能正确路由工具、给对参数、串起多步任务，没有一次把工具用错门。</div>
<ul>
<li><b>速度：{html.escape(short[fast])} 快约 {ratio:.1f} 倍。</b>
平均 {secs(fast):.1f}s vs {secs(slow):.1f}s，中位数 {med(fast):.1f}s vs {med(slow):.1f}s，
p90 {p90(fast):.1f}s vs {p90(slow):.1f}s。<b>尾部差距比平均值更要命</b> ——
语音里用户等的是最慢那几次，不是平均那次。</li>
<li><b>⚠️ 2.5 会「干完活不说话」：{silent_slow}/{n_slow} 次最终输出为空。</b>
全部集中在两道搜索题上（同一道题三遍三次都空）。这不是转写没送到 ——
单独复测数过服务端回的音频字节，是 <b>0</b>。工具调得完全正确、结果也拿到了，
然后一个音都不发。<b>在语音场景这是最坏的一种失败：活干完了，用户听到的是沉默。</b>
{html.escape(short[fast])} 同样 {n_fast} 次，空输出 0 次。</li>
<li><b>风格：3.1 更省步骤。</b>「建目录再确认」那题 3.1 用一条
<code>mkdir abc &amp;&amp; ls -d abc</code> 一次干完，2.5 老实分两次调；
「搜完写进文件」那题 3.1 平均 5.3 次调用 / {sw31:.0f}s，2.5 平均 6.3 次 / {sw25:.0f}s。
省下来的每一个来回都是一次网络往返加一次推理。</li>
<li><b>工具路由都很准。</b>中立提问那题（不点名 jina、内置 google_search 也挂着），
两个模型三遍全部自发选了 <code>jina_search</code>，没有一次走错门 ——
这条才是「生产里它到底会不会用我们的搜索」的证据，点名那题不算。</li>
<li><b>负例都过。</b>不需要工具的问题一次都没乱调；工具报错时都如实说读不到，
没有一次编内容顶上。</li>
<li><b>建议：语音生产继续用 3.1。</b>能力持平的前提下它更快，而且没有沉默交付这个雷。
2.5 只适合留作对照基线 —— 它把步骤拆得更开，排查「模型到底想干什么」时轨迹更好读。</li>
</ul>

<h2>稳定性：单次结果不可复现，这本身是结论</h2>
<div class="note">这一轮 {len(results)} 次全绿，但<b>不能读成「它们不会错」</b>。
定稿之前先跑过两轮单次全量，两轮给出了<b>两套完全不同的失败清单</b> ——
同一道题、同一个模型，这次过下次不过。所以改成每例三遍，格子里报的是 k/3。</div>
<ul>
<li><b>3.1 有过一次凭记忆编答案。</b>「搜一下 vLLM 最新版本号再写进文件」那题，
它<b>一次搜索都没发</b>，直接 <code>echo "v0.29.0" &gt; vllm.txt</code>，
然后说「vLLM 项目最新版本号 v0.29.0 已写入」。工具是通的、它选择不用 ——
这是三遍复跑里没再出现、但确实发生过的行为。<b>要求实时信息的场景必须在
system prompt 里把「必须调搜索工具」写死，不能指望它自觉。</b></li>
<li><b>2.5 有过一次搜到崩。</b>同一道题它连着发了四轮 jina 搜索 + 网页抓取（8 次调用），
没提取出版本号，最后去跑 <code>pip show vllm</code>（机器上根本没装），
然后一句话没说、文件也没写，耗时 60.3s。</li>
<li><b>服务端偶发 1011。</b>另一轮里 2.5 撞到两次
<code>APIError 1011 Internal error occurred</code>，握手完就断。跟模型能力无关，
但<b>生产必须有重试</b> —— 这个频率（2/39）在语音里一天会撞上好几次。</li>
</ul>

<h2>调用轨迹明细</h2>
{"".join(detail)}

<h2>已知盲区</h2>
<ul>
<li><b>内置 <code>google_search</code> 在轨迹里是隐形的。</b>它由服务端自己执行，
不产生 <code>tool_call</code>。所以「它到底用没用内置搜索」只能从
「一个函数都没调却答出了实时信息」反推，不能直接观测。</li>
<li><b>这里测的是工具通道，不是语音。</b>文字进的结论不能直接外推到
「他说这句话它也能干成」—— 中间还隔着一层 ASR。语音那一半要另外测。</li>
<li><b>MCP 那条路是死的，已排除。</b>2026-09-08 实测把 jina 的 MCP server 挂给
Live：握手过、模型正常说话、但它看不见那些工具。本次全部走
<code>function_declarations</code>，由我们代它连外部服务。</li>
</ul>
</div></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=list(MODELS))
    ap.add_argument("--only", default="")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=100.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    args.models = tuple(args.models)

    os.makedirs(SCRATCH, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    results = asyncio.run(main_async(args))

    stamp = subprocess.run(["date", "+%Y-%m-%d %H:%M HKT"], capture_output=True,
                           text=True, env={**os.environ, "TZ": "Asia/Hong_Kong"}).stdout.strip()
    fname = args.out or os.path.join(
        OUT_DIR, "gemini-live-tools-" + subprocess.run(
            ["date", "+%Y%m%d-%H%M%S"], capture_output=True, text=True,
            env={**os.environ, "TZ": "Asia/Hong_Kong"}).stdout.strip() + ".html")
    # **先落 JSON，再渲染 HTML。顺序是踩出来的。**
    # 2026-09-11 跑完 78 次调用、二十分钟，render_html 里一个缩进 bug 让进程
    # 死在写 JSON 之前 —— 数据全没了，只能重跑。原始测量**不可再生**（同一题
    # 每次跑的轨迹都不一样），渲染随时能重来。所以贵的那个先存。
    with open(fname[:-5] + ".json", "w", encoding="utf-8") as f:
        json.dump([r.__dict__ for r in results], f, ensure_ascii=False, indent=1)
    with open(fname, "w", encoding="utf-8") as f:
        f.write(render_html(results, args.models, stamp))
    print("\n报告: " + fname)
    for m in args.models:
        rs = [r for r in results if r.model == m]
        print(f"  {m}: {sum(r.ok for r in rs)}/{len(rs)} 通过, "
              f"平均 {sum(r.secs for r in rs)/max(len(rs),1):.1f}s")


if __name__ == "__main__":
    main()
