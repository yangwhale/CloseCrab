#!/usr/bin/env python3
"""不走语音桥，直接问 Gemini Live 一句话：你到底能不能调我给你的工具？

**为什么必须绕开 bridge。** bridge 那条路上串着 Discord 收音、DAVE 解密、VAD、
ASR、TTS 回放 —— 任何一环出问题都表现为「它没搜」。要判「工具通道本身通不通」，
就得把这些全摘掉：**文字进**、不解码音频，只留 function calling 这一条链。
（输出摘不掉 —— native-audio 模型拒绝 TEXT modality，见下面 1007 那段。）

背景（`gemini_live_bridge.py` 里 `tools=` 那段有完整版）：
  2026-09-08 试过 `types.Tool(mcp_servers=[...])` 直接把 jina 的 MCP 挂上去 ——
  **握手过、模型说话正常、但它看不见那些工具**，问它搜个东西回「我无法访问网页」。
  握手不报错 ≠ 生效。所以那条路是死的。

这个脚本验的是**另一条路**：把 jina 搜索包成一个普通的 `function_declaration`，
模型要用就发 tool_call 给我们，**我们自己去调 Jina 的 REST**，再把结果回灌。
两条路的区别是「谁去连 jina」—— MCP 是服务端连（连不上），这条是我们连。

用法:
  source ~/.zshenv && scripts/live-tool-probe.py
  scripts/live-tool-probe.py --model gemini-2.5-flash-native-audio-preview-12-2025
  scripts/live-tool-probe.py --prompt "搜一下 vLLM 最新版本号" --no-google-search
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request

from google import genai
from google.genai import types

_MODELS = (
    "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-native-audio-preview-12-2025",
)

# 工具声明。描述写得具体一点 —— 模型是靠这段话决定要不要调它的，
# 写成「搜索工具」它可能觉得内置的 google_search 更顺手就不调了。
_TOOL_JINA = types.FunctionDeclaration(
    name="jina_search",
    description=(
        "用 Jina 搜索引擎检索实时网络信息，返回标题、链接和摘要。"
        "需要最新资料、版本号、新闻、文档内容时用这个。"
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "query": types.Schema(type=types.Type.STRING, description="搜索关键词"),
        },
        required=["query"],
    ),
)


def jina_search(query: str, timeout: float = 20.0) -> dict:
    """真去调 Jina 的搜索端点。失败**如实返回错误**，不要编一个假结果回去 ——
    模型拿到假结果会说得头头是道，那比工具报错难查一百倍。"""
    auth = os.environ.get("JINA_AUTH") or ""
    if not auth and os.environ.get("JINA_API_KEY"):
        auth = "Bearer " + os.environ["JINA_API_KEY"]
    if not auth:
        return {"error": "没有 JINA_AUTH / JINA_API_KEY"}
    url = "https://s.jina.ai/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={
        "Authorization": auth,
        "Accept": "application/json",
        "X-Respond-With": "no-content",   # 只要标题+摘要，不要正文，省 token
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    items = (data.get("data") or [])[:5]
    return {"results": [
        {"title": it.get("title"), "url": it.get("url"),
         "snippet": (it.get("description") or "")[:300]}
        for it in items
    ]}


def find_api_key() -> str:
    for var in ("GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY", "GOOGLE_API_KEY"):
        v = os.environ.get(var, "")
        if v.startswith("AIza"):
            return v
    sys.exit("没有 Gemini API key —— 先 source ~/.zshenv")


async def probe(model: str, prompt: str, with_google: bool, timeout: float,
                neutral: bool = False) -> int:
    tools = [types.Tool(function_declarations=[_TOOL_JINA])]
    # **两个独立的 Tool 对象。** bridge 那边实测过合并会让路由错乱
    # （问「搜一下 X」它去调了别的工具）。这里保持一致，免得测出来的
    # 行为跟生产不是一回事。
    if with_google:
        tools.append(types.Tool(google_search=types.GoogleSearch()))

    cfg = types.LiveConnectConfig(
        # **只能要 AUDIO。** 本来想文字进文字出把 TTS 也摘掉，结果握手直接被拒：
        #   1007 The requested combination of response modalities (TEXT) is not
        #        supported by the model. models/gemini-3.1-flash-live-preview
        # native-audio 系列就是只出音频。于是退一步：仍然**文字进**（ASR 摘掉了，
        # 这是这个探针的主要目的），输出让它照常发音频，但我们不解码不播放，
        # 只读服务端顺带给的输出转写 —— 拿它当「模型说了什么」的文本代理。
        response_modalities=["AUDIO"],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=tools,
        # **两套 system instruction，因为它们回答的是两个不同的问题。**
        # 默认那套点名 jina，验的是「通道通不通」；`--neutral` 不点名，
        # 验的是「同时挂着内置 google_search 时它自己会不会选 jina」。
        # 只跑默认那套就下「生产里它会用 jina」的结论，是拿被引导的结果当自发行为。
        system_instruction=types.Content(parts=[types.Part.from_text(
            text=("你是个测试用助手。回答简短，直接给结论。" if neutral else
                  "你是个测试用助手。需要联网信息时**优先调用 jina_search 工具**。"
                  "回答简短，直接给结论。"))]),
    )

    client = genai.Client(api_key=find_api_key())
    t0 = time.time()
    calls, texts, errors = [], [], []

    def mark(tag: str, msg: str = "") -> None:
        print(f"[{time.time()-t0:6.2f}s] {tag:<14} {msg}", flush=True)

    async with client.aio.live.connect(model=model, config=cfg) as session:
        mark("已连接", model)
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))
        mark("已发问", prompt)

        async def pump():
            async for resp in session.receive():
                if resp.tool_call:
                    responses = []
                    for fc in resp.tool_call.function_calls:
                        calls.append(fc.name)
                        mark("⚙ 模型要调", f"{fc.name}({json.dumps(dict(fc.args), ensure_ascii=False)})")
                        if fc.name == "jina_search":
                            out = await asyncio.to_thread(jina_search, fc.args.get("query", ""))
                        else:
                            out = {"error": f"未知工具 {fc.name}"}
                        n = len(out.get("results", []))
                        mark("↩ 我回给它", out.get("error") or f"{n} 条结果")
                        responses.append(types.FunctionResponse(
                            id=fc.id, name=fc.name, response=out))
                    await session.send_tool_response(function_responses=responses)
                sc = resp.server_content
                if sc and sc.output_transcription and sc.output_transcription.text:
                    texts.append(sc.output_transcription.text)
                if sc and sc.model_turn:
                    for p in sc.model_turn.parts:
                        if p.text:          # AUDIO 模式下基本不会有，留着兜底
                            texts.append(p.text)
                if sc and sc.turn_complete:
                    mark("本轮结束")
                    return
                if getattr(resp, "go_away", None):
                    errors.append("服务端 go_away")
                    return

        try:
            await asyncio.wait_for(pump(), timeout=timeout)
        except asyncio.TimeoutError:
            errors.append(f"{timeout}s 超时")

    print("\n" + "=" * 60)
    print(f"模型      : {model}")
    print(f"调了工具  : {calls or '一个都没调'}")
    print(f"最终回答  : {''.join(texts).strip() or '(空)'}")
    if errors:
        print(f"异常      : {errors}")
    print("=" * 60)
    # 判据只有一条：**它有没有真的调 jina_search**。回答内容好不好看是另一回事。
    ok = "jina_search" in calls
    print("结论: " + ("✅ Live 能用上 jina 工具" if ok else "❌ 没调 jina_search"))
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=_MODELS[0], choices=_MODELS)
    ap.add_argument("--prompt", default="用 jina_search 搜一下 SGLang 最新的版本号是多少，然后告诉我。")
    ap.add_argument("--no-google-search", action="store_true",
                    help="不挂内置 google_search —— 排除「它偷懒用内置的」这个干扰项")
    ap.add_argument("--neutral", action="store_true",
                    help="system prompt 不点名 jina —— 看它自发选哪个工具")
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args()
    sys.exit(asyncio.run(probe(args.model, args.prompt,
                               not args.no_google_search, args.timeout, args.neutral)))


if __name__ == "__main__":
    main()
