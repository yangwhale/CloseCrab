#!/usr/bin/env python3
"""换声音/persona 后主动重连的单测。

声音只能在建连时定死，中途改不了 —— 所以「让改动生效」只能是「重连一次」。
`_send_loop` 在**双方都不说话**的空档发现配置变了就抛 `_ConfigChanged`，
`_worker_loop` 接住它、跳过 3 秒退避立刻重连。

这里锁住的是那个「什么时候才敢断」的判断。断早了会把 Gemini 的回答拦腰截断，
断晚了用户对着旧声音干等 —— 两个方向都要有测试压着。

    python3 -m pytest test_gemini_live_config_reload.py -q
"""
import asyncio
import inspect
import pathlib
import time

import pytest

from closecrab.voice import gemini_live_bridge as glb


class _FakeSession:
    """只记下发了什么，不真连 WebSocket。"""

    def __init__(self):
        self.stream_ends = 0

    async def send_realtime_input(self, **kw):
        if kw.get("audio_stream_end"):
            self.stream_ends += 1


# 大多数用例不关心模型这一维，用默认值把三元组占满。真正测模型切换的在下面
# 「模型白名单与切换」那节。
_M = glb._DEFAULT_MODEL


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch, tmp_path_factory):
    """把**三个** /tmp 配置文件全指向不存在的路径，让每条用例都从默认值起跑。

    指纹是 (voice, persona, model, thinking) 四元组，`_send_loop` 每一维都会去
    读文件。只要 /tmp 里躺着一份别人写的，全部用例的结果就跟着那份文件变 ——
    这不是假想：2026-09-10 为了 A/B 往 `/tmp/gemini-live-thinking.txt` 写了个
    `high`，三条**跟思考档位毫无关系**的用例当场变红。测试读环境状态就是这个
    下场，而且红得莫名其妙，会让人去改本来没错的代码。

    **钉的是路径不是函数**：钉函数的话，下面那几条「测 current_xxx 本身」的
    用例就把自己要测的东西给 mock 掉了。
    """
    d = tmp_path_factory.mktemp("noambient")
    monkeypatch.setattr(glb, "_MODEL_FILE", str(d / "absent-model.txt"))
    monkeypatch.setattr(glb, "_THINKING_FILE", str(d / "absent-thinking.txt"))
    monkeypatch.setattr(glb, "_VOICE_FILE", str(d / "absent-voice.txt"))


def _bridge(cfg_fp, last_out_ago=99.0):
    """造一个不建连的桥，直接摆好 _send_loop 要读的那两个状态。

    指纹允许只传前三维（声音/人格/模型）—— 这里自动补上第四维思考档位。
    下面那批用例关心的是前三维，补一下比每处都抄一遍 _DEFAULT_THINKING 清楚。
    """
    if cfg_fp is not None and len(cfg_fp) == 3:
        cfg_fp = (*cfg_fp, glb._DEFAULT_THINKING)
    b = glb.GeminiLiveBridge.__new__(glb.GeminiLiveBridge)
    b._running = True
    b._audio_queue = asyncio.Queue()
    b._cfg_fp = cfg_fp
    b._last_out_at = time.monotonic() - last_out_ago
    b._n_sent = b._n_fed = b._n_dropped = b._n_recv = 0
    b._log_delivery = lambda *a, **k: None
    return b


def _run_send_loop(b, timeout=2.0):
    """跑 _send_loop 直到它抛 _ConfigChanged，或者超时判定它没抛。"""
    async def run():
        return await asyncio.wait_for(b._send_loop(_FakeSession()), timeout=timeout)

    return asyncio.run(run())


# ── 该重连的 ──────────────────────────────────────────────────────────────

def test_voice_change_triggers_reconnect(monkeypatch):
    monkeypatch.setattr(glb, "current_voice", lambda: "Erinome")
    monkeypatch.setattr(glb, "current_persona", lambda: "p")
    b = _bridge(cfg_fp=("Zubenelgenubi", "p", _M))
    with pytest.raises(glb._ConfigChanged):
        _run_send_loop(b)


def test_persona_change_triggers_reconnect(monkeypatch):
    """persona 也要能触发 —— 指纹是 (voice, persona, model) 三元组，不是只看声音。"""
    monkeypatch.setattr(glb, "current_voice", lambda: "Erinome")
    monkeypatch.setattr(glb, "current_persona", lambda: "新人格")
    b = _bridge(cfg_fp=("Erinome", "旧人格", _M))
    with pytest.raises(glb._ConfigChanged):
        _run_send_loop(b)


