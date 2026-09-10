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
def _default_model(monkeypatch, tmp_path_factory):
    """指纹现在是三元组，`_send_loop` 会读 current_model()。/tmp 里若躺着一份
    别人写的 model 文件，全部用例的结果都跟着变 —— 所以把文件路径指向不存在的
    地方，让它落回默认。

    **钉的是路径不是函数**：钉函数的话，下面那几条「测 current_model 本身」的
    用例就把自己要测的东西给 mock 掉了。
    """
    missing = tmp_path_factory.mktemp("nomodel") / "absent.txt"
    monkeypatch.setattr(glb, "_MODEL_FILE", str(missing))


def _bridge(cfg_fp, last_out_ago=99.0):
    """造一个不建连的桥，直接摆好 _send_loop 要读的那两个状态。"""
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
    assert b._cfg_fp == ("Erinome", "p", _M25)


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
