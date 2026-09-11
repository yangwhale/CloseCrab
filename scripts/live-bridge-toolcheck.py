#!/usr/bin/env python3
"""语音桥的工具调用体检：跑**生产同款配置**，但不经过 Discord。

跟 `live-tool-bench.py` 的分工：那个是「两个模型的工具能力谁强」的横向对比，
用的是一套为对比而造的假工具。这个反过来 —— 模型、人格、工具、VAD、思考档位
**全部取生产运行时那一份**，只问一件事：**bunny 现在这套配置，工具调得对不对。**

## 为什么不从 Discord 说话去测

说话测只能测到「这一次」。同一句话说三遍，模型可能派、可能不派 —— 单次结果
不可复现（对比台那次的教训：13 例跑单遍得出的结论，跑到第三遍全变了）。
而且中间串着收音、DAVE 解密、VAD、ASR，任何一环坏了表现出来都是「它没干活」，
根本分不清是听错了还是判断错了。这里文字进、直连 Live API，变量收敛到
**「听懂之后它怎么决策」**这一条链上。

（ASR 那一半是另一份测试的事：把录好的 wav 回放进去，看 input_transcription。
 两半合起来才是完整的语音链路体检。）

## 配置零漂移

配置不是在这里重写一遍，而是 `object.__new__(GeminiLiveBridge)` 之后直接调它的
`_build_config()`。这不是偷懒 —— **手抄一份配置的测试，测的是那份手抄稿**。
生产那边哪天改了 thinking 档、换了模型、动了工具声明，这里会自动跟着变；
抄一份的话它会一直绿着，然后告诉你一个三个月前的结论。

## ⚠️ ask_<bot> 一律拦下来不真派

生产里这个工具会 spawn `inbox-send.py` 把活派给 bot 本体。这里**绝对不能真派** ——
本机跑的就是 bunny，30 多次调用等于给自己塞 30 个真任务，还会在语音频道里
播 30 段结论。所以工具在这里是桩：**参数照单全收记下来**（判分就靠它），
返回值跟 `_ask_owner` 成功时**一字不差**（模型看到的东西必须和生产一致，
否则它后半轮的表现就不是生产里的表现了）。

用法:
  source ~/.zshenv && scripts/live-bridge-toolcheck.py
  scripts/live-bridge-toolcheck.py --only 点名转达,闲聊不该派 --repeat 1
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Callable

sys.path.insert(0, os.path.expanduser("~/CloseCrab"))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from closecrab.voice import gemini_live_bridge as glb  # noqa: E402

BOT = glb.BOT_NAME
TOOL = glb._ASK_OWNER_TOOL          # ask_bunny
OUT_DIR = os.path.expanduser("~/voice-regression/reports")


# ══════════════════════════════════════════════════════════ 用例
#
# 判据的两条原则，都是对比台那次踩出来的：
#
#  1. **只看结果不看方式。** 上次有个 check 数「调了几次 run_bash」，
#     结果把两步并成 `mkdir x && ls x` 的模型判成不合格 —— 它做对了事，
#     只是没按我想的方式做。这里同理：不要求「必须只调一次」。
#  2. **负例和正例一样重要。** 一个见什么都派的模型能把所有正例考满分。
#     不该派的时候不派，是这套东西能用的前提。


@dataclass
class Case:
    id: str
    dim: str                 # 维度，报告里分组用
    prompt: str
    want: str                # 人话写的期望，报告里直接展示
    check: Callable          # (calls, tasks, text) -> (ok, note)


def _dispatched(calls):
    return [c for c in calls if c == TOOL]


def _joined(tasks):
    return " ".join(tasks)


CASES: list[Case] = [
    # ── 维度一：该派的必须派出去 ────────────────────────────────
    Case(
        "普通任务", "该派",
        "帮我看一下语音桥今天的日志里有没有报错。",
        f"要发出 {TOOL}",
        lambda calls, tasks, text: (
            bool(_dispatched(calls)),
            "派了" if _dispatched(calls) else "❌ 一个工具都没调，用户干等",
        ),
    ),
    Case(
        "点名转达", "该派",
        "让巴尼继续。",
        "点名 + 内容只有「继续」，也必须原样转过去",
        lambda calls, tasks, text: (
            bool(_dispatched(calls)) and "继续" in _joined(tasks),
            f"task={_joined(tasks)[:80]!r}" if _dispatched(calls)
            else "❌ 点了名却没派 —— 它替 bot 答了",
        ),
    ),
    Case(
        "细节保真", "该派",
        "让巴尼去看一下 /tmp/voice-42.log 第 137 行那个 NoDecryptorForUser 是怎么回事。",
        "路径、行号、错误名三个都要一字不差地进 task",
        lambda calls, tasks, text: (
            all(k in _joined(tasks)
                for k in ("/tmp/voice-42.log", "137", "NoDecryptorForUser")),
            "；".join(
                f"缺 {k}" for k in ("/tmp/voice-42.log", "137", "NoDecryptorForUser")
                if k not in _joined(tasks)
            ) or "细节全带上了",
        ),
    ),
    Case(
        "两件事", "该派",
        "让巴尼把语音日志翻一遍，另外再看看那台 GPU 机器还活着没有。",
        "两件事都要进 task（一次调用带两件、或者调两次都算对）",
        lambda calls, tasks, text: (
            bool(_dispatched(calls))
            and any(k in _joined(tasks) for k in ("日志", "log"))
            and any(k in _joined(tasks) for k in ("GPU", "gpu", "机器")),
            f"{len(_dispatched(calls))} 次调用，task={_joined(tasks)[:100]!r}",
        ),
    ),
    # 这条的判据**第一版写错了，改过**，过程本身值得记：
    # 原来写的是「必须派活」，跑出来 0/3 —— 可回看转写，模型把「提屁油/居屁油」
    # 正确还原成了 TPU/GPU 并且答得挺好。**它做对了事，只是没按我设想的方式做。**
    # 这正是对比台那次 `bash_chain` 的同一个错误（数调用次数 = 奖励更啰嗦的模型），
    # 我在这个文件顶上刚写完这条原则，第一次跑就自己踩了。
    # 现在只判客观的那一半：**听岔的词有没有被还原**。派不派是判断题不是对错题，
    # 记在 note 里交给人看。
    Case(
        "听岔了", "听力",
        "帮我查一下提屁油和居屁油到底有啥区别。",
        "ASR 明显听岔了（TPU / GPU）。不许把这两个词当真词往下走 —— "
        "要么还原、要么明说自己没听准，最差的是拿着乱码去派活",
        lambda calls, tasks, text: (
            ("TPU" in text.upper() and "GPU" in text.upper())
            or any(k in text for k in ("没听清", "没听准", "是不是说")),
            # 三岔，别写成两岔：第一版 else 里写死「承认没听清」，
            # 结果两条都没中的那次也印成「承认没听清」，读报告的人会以为它认了。
            ("还原成 TPU/GPU 了" if "TPU" in text.upper()
             else "承认没听清" if any(k in text for k in ("没听清", "没听准", "是不是说"))
             else "❌ 答得对但嘴上照念「提屁油/居屁油」")
            + ("；顺手派了活" if _dispatched(calls) else "；自己答的没派"),
        ),
    ),

    # ── 维度二：不该派的一次都不能派（负例）─────────────────────
    Case(
        "闲聊不该派", "不该派",
        "你今天听起来精神不错啊。",
        f"纯闲聊，不许发 {TOOL}",
        lambda calls, tasks, text: (
            not _dispatched(calls),
            "✅ 没乱派" if not _dispatched(calls)
            else f"❌ 闲聊也派：{_joined(tasks)[:80]!r}",
        ),
    ),
    Case(
        "明确不许派", "不该派",
        "别派给巴尼，你自己用一句话说说什么是 GPU。",
        "用户明确说了别派 —— 工具描述里写着「默认就派」，"
        "这条测的是明示指令能不能压过默认动作",
        lambda calls, tasks, text: (
            not _dispatched(calls) and bool(text.strip()),
            "✅ 听话，自己答了" if not _dispatched(calls)
            else "❌ 用户说别派它还是派了",
        ),
    ),
    Case(
        "语气词", "不该派",
        "嗯……那个……",
        "半句话，什么都没说清。不许凭空派一个活出去",
        lambda calls, tasks, text: (
            not _dispatched(calls),
            "✅ 没瞎派" if not _dispatched(calls)
            else f"❌ 拿半句话派了活：{_joined(tasks)[:80]!r}",
        ),
    ),

    # ── 维度三：派完之后的嘴上功夫 ──────────────────────────────
    Case(
        "派完要出声", "派完",
        "让巴尼把那个 SSRC 轮换的补丁验证一下。",
        "调完工具**这一轮必须开口**。沉默在语音里跟死机没有区别 —— "
        "这正是 ⚠️ [干完活没出声] 那道检测器盯的东西",
        lambda calls, tasks, text: (
            bool(_dispatched(calls)) and bool(text.strip()),
            "派了也说了" if (_dispatched(calls) and text.strip())
            else ("❌ 派了但一声不吭" if _dispatched(calls) else "❌ 没派"),
        ),
    ),
    Case(
        "不许替它转述", "派完",
        "问一下巴尼，那个 96k 码率的改动到底生效了没有。",
        "派完只说「已经交给它了」。**不许承诺自己去查了再回来告诉用户** —— "
        "工具是发出去就返回的，它根本拿不到结果，承诺了就是空头支票",
        lambda calls, tasks, text: (
            bool(_dispatched(calls))
            and not any(k in text for k in ("我去查", "我查完", "稍等我", "我看看结果",
                                            "我马上告诉你", "我查一下再")),
            f"回话={text[:100]!r}",
        ),
    ),
]


# ══════════════════════════════════════════════════════════ 执行


@dataclass
class Run:
    case: str
    dim: str
    rep: int
    ok: bool
    note: str
    calls: list = field(default_factory=list)
    tasks: list = field(default_factory=list)
    text: str = ""
    secs: float = 0.0
    error: str = ""
    promised_not_sent: bool = False   # 「只说没派」—— 直接用生产那条正则判
    silent_after_tool: bool = False   # 「干完活没出声」


def find_api_key() -> str:
    for var in ("GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY", "GOOGLE_API_KEY"):
        v = os.environ.get(var, "")
        if v.startswith("AIza"):
            return v
    sys.exit("没有 Gemini API key —— 先 source ~/.zshenv")


def build_production_config() -> types.LiveConnectConfig:
    """拿生产那份配置本体，不是照着抄一份。见文件头「配置零漂移」。"""
    bridge = object.__new__(glb.GeminiLiveBridge)
    bridge._resume_handle = None       # 全新会话，不续命
    return bridge._build_config()


# 桩返回值：跟 `_ask_owner` 成功那条**必须一字不差**（见文件头的 ⚠️）。
_STUB_RESULT = {
    "status": "已转交",
    "note": (
        f"已经交给 {BOT} 了。它干完会自己在这个语音频道里说结果，"
        f"你不用等、也不用替它转述。跟用户说一句就继续聊别的。"
    ),
}


async def run_case(client, cfg, case: Case, rep: int, timeout: float) -> Run:
    # **一例一条新连接。** 复用会话上一题的派活记录会污染下一题 ——
    # 模型看得见历史，第二次就可能说「这个我刚才已经派过了」而不再调工具。
    calls, tasks, texts = [], [], []
    t0 = time.time()
    try:
        async with client.aio.live.connect(
            model=glb.current_model(), config=cfg
        ) as session:
            await session.send_client_content(
                turns=types.Content(
                    role="user", parts=[types.Part.from_text(text=case.prompt)]
                )
            )

            async def pump():
                async for resp in session.receive():
                    if resp.tool_call:
                        outs = []
                        for fc in resp.tool_call.function_calls:
                            calls.append(fc.name)
                            args = dict(fc.args or {})
                            if fc.name == TOOL:
                                tasks.append(str(args.get("task", "")))
                                result = _STUB_RESULT
                            else:
                                result = {"error": f"未知工具 {fc.name}"}
                            outs.append(types.FunctionResponse(
                                id=fc.id, name=fc.name, response=result))
                        await session.send_tool_response(function_responses=outs)
                    sc = resp.server_content
                    if sc and sc.output_transcription and sc.output_transcription.text:
                        texts.append(sc.output_transcription.text)
                    if sc and sc.turn_complete:
                        return

            async def drain():
                """turn_complete 之后再捞 1.5 秒。

                **踩过才加的**：服务端会把最后几段 output_transcription 排在
                turn_complete 后面发，一看见 turn_complete 就收工会把话截在半路，
                空转写和「模型真的沉默」长得一模一样 —— 这个误判会把正常轮次
                记成「干完活没出声」，正好污染这份报告最关键的那一列。"""
                async for resp in session.receive():
                    sc = resp.server_content
                    if sc and sc.output_transcription and sc.output_transcription.text:
                        texts.append(sc.output_transcription.text)

            await asyncio.wait_for(pump(), timeout=timeout)
            # 计时停在这儿：下面 1.5 秒是我们自己等的，不是模型花的。
            elapsed = time.time() - t0
            try:
                await asyncio.wait_for(drain(), timeout=1.5)
            except Exception:
                pass
    except asyncio.TimeoutError:
        return Run(case.id, case.dim, rep, False, f"{timeout}s 超时", calls, tasks,
                   "".join(texts), time.time() - t0, "超时")
    except Exception as e:
        return Run(case.id, case.dim, rep, False, "连接/协议异常", calls, tasks,
                   "".join(texts), time.time() - t0, f"{type(e).__name__}: {e}")

    # **必须过一遍生产的 `_tidy`。** 服务端的转写是按词切的，中文里会夹一堆空格
    # （「我让 巴尼 去查 一下」）。`_recv_loop` 在判那两道检测器之前先 _tidy 抹掉了
    # 它们，这里不抹，判据就比生产**更弱** —— 第一次跑就漏报了一次真实的「只说没派」。
    # 想复用生产判据就得连它的前处理一起复用，只 import 那条正则不算复用。
    text = glb._tidy("".join(texts))
    try:
        ok, note = case.check(calls, tasks, text)
    except Exception as e:
        ok, note = False, f"check 自己炸了: {e}"

    # 两道生产检测器在这里**复用生产的判据本体**，不重写一遍条件。
    # （抄一遍的测试只能证明「我抄对了我自己」—— test_ssrc_rotation.py 第一版
    #  就是那么写的，永远绿。）
    promised = glb._promised_dispatch(text) and not _dispatched(calls)
    silent = bool(_dispatched(calls)) and not text

    return Run(case.id, case.dim, rep, ok, note, calls, tasks, text, elapsed,
               "", promised, silent)


async def main_async(args) -> list[Run]:
    client = genai.Client(api_key=find_api_key())
    cases = [c for c in CASES if not args.only or c.id in args.only.split(",")]
    cfg = build_production_config()
    runs: list[Run] = []
    total = len(cases) * args.repeat
    n = 0
    for rep in range(1, args.repeat + 1):
        for case in cases:
            n += 1
            r = await run_case(client, cfg, case, rep, args.timeout)
            runs.append(r)
            flags = ("" if not r.promised_not_sent else " ⚠️只说没派") + \
                    ("" if not r.silent_after_tool else " ⚠️干完活没出声")
            print(f"[{n}/{total}] {'✅' if r.ok else '❌'} {case.id:<12} "
                  f"rep{rep} {r.secs:5.1f}s 调用={len(_dispatched(r.calls))}"
                  f"{flags} {r.error} — {r.note[:70]}", flush=True)
            await asyncio.sleep(1.0)
    return runs


# ══════════════════════════════════════════════════════════ 报告


def _esc(s):
    return html.escape(str(s or ""))


def render_html(runs: list[Run], cases: list[Case], stamp: str, meta: dict) -> str:
    by_case: dict[str, list[Run]] = {}
    for r in runs:
        by_case.setdefault(r.case, []).append(r)

    rows = []
    for c in cases:
        rs = by_case.get(c.id, [])
        if not rs:
            continue
        k, n = sum(1 for r in rs if r.ok), len(rs)
        color = "#1b873f" if k == n else ("#b35c00" if k else "#c0392b")
        rows.append(f"""
        <tr>
          <td><span class="dim">{_esc(c.dim)}</span><br><b>{_esc(c.id)}</b></td>
          <td class="q">{_esc(c.prompt)}</td>
          <td class="want">{_esc(c.want)}</td>
          <td style="text-align:center;color:{color};font-weight:700;font-size:16px">
            {k}/{n}</td>
          <td class="notes">{"<br>".join(
              f'<span style="color:{"#1b873f" if r.ok else "#c0392b"}">'
              f'{"✅" if r.ok else "❌"}</span> {_esc(r.note)}' for r in rs)}</td>
        </tr>""")

    detail = []
    for c in cases:
        for r in by_case.get(c.id, []):
            detail.append(f"""
        <div class="run">
          <div class="rh">{"✅" if r.ok else "❌"} {_esc(r.case)} · 第 {r.rep} 遍 ·
               {r.secs:.1f}s{" · ⚠️ 只说没派" if r.promised_not_sent else ""}
               {" · ⚠️ 干完活没出声" if r.silent_after_tool else ""}</div>
          <div class="kv"><span>调用</span><code>{_esc(r.calls or "无")}</code></div>
          {"".join(f'<div class="kv"><span>task</span><code>{_esc(t)}</code></div>'
                   for t in r.tasks)}
          <div class="kv"><span>它说了啥</span><q>{_esc(r.text) or
               "<i style=color:#c0392b>（一个字都没有）</i>"}</q></div>
          {f'<div class="kv"><span>错误</span><code>{_esc(r.error)}</code></div>'
           if r.error else ""}
        </div>""")

    total = len(runs)
    passed = sum(1 for r in runs if r.ok)
    promised = sum(1 for r in runs if r.promised_not_sent)
    silent = sum(1 for r in runs if r.silent_after_tool)

    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>语音桥工具调用体检 · {stamp}</title>
<link rel="icon" href="data:image/svg+xml,{'%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22%3E%3Ctext y=%22.9em%22 font-size=%2290%22%3E%F0%9F%A6%80%3C/text%3E%3C/svg%3E'}">
<style>
 :root{{--ink:#1a1a1a;--mut:#5f6368;--line:#e0e0e0;--bg:#fafafa}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--bg);color:var(--ink);
   font:15px/1.7 -apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif}}
 .wrap{{max-width:1180px;margin:0 auto;padding:36px 22px 80px}}
 h1{{font-size:26px;margin:0 0 6px;font-weight:600}}
 .sub{{color:var(--mut);font-size:13px;margin-bottom:26px}}
 .cards{{display:flex;gap:14px;flex-wrap:wrap;margin:0 0 30px}}
 .card{{background:#fff;border:1px solid var(--line);border-radius:10px;
   padding:16px 20px;min-width:150px}}
 .card b{{display:block;font-size:26px;font-weight:600;line-height:1.2}}
 .card span{{color:var(--mut);font-size:12px}}
 table{{width:100%;border-collapse:collapse;background:#fff;
   border:1px solid var(--line);border-radius:10px;overflow:hidden}}
 th{{background:#f1f3f4;text-align:left;font-size:12px;color:var(--mut);
   padding:10px 12px;font-weight:600}}
 td{{padding:12px;border-top:1px solid var(--line);vertical-align:top;font-size:13px}}
 .dim{{display:inline-block;background:#e8f0fe;color:#1967d2;border-radius:4px;
   padding:1px 7px;font-size:11px}}
 .q{{color:#333;max-width:250px}}
 .want{{color:var(--mut);max-width:290px;font-size:12px}}
 .notes{{font-size:12px;color:#444}}
 h2{{font-size:18px;margin:44px 0 14px;font-weight:600}}
 .run{{background:#fff;border:1px solid var(--line);border-radius:8px;
   padding:12px 16px;margin-bottom:10px}}
 .rh{{font-weight:600;font-size:13px;margin-bottom:6px}}
 .kv{{display:flex;gap:10px;font-size:12.5px;margin:3px 0}}
 .kv>span{{color:var(--mut);min-width:66px;flex:none}}
 code{{background:#f1f3f4;border-radius:4px;padding:1px 6px;
   font:12px/1.6 ui-monospace,Menlo,monospace;word-break:break-all}}
 q{{color:#222}}
 .note{{background:#fff;border:1px solid var(--line);border-left:3px solid #1967d2;
   border-radius:6px;padding:14px 18px;font-size:13px;color:#444;margin:0 0 26px}}
</style></head><body><div class="wrap">
<h1>语音桥工具调用体检</h1>
<div class="sub">{stamp} · 模型 {_esc(meta['model'])} · 思考档 {_esc(meta['thinking'])}
 · 人格 {meta['persona_len']} 字 · 每例 {meta['repeat']} 遍</div>

<div class="cards">
 <div class="card"><b>{passed}/{total}</b><span>用例通过</span></div>
 <div class="card"><b style="color:{'#1b873f' if not promised else '#c0392b'}">{promised}</b>
   <span>⚠️ 只说没派</span></div>
 <div class="card"><b style="color:{'#1b873f' if not silent else '#c0392b'}">{silent}</b>
   <span>⚠️ 干完活没出声</span></div>
 <div class="card"><b>{sum(r.secs for r in runs)/max(len(runs),1):.1f}s</b>
   <span>平均一轮</span></div>
</div>

<div class="note">
 <b>这份报告测的是什么。</b>配置不是为测试另写的一套，而是直接调语音桥的
 <code>_build_config()</code> —— 模型、人格、工具声明、VAD、思考档全是它此刻在生产里用的那份。
 输入走文字、不经过 Discord 收音和 ASR，把变量收敛到「听懂之后它怎么决策」这一条链上。<br><br>
 <b>判据只看结果不看方式</b>：不要求它必须调几次、按什么顺序调，只看事情有没有办对。<br>
 <b>负例和正例一样重要</b>：一个见什么都派的模型能把所有正例考满分，
 所以「不该派」那一组挂了比「该派」挂了更严重。<br><br>
 <b>派活工具是桩，没有真派出去</b> —— 参数照单全收用来判分，返回值跟生产成功时一字不差。
 本机跑的就是 {_esc(BOT)}，真派等于给自己塞几十个任务。
</div>

<table><thead><tr>
 <th>用例</th><th>说了什么</th><th>期望</th><th>通过</th><th>每遍结果</th>
</tr></thead><tbody>{"".join(rows)}</tbody></table>

<h2>逐次明细</h2>{"".join(detail)}
</div></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--repeat", type=int, default=3,
                    help="单次结果不可复现，默认跑 3 遍报 k/n")
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--from-json", default="",
                    help="不重跑，拿已存的原始数据重出报告。"
                         "两道检测器的判据会**用当前生产代码重算** —— "
                         "它们是转写文本的纯函数，改完判据不必再花十分钟重跑一遍模型。")
    args = ap.parse_args()

    cases = [c for c in CASES if not args.only or c.id in args.only.split(",")]

    if args.from_json:
        d = json.load(open(os.path.expanduser(args.from_json), encoding="utf-8"))
        runs = [Run(**r) for r in d["runs"]]
        by_id = {c.id: c for c in CASES}
        for r in runs:
            # 判分也一起重算。报告里每一格都是「记录下来的轨迹」的纯函数，
            # 那就没有理由让其中一部分停留在跑那次的旧判据上 ——
            # 半新半旧的报告最难读：你不知道哪一列该信。
            c = by_id.get(r.case)
            if c and not r.error:
                try:
                    r.ok, r.note = c.check(r.calls, r.tasks, r.text)
                except Exception as e:
                    r.ok, r.note = False, f"check 自己炸了: {e}"
            r.promised_not_sent = (glb._promised_dispatch(r.text)
                                   and not _dispatched(r.calls))
            r.silent_after_tool = bool(_dispatched(r.calls)) and not r.text
        stamp = time.strftime("%Y%m%d-%H%M%S")
        hpath = os.path.join(OUT_DIR, f"bridge-toolcheck-{stamp}.html")
        with open(hpath, "w", encoding="utf-8") as f:
            f.write(render_html(runs, cases, stamp, d["meta"]))
        print(f"报告: {hpath}｜只说没派 "
              f"{sum(1 for r in runs if r.promised_not_sent)}｜干完活没出声 "
              f"{sum(1 for r in runs if r.silent_after_tool)}")
        return
    meta = {
        "model": glb.current_model(),
        "thinking": glb.current_thinking(),
        "voice": glb.current_voice(),
        "persona_len": len(glb.current_persona()),
        "repeat": args.repeat,
    }
    print(f"生产配置: 模型={meta['model']} 思考={meta['thinking']} "
          f"声音={meta['voice']} 人格={meta['persona_len']} 字 工具={TOOL}+google_search",
          flush=True)

    runs = asyncio.run(main_async(args))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(OUT_DIR, exist_ok=True)
    # **先落 JSON 再渲染 HTML。** 上次 render_html 里一个下标越界，
    # 把跑了四十分钟的 78 条结果全带走了。原始数据必须先落地。
    jpath = os.path.join(OUT_DIR, f"bridge-toolcheck-{stamp}.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "runs": [asdict(r) for r in runs]},
                  f, ensure_ascii=False, indent=2)
    print(f"\n原始数据: {jpath}")

    hpath = os.path.join(OUT_DIR, f"bridge-toolcheck-{stamp}.html")
    with open(hpath, "w", encoding="utf-8") as f:
        f.write(render_html(runs, cases, stamp, meta))
    print(f"报告: {hpath}")

    passed = sum(1 for r in runs if r.ok)
    print(f"通过 {passed}/{len(runs)}｜只说没派 "
          f"{sum(1 for r in runs if r.promised_not_sent)}｜干完活没出声 "
          f"{sum(1 for r in runs if r.silent_after_tool)}")


if __name__ == "__main__":
    main()