# ── 不该重连的（negative） ─────────────────────────────────────────────────

def test_unchanged_config_never_reconnects(monkeypatch):
    """没改就不能断。少了这条，「无脑断连」也能让上面两条过 —— 而那是每 0.6 秒
    重连一次的死循环。"""
    monkeypatch.setattr(glb, "current_voice", lambda: "Erinome")
    monkeypatch.setattr(glb, "current_persona", lambda: "p")
    b = _bridge(cfg_fp=("Erinome", "p", _M))
    with pytest.raises(asyncio.TimeoutError):
        _run_send_loop(b)


def test_no_reconnect_while_gemini_is_speaking(monkeypatch):
    """Gemini 独白时上行本来就是静的 —— 只看「没收到帧」会把它的回答拦腰截断。

    这条是整个机制里最容易写错的地方：`_IDLE_GAP_S` 超时只证明**用户**没说话。
    """
    monkeypatch.setattr(glb, "current_voice", lambda: "Erinome")
    monkeypatch.setattr(glb, "current_persona", lambda: "p")
    # 配置确实变了，但 0.1 秒前还在往 Discord 喂它的声音
    b = _bridge(cfg_fp=("Zubenelgenubi", "p", _M), last_out_ago=0.1)
    with pytest.raises(asyncio.TimeoutError):
        _run_send_loop(b, timeout=1.0)


def test_reconnects_once_gemini_falls_silent(monkeypatch):
    """接上一条：它说完、下行也静下来之后，才轮到我们断。"""
    monkeypatch.setattr(glb, "current_voice", lambda: "Erinome")
    monkeypatch.setattr(glb, "current_persona", lambda: "p")
    b = _bridge(cfg_fp=("Zubenelgenubi", "p", _M), last_out_ago=glb._OUT_QUIET_S + 0.2)
    with pytest.raises(glb._ConfigChanged):
        _run_send_loop(b)


def test_first_connection_is_not_a_change(monkeypatch):
    """_cfg_fp 还没写入（None）时不能当成「配置变了」—— 那是刚建连还没走到
    _build_config，断掉等于连都连不上。"""
    monkeypatch.setattr(glb, "current_voice", lambda: "Erinome")
    monkeypatch.setattr(glb, "current_persona", lambda: "p")
    b = _bridge(cfg_fp=None)
    with pytest.raises(asyncio.TimeoutError):
        _run_send_loop(b, timeout=1.0)


# ── 声音白名单 ────────────────────────────────────────────────────────────

def test_bad_voice_name_falls_back(monkeypatch, tmp_path):
    """写错名字要退回默认。透传出去服务端会在握手阶段拒绝，然后进 3 秒一次的
    无限重连，日志里只有一句语焉不详的连接失败。"""
    f = tmp_path / "voice.txt"
    f.write_text("Nonexistent\n")
    monkeypatch.setattr(glb, "_VOICE_FILE", str(f))
    assert glb.current_voice() == glb._DEFAULT_VOICE

    f.write_text("Erinome\n")
    assert glb.current_voice() == "Erinome"


# ── 模型白名单与切换 ──────────────────────────────────────────────────────

_M25 = "gemini-2.5-flash-native-audio-preview-12-2025"


def test_model_file_missing_uses_default(monkeypatch, tmp_path):
    monkeypatch.setattr(glb, "_MODEL_FILE", str(tmp_path / "nope.txt"))
    assert glb.current_model() == glb._DEFAULT_MODEL


def test_model_file_selects_whitelisted(monkeypatch, tmp_path):
    f = tmp_path / "model.txt"
    f.write_text(_M25 + "\n")
    monkeypatch.setattr(glb, "_MODEL_FILE", str(f))
    assert glb.current_model() == _M25


def test_bad_model_name_falls_back(monkeypatch, tmp_path):
    """negative —— 跟声音那条同一个病因：模型名写错会在握手阶段被拒，
    然后是 3 秒一次的无限重连。宁可退回默认也不要透传。"""
    f = tmp_path / "model.txt"
    f.write_text("gemini-9.9-imaginary\n")
    monkeypatch.setattr(glb, "_MODEL_FILE", str(f))
    assert glb.current_model() == glb._DEFAULT_MODEL


