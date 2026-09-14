"""高密度 debug 阶段的日志层 —— 一个开关管全部。

**现在 `LK_DEBUG` 默认是 1。** 等这条链路稳定了，把下面那行的默认值改成 `"0"`，
或者在 systemd drop-in 里设 `LK_DEBUG=0`。日志不占地方，查不出病才费钱。

为什么单独开一个文件、而不是把 `logger.info` 撒进 agent.py：这些探针是**临时**的，
写在业务代码里以后就分不清哪行是逻辑、哪行是当初为了查某个 bug 加的。集中在这里，
将来一把删干净，或者一直留着按需开。

三层，各管各的：

1. **线上报文** —— 直接开 plugin 自带的 `lk_google_debug`（realtime_api.py:47）。
   它把收到的每条 LiveServerMessage 和发出去的每条非音频 client event 全 dump 出来
   （音频已经替换成 `<audio>`，不会刷屏）。这层是**唯一**能回答「Gemini 到底说了
   什么」的东西，别自己再造一遍。
2. **plugin 内部状态机** —— 打补丁包一层，把 generation 的开始/结束、tool call 的
   下发/撤销、input speech 的起止翻译成一行人话。报文那层什么都有，但读起来太碎，
   出事时需要一条能一眼扫完的时间线。
3. **我们自己的工具协程** —— 谁在跑、跑了多久、是正常结束还是被撤了之后还在跑。
   2026-09-14 那次卡死就卡在这一层的盲区：服务端撤了 tool call，本地协程毫不知情
   继续跑完，然后把结果往一个已经没人接的地方送。

补丁全部是**包一层再调原函数**，不改行为。挂不上就打 error 然后放过，
绝不因为探针把主链路搞崩。
"""

from __future__ import annotations

import functools
import logging
import os
import time

ENABLED = os.getenv("LK_DEBUG", "1") == "1"

log = logging.getLogger("lk-dbg")


def t(fmt: str, *args: object) -> None:
    """打一条 trace。关了就是个空调用。"""
    if ENABLED:
        log.info(fmt, *args)


# ── 第 3 层：我们自己的工具协程 ──────────────────────────────────────
#
# 一个进程内的全局台账。key 是我们自己发的序号，不是 Gemini 的 call_id ——
# 那个 id 在函数体里拿不到（工具签名里没有 RunContext）。靠时间戳跟第 2 层的
# `工具下发` / `服务端撤销` 两行对齐就够了，反正同一秒内不会有几十个调用。

_inflight: dict[int, tuple[str, float]] = {}
_seq = 0


def _running_now() -> str:
    if not _inflight:
        return "（无）"
    now = time.monotonic()
    return "，".join(f"{name}#{i} 已跑 {now - t0:.2f} 秒" for i, (name, t0) in _inflight.items())


def _trace_calls(fn):
    """给工具函数包一层：进、出、报错、被取消，各一行。"""

    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        global _seq
        _seq += 1
        seq = _seq
        name = fn.__name__
        _inflight[seq] = (name, time.monotonic())
        t0 = time.monotonic()
        t("工具开跑 %s#%d（此刻在跑的：%s）", name, seq, _running_now())
        try:
            out = await fn(*a, **kw)
        except BaseException as e:  # CancelledError 也要抓，它正是我们要看的那种
            _inflight.pop(seq, None)
            t("工具结束 %s#%d %.2f 秒 —— %s: %s", name, seq, time.monotonic() - t0,
              type(e).__name__, e)
            raise
        _inflight.pop(seq, None)
        t("工具结束 %s#%d %.2f 秒，返回 %d 字", name, seq, time.monotonic() - t0,
          len(out) if isinstance(out, str) else -1)
        return out

    return wrapper


def make_function_tool(real_function_tool):
    """产出一个替代品，用法跟上游的 `function_tool` 完全一样。

    两种调用形态都要接住：裸 `@function_tool` 和 `function_tool(fn, name=...)`。
    包装用 `functools.wraps`，所以 `__annotations__` 原样保留 —— 上游靠它生成
    JSON schema，丢了的话工具会带着空参数下发给模型，而且不报错。
    """
    if not ENABLED:
        return real_function_tool

    def function_tool(f=None, **kw):
        def deco(fn):
            return real_function_tool(_trace_calls(fn), **kw)

        return deco(f) if f is not None else deco

    return function_tool


