#!/usr/bin/env python3
"""「干完活没出声」检测器的单测。

这道检测器盯的是语音场景里最难查的一种失败：**工具调了、活干了、一个字没说**。
用户那头听到的是一片安静 —— 跟连接断了、跟没听见，长得一模一样。他会把整句话
重说一遍，然后以为这套东西又坏了。

不是假想出来的风险。2026-09-11 的工具对比台（`scripts/live-tool-bench.py`，
13 例 × 2 模型 × 3 遍 = 78 次）实测：**2.5 native-audio 有 6/39 次跑完工具后
完全不出声**，全部集中在两道 jina 搜索题上。当时先怀疑是转写投递时序问题
（服务端把最后几段 output_transcription 排在 turn_complete 后面发），专门写了个
探针去数 inline_data 的字节数 —— **0 字节**，它是真的什么都没生成。3.1 是 0/39。

persona 里已经写了「调了工具就必须开口」，但那是 prompt 压 prompt，压不死。
所以补这道确定性的检查：压不住，至少日志里查得到。

**这份测试驱动的是真的 `_recv_loop`**，不是把判断条件在测试里重抄一遍。
（`test_ssrc_rotation.py` 第一版就是抄了一遍守卫逻辑，测出来永远是绿的，
后来整个重写 —— 那种测试只能证明「我抄对了我自己」。）

负例比正例重要：这行警告一旦在正常轮次上乱响，就会变成没人看的噪音，
真出事的那次也会被一起忽略掉。所以三种**正当沉默**各有一条用例压着。

    python3 -m pytest test_voice_silent_completion.py -q
"""
import asyncio
import types

import pytest

from closecrab.voice import gemini_live_bridge as glb


# ── 假消息：只长出 _recv_loop 会摸到的那几个属性 ────────────────────────


class _Resp:
    """服务端推过来的一条消息。

    `_recv_loop` 一上来就 `resp.model_dump(exclude_none=True)`，所以这里得
    同时供两副面孔：属性访问（`resp.tool_call`）和 dict（`sc.get("turn_complete")`）。
    """

    def __init__(self, *, tool_call=None, server_content=None, dump=None):
        self.tool_call = tool_call
        self.server_content = server_content
        self.session_resumption_update = None
        self.go_away = None
        self._dump = dump or {}

    def model_dump(self, exclude_none=True):
        return self._dump


def _tool_call(name="ask_bunny"):
    call = types.SimpleNamespace(id="fc_1", name=name, args={"task": "查点东西"})
    return types.SimpleNamespace(function_calls=[call])


def _audio_chunk():
    """一段模型音频。inline_data 里塞什么无所谓，只要非空。"""
    part = types.SimpleNamespace(
        text=None, inline_data=types.SimpleNamespace(data=b"\x00\x01")
    )
    return _Resp(
        server_content=types.SimpleNamespace(model_turn=types.SimpleNamespace(parts=[part])),
        dump={"server_content": {"model_turn": {}}},
    )


def _transcript(text):
    return _Resp(dump={"server_content": {"output_transcription": {"text": text}}})


def _interrupted():
    return _Resp(dump={"server_content": {"interrupted": True}})


def _turn_complete():
    return _Resp(dump={"server_content": {"turn_complete": True}})


class _FakeSession:
    """一次性的假 session。

    **第二次 receive() 必须让循环停下来。** `_recv_loop` 外层是
    `while self._running:`，每轮 turn_complete 后 break 出来再进一次 receive()
    接下一轮 —— 假 session 要是每次都把脚本从头重播一遍，这个循环里又没有
    任何真正的 await 点，事件循环永远拿不回控制权，测试就是纯挂死
    （第一版就是这么写的，跑了三分钟一个字没输出）。
    """

    def __init__(self, script, bridge):
        self._script = script
        self._bridge = bridge
        self._calls = 0

    async def receive(self):
        self._calls += 1
        if self._calls > 1:
            self._bridge._running = False
            return
        for resp in self._script:
            # **每条消息之间必须让出一次事件循环。** 真实链路上每条消息都是从
            # 网络上收下来的，天然带 await 点，甩出去的工具任务因此有机会被调度。
            # 假 session 要是一口气把脚本同步塞完，工具任务连起跑都没起跑，
            # turn_complete 那里就会一律判成「工具还在跑」，把该响的警告吃掉 ——
            # 测出来是绿的，测的却不是生产里会发生的事。
            await asyncio.sleep(0)
            yield resp
        self._bridge._running = False