def test_model_change_triggers_reconnect_and_drops_handle(monkeypatch):
    """换模型必须重连，而且**旧 handle 要扔掉** —— 它是上一个模型那边的凭证。"""
    monkeypatch.setattr(glb, "current_voice", lambda: "Erinome")
    monkeypatch.setattr(glb, "current_persona", lambda: "p")
    monkeypatch.setattr(glb, "current_model", lambda: _M25)
    b = _bridge(cfg_fp=("Erinome", "p", glb._DEFAULT_MODEL))
    b._resume_handle = "old-handle"
    with pytest.raises(glb._ConfigChanged):
        _run_send_loop(b)
    assert b._resume_handle is None


def test_voice_change_keeps_handle(monkeypatch):
    """negative —— 只有换模型才丢 handle。声音变了还是同一个后端 session，
    连它一起扔等于每次换声音都失忆一次。"""
    monkeypatch.setattr(glb, "current_voice", lambda: "Kore")
    monkeypatch.setattr(glb, "current_persona", lambda: "p")
    b = _bridge(cfg_fp=("Erinome", "p", glb._DEFAULT_MODEL))
    b._resume_handle = "keep-me"
    with pytest.raises(glb._ConfigChanged):
        _run_send_loop(b)
    assert b._resume_handle == "keep-me"


def test_is_gemini_3_discriminates():
    assert glb._is_gemini_3("gemini-3.1-flash-live-preview")
    # negative：2.5 走的是 thinking_budget，误判成 3.x 会传错字段被服务端拒
    assert not glb._is_gemini_3(_M25)


# ── 识别质量相关的建连参数 ────────────────────────────────────────────────

def _cfg(monkeypatch, model):
    monkeypatch.setattr(glb, "current_voice", lambda: "Erinome")
    monkeypatch.setattr(glb, "current_persona", lambda: "p")
    monkeypatch.setattr(glb, "current_model", lambda: model)
    b = glb.GeminiLiveBridge.__new__(glb.GeminiLiveBridge)
    b._resume_handle = None
    b._cfg_fp = None
    return b._build_config(), b


def test_vad_accumulates_context(monkeypatch):
    """症状是「一句话被切碎、逐片理解」。官方文档把它归因到 silence_duration_ms
    过小；这里钉住我们比服务端默认(约 800ms)更宽松，且首音节有回看余量。"""
    cfg, _ = _cfg(monkeypatch, glb._DEFAULT_MODEL)
    vad = cfg.realtime_input_config.automatic_activity_detection
    assert vad.disabled is False
    assert vad.silence_duration_ms > 800
    assert vad.prefix_padding_ms >= 200
    assert vad.end_of_speech_sensitivity == glb.types.EndSensitivity.END_SENSITIVITY_LOW
    # negative：起始灵敏度**不能**调低，那会漏掉开口瞬间
    assert vad.start_of_speech_sensitivity is None


def test_thinking_level_set_on_3x(monkeypatch):
    """3.1 默认 minimal —— 不显式抬高就等于让它不思考。"""
    cfg, _ = _cfg(monkeypatch, glb._DEFAULT_MODEL)
    assert cfg.thinking_config is not None
    assert cfg.thinking_config.thinking_level == glb.types.ThinkingLevel.LOW


def test_thinking_level_absent_on_25(monkeypatch):
    """negative —— 2.5 用 thinking_budget，把 thinking_level 传过去是握手被拒。"""
    cfg, _ = _cfg(monkeypatch, _M25)
    assert cfg.thinking_config is None
    # VAD 那组跟模型无关，两边都要有
    assert cfg.realtime_input_config.automatic_activity_detection.silence_duration_ms > 800


def test_cfg_fp_records_the_model_actually_used(monkeypatch):
    """指纹必须记下这次真用的模型，否则切回来时比不出差异、改动被静默吞掉。"""
    _, b = _cfg(monkeypatch, _M25)
    assert b._cfg_fp == ("Erinome", "p", _M25, glb._DEFAULT_THINKING)


# ── 过期 session handle 的识别 ────────────────────────────────────────────
#
# 2026-09-09 事故：服务端判定 session 过期后回 1008，重连时我们仍带着同一个
# handle，于是被同样的理由拒绝 —— 5.6 小时空转 5729 次，语音全程是哑的。

@pytest.mark.parametrize("msg", [
    "1008 None. BidiGenerateContent session expired",
    "session not found",
    "invalid session resumption handle",
])
def test_stale_handle_detected(msg):
    assert glb._is_stale_session_error(Exception(msg)) is True