# ── 第 1、2 层：plugin ──────────────────────────────────────────────


def _patch(cls, name: str, make) -> None:
    orig = getattr(cls, name, None)
    if orig is None:
        log.error("[dbg] %s.%s 不存在，这个探针没挂上（上游改过名？）", cls.__name__, name)
        return
    if getattr(orig, "_lkdbg", False):
        return
    new = make(orig)
    functools.update_wrapper(new, orig)
    new._lkdbg = True  # 幂等，别让重复调用套娃
    setattr(cls, name, new)


def _gen_id(sess) -> str:
    gen = getattr(sess, "_current_generation", None)
    if gen is None:
        return "无"
    return f"{gen.response_id}{'(已结)' if gen._done else ''}"


def arm_logging() -> None:
    """让 plugin 的 DEBUG 记录真的能走到 journal。**必须在 worker 主进程里调**
    （`cli.run_app` 之前），只在 entrypoint 里调是不够的。

    日志在这套东西里跨了两个进程，被**过滤两次**：

    1. job 子进程照**子进程自己**的 logger 级别决定发不发；
    2. 记录 pickle 到主进程之后，`LogQueueListener.handle()` 拿 `record.name`
       **再查一次主进程的 logger**（log_queue.py:49），级别不够当场丢掉。

    所以只在 entrypoint 里设，第 1 关过了、第 2 关照样被丢 —— 现象是日志里
    干干净净，看着像 dump 压根没打开。（2026-09-14 我就这么白跑了一趟重启。）

    在主进程里设还顺手把第 1 关也办了：job 进程启动时会把主进程**所有** logger
    的级别快照带过去（job_proc_executor.py:96-101）。
    """
    if not ENABLED:
        return
    logging.getLogger("livekit.plugins.google").setLevel(logging.DEBUG)