def _run(script, *, tool_hangs=False):
    """把一串假消息喂给真的 `_recv_loop`，收集它打出来的交付日志。

    返回 [(tag, message), ...]。
    """
    bridge = object.__new__(glb.GeminiLiveBridge)
    logged: list = []

    bridge._running = True
    bridge._n_recv = 0
    bridge._tasks = set()
    bridge._resume_handle = None
    bridge._turn_t0 = None
    bridge._turn_first_audio_at = None
    bridge._log_delivery = lambda tag, msg: logged.append((tag, msg))
    bridge._log_turn_latency = lambda resp: None
    bridge._drop_pending_audio = lambda: None
    bridge._play_gemini_audio = lambda pcm: None

    async def _tool(session, call):
        # tool_hangs=True 模拟「turn_complete 到了，工具还挂着」：
        # 这是正当沉默，结果回来后模型会在下一轮接着说。
        if tool_hangs:
            await asyncio.sleep(3600)

    bridge._handle_tool_call = _tool

    async def go():
        session = _FakeSession(script, bridge)
        try:
            await asyncio.wait_for(bridge._recv_loop(session), timeout=5)
        finally:
            for t in list(bridge._tasks):
                t.cancel()

    asyncio.run(go())
    return logged


def _tags(logged):
    return [tag for tag, _ in logged]


# ── 正例：这一行必须响 ──────────────────────────────────────────────


def test_调了工具却一声不吭_要报警():
    """实测过的那 6/39：工具调完，音频 0 字节、转写空。"""
    logged = _run([_Resp(tool_call=_tool_call(), dump={"tool_call": {}}), _turn_complete()])
    assert "⚠️ [干完活没出声]" in _tags(logged)


# ── 负例：这三种沉默是正当的，响了就是噪音 ──────────────────────────


def test_调了工具也说了话_不报警():
    """最常见的正常轮次。这条要是挂了，说明检测器在对每次工具调用乱叫。"""
    logged = _run([
        _Resp(tool_call=_tool_call(), dump={"tool_call": {}}),
        _audio_chunk(),
        _transcript("已经交给巴尼去查了"),
        _turn_complete(),
    ])
    assert "⚠️ [干完活没出声]" not in _tags(logged)
    assert "🤖 [它回了啥]" in _tags(logged)


def test_出了声但转写是空的_不报警():
    """音频推出去了、转写没跟上 —— 用户**听得见**，不是这个病。

    这条正是 2026-09-11 差点误判的那种情况：只看转写为空就报警，会把一堆
    正常轮次记成失败。判据必须是「有没有出过声」，不是「有没有转写」。
    """
    logged = _run([
        _Resp(tool_call=_tool_call(), dump={"tool_call": {}}),
        _audio_chunk(),
        _turn_complete(),
    ])
    assert "⚠️ [干完活没出声]" not in _tags(logged)


def test_被用户打断_不报警():
    """插话本来就该闭嘴。"""
    logged = _run([
        _Resp(tool_call=_tool_call(), dump={"tool_call": {}}),
        _interrupted(),
        _turn_complete(),
    ])
    assert "⚠️ [干完活没出声]" not in _tags(logged)
    assert "✋ [被打断]" in _tags(logged)


def test_工具还没跑完_不报警只留一行说明():
    """结果回来后模型会在下一轮接着说，这时候报警是冤枉它。"""
    logged = _run(
        [_Resp(tool_call=_tool_call(), dump={"tool_call": {}}), _turn_complete()],
        tool_hangs=True,
    )
    assert "⚠️ [干完活没出声]" not in _tags(logged)
    assert any("工具还在跑" in msg for _, msg in logged)


def test_压根没调工具的空轮_不报警():
    """纯空轮跟这个病无关 —— 别把它也算进来。"""
    logged = _run([_turn_complete()])
    assert "⚠️ [干完活没出声]" not in _tags(logged)


# ── 镜像那道（「只说没派」）不能被这次改动碰坏 ────────────────────────


def test_镜像那道检测器还在正常工作():
    """说了要派、却没调工具 —— 老规矩，照旧要响。"""
    logged = _run([_transcript("我已经叫巴尼去查了，稍等"), _turn_complete()])
    assert "⚠️ [只说没派]" in _tags(logged)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