@pytest.mark.parametrize("msg", [
    # 这条是**正常的周期性重置**（官方说连接寿命约 10 分钟），handle 还有效。
    # 把它误判成过期 = 每十分钟丢一次对话历史，比原 bug 更隐蔽。
    "1008 None. The operation was aborted.",
    "Connection reset by peer",
    "500 Internal Server Error",
])
def test_normal_disconnect_keeps_handle(msg):
    """negative: 不能靠「是不是 1008」判断 —— 正常轮换报的也是 1008。"""
    assert glb._is_stale_session_error(Exception(msg)) is False


# ── persona 日志抑制 ──────────────────────────────────────────────────────
#
# current_persona() 从「建连时调一次」变成了「idle 时每 0.6 秒调一次」。
# 无条件打 INFO 会把日志冲垮（实测一天 5.6 万条），但抑制过头又会让真正的
# persona 切换悄无声息 —— 两个方向都得压住。

@pytest.fixture
def persona_dir(monkeypatch, tmp_path):
    """把 persona 查找路径指到临时目录，并清掉跨用例的抑制状态。"""
    monkeypatch.setattr(glb, "_PERSONA_DIR_RUNTIME", str(tmp_path))
    monkeypatch.setattr(glb, "_last_persona_seen", None)
    return tmp_path / f"{glb.BOT_NAME}.md"


def test_repeated_reads_log_once(persona_dir, caplog):
    """没变就只在第一次说一声 —— 这是 idle 轮询的常态路径。"""
    persona_dir.write_text("人格甲", encoding="utf-8")
    with caplog.at_level("INFO", logger=glb.log.name):
        for _ in range(20):
            assert glb.current_persona() == "人格甲"
    assert sum("使用 persona" in r.message for r in caplog.records) == 1


def test_changed_persona_logs_again(persona_dir, caplog):
    """negative: 抑制不能把真正的切换也吃掉，否则换人格后无从确认生效了没。"""
    persona_dir.write_text("人格甲", encoding="utf-8")
    with caplog.at_level("INFO", logger=glb.log.name):
        glb.current_persona()
        glb.current_persona()
        persona_dir.write_text("人格乙，长度不一样", encoding="utf-8")
        assert glb.current_persona() == "人格乙，长度不一样"
    assert sum("使用 persona" in r.message for r in caplog.records) == 2


# ── 按需建连（demand gate） ───────────────────────────────────────────────
#
# 2026-09-10：桥原来是断了就无脑重连，于是没人说话时变成「连上 → 干坐 150 秒
# 被服务端掐 → 再连」，实测一天 550 次全程零音频。现在建连前先过这道门。

def _demand_bridge():
    b = glb.GeminiLiveBridge.__new__(glb.GeminiLiveBridge)
    b._running = True
    b._audio_queue = asyncio.Queue()
    b._demand = asyncio.Event()
    b._n_fed = b._n_dropped = 0
    return b


def test_demand_gate_blocks_while_silent():
    """negative，也是这次改动的**全部意义**：没音频就不许放行。
    少了这条，`_await_demand` 直接 `return True` 也能让下面两条过。"""
    b = _demand_bridge()

    async def run():
        return await asyncio.wait_for(b._await_demand(), timeout=0.5)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run())


def test_demand_gate_opens_on_new_audio():
    """两小时不说话之后再开口 —— 新音频要能把建连那头叫醒。"""
    b = _demand_bridge()

    async def run():
        async def speak():
            await asyncio.sleep(0.05)
            b._enqueue(b"\x00" * 320)
        asyncio.create_task(speak())
        return await asyncio.wait_for(b._await_demand(), timeout=1.0)

    assert asyncio.run(run()) is True


def test_demand_gate_sees_audio_queued_before_waiting():
    """帧在我们进门之前就到了（握手期间攒下的那些）也得放行。

    这条压的是 clear/check 的顺序：`_await_demand` 先 clear 再查空，
    只 `await` 不查空的写法会把这一帧永远漏掉 —— 表现就是「说了第一句没反应」。
    """
    b = _demand_bridge()
    b._audio_queue.put_nowait(b"\x00" * 320)
    b._demand.clear()  # 模拟信号已被上一轮消费掉

    async def run():
        return await asyncio.wait_for(b._await_demand(), timeout=0.5)

    assert asyncio.run(run()) is True