def install() -> None:
    """打开线上报文 dump，并给 plugin 的状态机挂探针。在 job 进程里调，幂等。"""
    if not ENABLED:
        return

    from livekit.plugins.google.realtime import realtime_api as ra

    # 第 1 层：plugin 自带的报文 dump。它是 import 时从环境变量读的模块级变量，
    # 这会儿改环境变量已经来不及了，直接改属性。
    ra.lk_google_debug = 1
    arm_logging()  # 级别本该是从主进程继承来的，这里兜一道，反正幂等

    S = ra.RealtimeSession

    def _start_new_generation(orig):
        def f(self):
            prev = _gen_id(self)
            orig(self)
            t("[gen] 新一轮 %s（上一轮 %s）", _gen_id(self), prev)
        return f

    def _mark_current_generation_done(orig):
        def f(self):
            gen = getattr(self, "_current_generation", None)
            if gen is not None and not gen._done:
                t("[gen] 收尾 %s：模型说了 %d 字，听到用户 %r",
                  gen.response_id, len(gen.output_text or ""),
                  (gen.input_transcription or "")[:60])
            orig(self)
        return f

    def _handle_server_content(orig):
        def f(self, sc):
            bits = []
            if sc.model_turn:
                n_audio = sum(
                    1 for p in (sc.model_turn.parts or []) if getattr(p, "inline_data", None)
                )
                bits.append(f"音频×{n_audio}" if n_audio else "model_turn")
            if sc.input_transcription and sc.input_transcription.text:
                bits.append(f"听到={sc.input_transcription.text!r}")
            if sc.output_transcription and sc.output_transcription.text:
                bits.append(f"说出={sc.output_transcription.text!r}")
            if sc.interrupted:
                bits.append("**被打断**")
            if sc.generation_complete:
                bits.append("generation_complete")
            if sc.turn_complete:
                bits.append("turn_complete")
            # 纯音频帧每秒几十条，只有带「事件」的才值得留一行
            if bits and bits != ["音频×1"]:
                t("[srv] %s ‖ 当前轮=%s", " ".join(bits), _gen_id(self))
            orig(self, sc)
        return f

    def _handle_tool_calls(orig):
        def f(self, tool_call):
            calls = tool_call.function_calls or []
            t("[tool] 服务端下发 %d 个：%s ‖ 当前轮=%s",
              len(calls), [f"{c.name}/{c.id}" for c in calls], _gen_id(self))
            orig(self, tool_call)
        return f

    def _handle_tool_call_cancellation(orig):
        def f(self, cancellation):
            # **这行是 2026-09-14 那次卡死的核心证据位。** 服务端撤了哪几个 id，
            # 而本地此刻还有哪几个协程蒙在鼓里接着跑。
            t("[tool] 服务端撤销 %s ‖ 本地还在跑：%s ‖ 当前轮=%s",
              list(cancellation.ids or []), _running_now(), _gen_id(self))
            orig(self, cancellation)
        return f

    def _handle_input_speech_started(orig):
        def f(self):
            t("[vad] 用户开口（服务端判的）‖ 当前轮=%s", _gen_id(self))
            orig(self)
        return f

    def _handle_input_speech_stopped(orig):
        def f(self):
            t("[vad] 用户说完 ‖ 当前轮=%s", _gen_id(self))
            orig(self)
        return f

    def _update_chat_ctx(orig):
        async def f(self, chat_ctx):
            # 3.1 Live 在建会话时就警告过 "limited mid-session update support"：
            # 这个调用对它基本是空操作。被打断的工具结果就是走这条路回去的 ——
            # 如果它真的没生效，模型永远不知道那次调用发生过什么。
            t("[ctx] update_chat_ctx（%d 条）—— 3.1 Live 对中途更新支持有限，"
              "留意它到底生没生效", len(chat_ctx.items))
            return await orig(self, chat_ctx)
        return f

    for name, make in (
        ("_start_new_generation", _start_new_generation),
        ("_mark_current_generation_done", _mark_current_generation_done),
        ("_handle_server_content", _handle_server_content),
        ("_handle_tool_calls", _handle_tool_calls),
        ("_handle_tool_call_cancellation", _handle_tool_call_cancellation),
        ("_handle_input_speech_started", _handle_input_speech_started),
        ("_handle_input_speech_stopped", _handle_input_speech_stopped),
        ("update_chat_ctx", _update_chat_ctx),
    ):
        _patch(S, name, make)

    # 自检。第 1 层的报文 dump 走的是 **DEBUG**，而 livekit CLI 自己往 root 上装了
    # handler —— 那个 handler 的级别我们管不着。所以这里拿**同一个 logger、同一个
    # 级别**先发一行：journal 里看得到它，才说明 dump 真的通了。
    # 不做这步的话，等出事那天翻日志发现一片空白，还得先花时间分辨是「没抓到」
    # 还是「压根没打开」。
    logging.getLogger("livekit.plugins.google").debug(
        "[dbg] 报文 dump 自检：能看到这行，说明 DEBUG 级别通到 journal 了")

    t("[dbg] 调试日志已开（LK_DEBUG=1）。稳定后把 dbg.py 的默认值改成 0。")


# ── 会话级：说话回合的一生 ──────────────────────────────────────────


def arm_session(session, room: str) -> None:
    """挂 AgentSession 的事件。`agent_state_changed` 在 agent.py 里已经有了。"""
    if not ENABLED:
        return

    def _on_speech(ev):
        h = ev.speech_handle
        t0 = time.monotonic()
        t("[说] 新回合 %s", h.id)

        def _done(_):
            t("[说] 回合 %s 结束 %.2f 秒，被打断=%s",
              h.id, time.monotonic() - t0, h.interrupted)

        h.add_done_callback(_done)

    session.on("speech_created", _on_speech)
    session.on("user_input_transcribed",
               lambda ev: t("[听] %r（final=%s）", ev.transcript, ev.is_final))
    # item 也可能是 AgentHandoff 之类没有 role/text_content 的东西，别让探针自己抛
    session.on("conversation_item_added",
               lambda ev: t("[史] +%s %r", getattr(ev.item, "role", ev.item.type),
                            (getattr(ev.item, "text_content", "") or "")[:80]))
    session.on("function_tools_executed",
               lambda ev: t("[tool] 本轮执行完毕：%s",
                            [c.name for c in (ev.function_calls or [])]))
    session.on("agent_false_interruption",
               lambda ev: t("[打断] 判定为误触发，已恢复=%s", ev.resumed))
    session.on("error", lambda ev: t("[错] %s", ev.error))
    t("[dbg] 房间 %s 的会话探针已挂", room)