def test_demand_gate_releases_on_shutdown():
    """`_running` 落下来时要返回 False 让 worker 退出，不能挂在 wait 上。"""
    b = _demand_bridge()

    async def run():
        async def shutdown():
            await asyncio.sleep(0.05)
            b._running = False
            b._demand.set()
        asyncio.create_task(shutdown())
        return await asyncio.wait_for(b._await_demand(), timeout=1.0)

    assert asyncio.run(run()) is False


# ── 空闲收摊 vs 真故障 ────────────────────────────────────────────────────

def test_idle_timeout_is_benign():
    """连上活过门槛后被掐 = 没人说话，服务端收摊。不退避、不计失败次数。"""
    e = Exception("1008 None. The operation was aborted.")
    assert glb._is_benign_idle_close(e, lived_s=155.0) is True


def test_early_abort_is_not_benign():
    """negative: 同一句话、连上没几秒就报 —— 这是真故障，必须退避。

    只匹配错误正文的写法会在这里放行，然后就是每秒一次的热重连
    （2026-09-08 空转 5.6 小时正是这个形状）。
    """
    e = Exception("1008 None. The operation was aborted.")
    assert glb._is_benign_idle_close(e, lived_s=2.0) is False


def test_other_errors_are_not_benign():
    """negative: 活得久也不代表什么错都能忍 —— 只有 abort 那句算正常收尾。"""
    assert glb._is_benign_idle_close(Exception("500 Internal Server Error"), 300.0) is False
    assert glb._is_benign_idle_close(Exception("session expired"), 300.0) is False


def test_backoff_is_capped():
    """退避要涨上去、又不能涨到没边 —— 60 秒封顶。

    原来固定 3 秒 = 每小时 1200 次无效调用。指数退避把下一个未知死循环压到
    每小时 60 次，日志上也一眼看得出「这不是偶发断线，是卡住了」。
    """
    delays = [min(3 * (2 ** min(n - 1, 5)), 60) for n in range(1, 12)]
    assert delays[0] == 3, "第一次失败不该等太久"
    assert delays == sorted(delays), "必须单调不降"
    assert max(delays) == 60, f"必须封顶在 60 秒，实际 {max(delays)}"


# ---- 交付日志按 bot 分文件 ----


def test_delivery_log_is_per_bot():
    """交付日志路径必须带 BOT_NAME。

    回归的是 2026-09-10 那次误诊：全 fleet 共写 /tmp/gemini-live-delivery.log，
    读 bunny 的重连记录时读到了天猫精灵的行，把别人的故障当成自己的修复没生效。
    """
    assert glb.BOT_NAME, "BOT_NAME 不该为空"
    assert glb.BOT_NAME in glb.LOG_FILE, f"日志路径没带 bot 名: {glb.LOG_FILE}"
    # 负向：绝不能退回那个共享路径
    assert glb.LOG_FILE != "/tmp/gemini-live-delivery.log"


def test_bridge_defaults_to_per_bot_log():
    """Bridge 不传 log_path 时用的就是那个 per-bot 路径（别只测常量、不测用法）。"""
    sig = inspect.signature(glb.GeminiLiveBridge.__init__)
    assert sig.parameters["log_path"].default == glb.LOG_FILE


# ---- 「说了要派，其实没派」检测器 ----
#
# 回归的是 2026-09-10 那次自伤：prompt 写成「先开口说两句、再调工具」，
# 模型说完两句就 turn_complete，ask_<bot> 永远发不出去。用户听到
# 「行，我让巴尼去查」，以为办了，日志里连一条工具调用都没有。


def test_tool_description_says_call_before_speaking():
    """工具描述必须要求先调工具再说话 —— 反过来写会让这一轮直接结束。"""
    desc = glb._TOOL_ASK_OWNER.description
    assert "先调这个工具，再开口说话" in desc
    # 负向：那句害人的旧措辞不许回来
    assert "调之前先开口说两句" not in desc


def test_promised_dispatch_regex_catches_real_failure():
    """命中那句真实的失败原话（日志原文，只把名字换成本机 bot 的叫法）。"""
    name = glb._SPOKEN_NAMES[0]
    real = f"你是想了解不同大模型的能力现状对吧？行，我让{name}去查一下它们的最新进展。"
    assert glb._PROMISED_DISPATCH_RE.search(real), "没抓到真实失败句"
    for phrase in (f"这个交给{name}", f"我叫{name}看一眼", f"转给{name}"):
        assert glb._PROMISED_DISPATCH_RE.search(phrase), f"漏抓: {phrase}"


def test_promised_dispatch_regex_ignores_normal_chat():
    """负向：普通闲聊不能误报，否则日志里全是狼来了。"""
    for phrase in (
        "嗯，听得非常清楚！怎么了，有什么需要帮忙的吗？",
        "我让你久等了，不好意思。",
        "这个我自己就能说，不用查。",
    ):
        assert not glb._PROMISED_DISPATCH_RE.search(phrase), f"误报: {phrase}"


# ------------------------------------------------------------ 只剩一个工具
#
# 2026-09-10 删掉了 run_shell。它最后的正当用途只剩「换自己的声音」，而声音定了
# 就不换了 —— 于是它变成一个没有用途、却是全场唯一能改坏东西的工具。
# 30 个声音名同时从所有 prompt 里消失：那不是需要长期驻留在上下文里的东西。
#
# 下面全是**负向测试**：它们不验证任何功能，只钉死「这些东西不许回来」。


def test_run_shell_is_gone_for_good():
    """负向：这个工具不许以任何形式复活。"""
    assert not hasattr(glb, "_TOOL_RUN_SHELL"), "run_shell 的声明又回来了"
    assert not hasattr(glb, "_run_shell"), "模块级 _run_shell 又回来了"
    assert not hasattr(glb.GeminiLiveBridge, "_run_shell"), "_run_shell 方法又回来了"


def test_only_one_function_declaration_reaches_the_model():
    """正向：真正发给服务端的那份 setup 里，function 有且只有 ask_<bot> 一个。

    **光看常量删干净了不算数** —— 决定模型手上有什么的是 `_build_config()`
    里那份列表，不是模块里还剩几个常量。
    """
    bridge = glb.GeminiLiveBridge.__new__(glb.GeminiLiveBridge)
    bridge._resume_handle = None
    cfg = bridge._build_config()
    declared = [f.name for t in cfg.tools if t.function_declarations for f in t.function_declarations]
    assert declared == [glb._ASK_OWNER_TOOL], f"注册的 function 不止一个: {declared}"
    # google_search 走服务端，不占 function calling 通道 —— 它该还在。
    assert any(t.google_search is not None for t in cfg.tools), "联网搜索被误删了"


def test_no_voice_name_appears_in_any_prompt_source():
    """负向：30 个声音名不许出现在**任何一份**送进上下文的文本里。

    三个来源都要查（漏一个就等于没删）：persona 文件、兜底人格、工具描述。
    """
    persona_dir = pathlib.Path(glb.__file__).parent / "personas"
    sources = {p.name: p.read_text(encoding="utf-8") for p in persona_dir.glob("*.md")}
    sources["_PERSONA_FALLBACK"] = glb._PERSONA_FALLBACK
    sources["ask_owner.description"] = glb._TOOL_ASK_OWNER.description
    for where, text in sources.items():
        listed = [n for n in glb._VOICES if n in text]
        assert not listed, f"{where} 里又出现了声音名: {listed}"


def test_voices_table_survives_as_the_validation_whitelist():
    """正向：名单本身要留着 —— 它是 `current_voice()` 唯一的校验依据。

    删名字容易删过头：表没了，写错的声音名就会被原样发给服务端。
    """
    assert glb._DEFAULT_VOICE in glb._VOICES
    assert len(glb._VOICES) >= 30


def test_personas_do_not_promise_shell_access():
    """负向：persona 不许再教模型「自己跑一条命令」—— 那个能力已经没有了。"""
    persona_dir = pathlib.Path(glb.__file__).parent / "personas"
    for p in persona_dir.glob("*.md"):
        text = p.read_text(encoding="utf-8")
        assert "run_shell" not in text, f"{p.name} 里还写着 run_shell"
        assert "自己跑" not in text, f"{p.name} 里还在教它自己跑命令"


def test_destructive_prohibition_moved_into_persona():
    """禁令原先挂在 run_shell 的工具描述上，工具删了就没人管了 —— 必须搬进 persona。

    这条最容易在删代码时一起蒸发：删的人只看到「工具没了，禁令也没用了」。
    """
    persona_dir = pathlib.Path(glb.__file__).parent / "personas"
    for name in ("bunny.md", "_default.md"):
        text = (persona_dir / name).read_text(encoding="utf-8")
        assert "杀进程" in text, f"{name} 里没有那条禁令"


# ---------------------------------------------------------- 思考档位 A/B
#
# 2026-09-10 把 thinking_level 从写死的 "low" 改成跟声音/模型同一个套路：写文件、
# 进配置指纹、空档自动重连。目的是能在**同一个会话里**来回切档做对比，
# 而不是改一次代码重启一次 —— 重启会换 session，两组数就不可比了。


def test_thinking_levels_match_the_official_list():
    """四档是查官方 Live 文档确认的，别照 SDK 枚举抄。

    SDK 的 ThinkingLevel 对所有 Gemini 3 通用，而**哪个模型认哪几档是逐模型定的**
    （3-pro-preview 只认 low/high）。这里钉死 3.1 Flash Live 那一份。
    """
    assert glb._THINKING_LEVELS == ("minimal", "low", "medium", "high")
    assert glb._DEFAULT_THINKING in glb._THINKING_LEVELS


def test_thinking_level_reads_from_file(tmp_path, monkeypatch):
    monkeypatch.setattr(glb, "_THINKING_FILE", str(tmp_path / "t.txt"))
    assert glb.current_thinking() == glb._DEFAULT_THINKING  # 文件不存在 → 默认
    (tmp_path / "t.txt").write_text("  HIGH \n", encoding="utf-8")
    assert glb.current_thinking() == "high", "要能容忍大小写和空白"


def test_bogus_thinking_level_falls_back(tmp_path, monkeypatch):
    """负向：白名单外的值必须退回默认。

    透传出去的后果跟声音名写错一样 —— 握手阶段被拒，然后 3 秒一次无限重连，
    日志里只有一句语焉不详的连接失败。
    """
    monkeypatch.setattr(glb, "_THINKING_FILE", str(tmp_path / "t.txt"))
    (tmp_path / "t.txt").write_text("ultra", encoding="utf-8")
    assert glb.current_thinking() == glb._DEFAULT_THINKING


def test_thinking_level_reaches_the_wire_and_the_fingerprint(tmp_path, monkeypatch):
    """光能读出来不算数 —— 得真进 config，而且进指纹才会触发热重连。"""
    monkeypatch.setattr(glb, "_THINKING_FILE", str(tmp_path / "t.txt"))
    (tmp_path / "t.txt").write_text("high", encoding="utf-8")
    bridge = glb.GeminiLiveBridge.__new__(glb.GeminiLiveBridge)
    bridge._resume_handle = None
    cfg = bridge._build_config()
    # SDK 会把字符串收敛成 ThinkingLevel 枚举（值是大写的 "HIGH"）。
    # 比 `.value.lower()` 而不是比字符串 —— 否则这条会因为大小写假红。
    assert cfg.thinking_config.thinking_level.value.lower() == "high"
    assert bridge._cfg_fp[3] == "high", "没进指纹 = 改了文件永远不会重连"


def test_thinking_field_is_not_sent_to_gemini_25(tmp_path, monkeypatch):
    """负向：2.5 走 thinking_budget，传 thinking_level 会被握手拒掉。"""
    monkeypatch.setattr(glb, "_MODEL_FILE", str(tmp_path / "m.txt"))
    (tmp_path / "m.txt").write_text("gemini-2.5-flash-native-audio-preview-12-2025", encoding="utf-8")
    bridge = glb.GeminiLiveBridge.__new__(glb.GeminiLiveBridge)
    bridge._resume_handle = None
    cfg = bridge._build_config()
    assert cfg.thinking_config is None


def test_latency_is_not_logged_without_an_anchor():
    """负向：没有计时起点就一条都不许记。

    没起点的轮次（工具结果回来后接着说、重连后第一条）量出来的数对不上语义，
    混进均值里就是**看不出来的错**——数字还是那么好看，只是没意义了。
    """
    bridge = glb.GeminiLiveBridge.__new__(glb.GeminiLiveBridge)
    bridge._turn_t0 = None
    logged = []
    bridge._log_delivery = lambda tag, msg: logged.append((tag, msg))
    bridge._log_turn_latency(object())
    assert logged == []


def test_latency_line_is_parsable_and_labels_the_level():
    """正向：记出来的那行要带档位、能被脚本解析 —— 不然事后没法分组对比。"""
    import time as _t
    bridge = glb.GeminiLiveBridge.__new__(glb.GeminiLiveBridge)
    bridge._thinking = "high"
    bridge._turn_t0 = _t.monotonic() - 3.0
    bridge._turn_first_audio_at = _t.monotonic() - 1.0
    logged = []
    bridge._log_delivery = lambda tag, msg: logged.append((tag, msg))
    bridge._log_turn_latency(object())
    assert len(logged) == 1
    tag, msg = logged[0]
    assert "延迟" in tag
    kv = dict(p.split("=", 1) for p in msg.split())
    assert kv["level"] == "high"
    assert 1.8 < float(kv["ttfa"]) < 2.2
    assert 2.8 < float(kv["total"]) < 3.2
    assert bridge._turn_t0 is None, "结算完必须清掉起点，否则下一轮会重复计一次"


# ------------------------------------------------------------------ AutoMod
#
# 2026-09-10 第二次改 persona 的方向：从「无条件转发」改成「每轮自己判断」。
#
# 为什么改：把 thinking_level 抬到 high 之后实测三轮，模型花了 787～902 个思考
# token，而三轮的结论**全是同一个动作 —— 转给巴尼**。判断权被 prompt 拿走了，
# 那些 token 就是纯浪费。AutoMod 把判断权还回去。
#
# 下面这几条锁的不是措辞，是**改这份 persona 的人最容易一起弄丢的东西**。
# persona 是自由文本，没有编译器 —— 一句话删掉，行为悄悄就变了。

_PERSONAS = ("bunny.md", "_default.md")


def _persona_text(name):
    return (pathlib.Path(glb.__file__).parent / "personas" / name).read_text(
        encoding="utf-8"
    )


def test_automod_is_actually_in_both_personas():
    """两份都要有。只改 bunny.md 的话，以后给别的 bot 建桥会拿到相反的规则。"""
    for name in _PERSONAS:
        assert "AutoMod" in _persona_text(name), f"{name} 没有 AutoMod 那一节"


def test_unconditional_dispatch_rule_is_gone():
    """负向 —— 旧规则必须删干净，不能跟 AutoMod 并存。

    这条比「有没有写 AutoMod」重要得多：两套相反的规则同时躺在一份 prompt 里，
    模型不会报错，它只会**每轮随机挑一套**。那种 bug 从日志上看就是「时好时坏」，
    根本查不出来。
    """
    dead = ("默认答案永远是", "你不是一个助手，你是一个转换器", "几乎所有事")
    for name in _PERSONAS:
        text = _persona_text(name)
        for phrase in dead:
            assert phrase not in text, f"{name} 里还留着旧的无条件转发规则: {phrase}"


def test_named_dispatch_survives_automod():
    """点名转发是**硬规则**，不参与 AutoMod 判断。

    删它的后果不是「多答几句」：语音频道里用户分不清是谁在说话，
    助手替巴尼答了，巴尼那边根本不知道有人叫过它 —— 跨轮次的任务就断了。
    所以不只要求这条还在，还要求**明说它凌驾于 AutoMod 之上**。
    """
    text = _persona_text("bunny.md")
    assert "用户一提「巴尼」" in text
    assert "AutoMod 在这条面前不算数" in text, "没写清点名转发不受 AutoMod 管"

    default = _persona_text("_default.md")
    assert "AutoMod 不算数" in default


def test_automod_pins_the_line_at_machine_access():
    """判据必须是「要不要看这台机器」，不能是「难不难」。

    「难的转、简单的自己答」听着合理，实际会让它自己回答「现在几点」「哪个进程
    在跑」—— 那些问题都很简单，而它手上没有任何能看这台机器的工具，只能编。
    """
    for name in _PERSONAS:
        text = _persona_text(name)
        assert "没有任何能看这台机器的工具" in text, f"{name} 没说清它看不到机器"
        assert "拿不准" in text, f"{name} 没有「拿不准就转」的兜底"


def test_tool_before_speech_survives():
    """负向 —— 先调工具再开口的纪律不能被 AutoMod 冲掉。

    它是为了压住一个实测过的失败：模型说完「我让巴尼去查」就以为办完了，
    工具一次都没发。`_PROMISED_DISPATCH_RE` 那个检测器就是为这个失败装的，
    它假设「决定转的时候一定先调工具」仍然是 persona 的本意。
    """
    for name in _PERSONAS:
        text = _persona_text(name)
        assert "顺序不能反" in text, f"{name} 丢了先调工具再开口的纪律"
        assert "先动手，再开口" in text, f"{name} 丢了那句总结"
