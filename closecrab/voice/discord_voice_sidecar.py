"""Discord 语音小尾巴 (voice-only sidecar)。

用途：当 bot 的 active_channel 是飞书时，仍想借用 Discord 的语音输出能力。
这个 sidecar 维持 Discord gateway 连接、**自动常驻**一个固定语音频道，并把
飞书对话的口语回复 (voice-summary) 镜像念到该频道——用户进频道就能听。

它**故意不注册** ``on_message`` / 任何消息处理 handler，所以「接收消息那条
路」天然堵死。它不依赖 BotCore，完全自包含，跑在独立后台 daemon 线程里。

发送 (TTS 播报) 与接收 (STT 转写) 都做：
- **发送**：``speak_text`` / 流式播放，把飞书口语回复念到常驻语音频道。
- **接收**：``/listen`` 起 ``vc.start_recording``，复用发送那条 VoiceClient
  (在共享 UDP socket 上加监听，不新建连接)。py-cord 2.8.0 + davey 原生做完
  DAVE/MLS 握手 + 逐人解密，解密后的 PCM 进 sink → 连续流 → silero VAD 断句
  → Gemini STT → 把转写文字发回频道文字区。``/stoplisten`` 停。

启用方式 (Firestore ``bots/{name}``)::

    channels:
      discord:
        token: "<bot token>"
        voice_sidecar: true              # 总开关，缺省 false 不启动
        voice_channel_id: "123..."       # 常驻的语音频道 id；缺省自动取
                                         # server 第一个语音频道

工作原理：
- ``bot.start(token)`` 在子线程跑 (run() 会装 signal handler 只能在主线程)。
- ``on_ready`` 后自动 connect 到常驻频道 (DAVE E2EE 由 py-cord 2.8.0 + davey
  处理，发协议版本 1，不再被 4017 拒)。
- 模块级 ``speak_text(text)`` 是给**飞书线程**调用的线程安全入口：用
  ``run_coroutine_threadsafe`` 把 (TTS + play) 调度到 sidecar 自己的 loop。
  飞书在 ``_send_voice_summary`` 末尾无脑调它；sidecar 没跑时静默 no-op。
"""

import asyncio
import audioop  # 3.12 可用 (3.13 PEP 594 移除, 届时换 numpy/scipy 重采样)
from dataclasses import dataclass
import logging
import os
import re
import threading
import time

# LiveKit plugin 必须在主线程 import (注册 plugin registry), 否则
# AgentSession 从 sidecar 线程启动时报 "Plugins must be registered on the main thread"。
try:
    from livekit.plugins import google as _lk_google  # noqa: F401
except ImportError:
    pass

log = logging.getLogger("closecrab.discord_voice_sidecar")

# ─── 语音 buffer 落盘 + 重播 ──────────────────────────────────────────────
# Chris 2026-06-01: 好不容易生成的音频别播完就丢, 整段存成一个文件; 点重播就把
# 这个文件重新 streaming 到同一个 Discord 语音入口 (暂停/继续复用 vc.pause/resume)。
# 文件 = 实际推给 Discord 的 48kHz/stereo/s16 raw PCM (跟 _StreamPCMSource 一致),
# 重播直接 _FilePCMSource 顺读, 不重新调 Gemini, 也不丢音。fid 编码进飞书重播按钮。
_BUF_DIR = "/tmp/jarvis-tts-buf"
_FID_RE = re.compile(r"^[0-9a-zA-Z_-]{1,64}$")  # 防路径穿越: 只许字母数字下划线连字符
_PCM_BYTES_PER_SEC = 48000 * 2 * 2  # 48kHz * stereo * s16

# 当前播放进度 (供飞书进度条 patch 卡片读)。played/total 单位 = 字节(48k/stereo/s16)。
# total<=0 表示还在生成(直播首播时总长未知); active=False 表示已播完/没在播。
_progress_lock = threading.Lock()
_progress = {"fid": "", "played": 0, "total": 0, "active": False}


def _set_progress(fid=None, *, played=None, total=None, active=None):
    with _progress_lock:
        if fid is not None:
            _progress["fid"] = fid
        if played is not None:
            _progress["played"] = played
        if total is not None:
            _progress["total"] = total
        if active is not None:
            _progress["active"] = active


def get_playback_progress():
    """返回 (elapsed_s, total_s, active, fid) 或 None。

    total_s<=0 表示总长未知(直播首播还在生成)。供飞书 _voice_progress_updater 读。
    """
    with _progress_lock:
        if not _progress["fid"]:
            return None
        return (
            _progress["played"] / _PCM_BYTES_PER_SEC,
            _progress["total"] / _PCM_BYTES_PER_SEC,
            _progress["active"],
            _progress["fid"],
        )


def _buf_path(fid: str) -> str:
    """fid → buffer 文件绝对路径。fid 不合法返回空串 (防路径穿越)。"""
    if not fid or not _FID_RE.match(fid):
        return ""
    return os.path.join(_BUF_DIR, f"{fid}.pcm")

# 模块级状态：给飞书线程跨线程调用 speak_text() 用。sidecar 未启动时全为 None/0，
# speak_text() 据此静默跳过。
_sidecar_loop: "asyncio.AbstractEventLoop | None" = None
_sidecar_bot = None
_sidecar_thread = None  # 运行时启停用：保存 sidecar 线程引用以便 stop 时 join
_target_voice_channel_id: int = 0
_heartbeat_task = None  # 后台 voice 健康检查 task，防重复启动
_commands_synced = False  # slash 命令只注册一次 (on_ready 在 RESUME 后可能重复触发)
# TTS 播报队列: hint/opener/最终回复 统一入队, 单 consumer 串行播放。
# reply 入队时清洗队列中所有 pending hint (结论到了中间过程不用再念)。
@dataclass
class _SpeakItem:
    text: str
    fid: str = ""
    is_reply: bool = False  # True = 最终回复 (fid 非空), False = hint/opener
    enqueue_time: float = 0.0  # monotonic 入队时间，测排队延迟
    backend: str = ""  # 指定 TTS 后端 (qwen3/gemini/cloud_tts)，空=用默认

_speak_queue: "asyncio.Queue[_SpeakItem] | None" = None
_speak_consumer_task: "asyncio.Task | None" = None

# 没配 voice_channel_id 的 bot 默认连这个频道 (Discord General)。多 bot 共用。
_DEFAULT_VOICE_CHANNEL_ID = "1471064068851761165"

# ─── 语音「接收」(STT) 模块级状态 ────────────────────────────────────────────
# py-cord 2.8.0 + davey 0.1.5 原生做完 DAVE/MLS 握手 + 逐人解密, 解密后的 PCM 直接
# 到 sink.write。接收复用发送那条 VoiceClient (vc.start_recording 在共享 UDP socket
# 上加监听, 不新建连接), 故不会动到发送(TTS 播放)路径。
_stt_engine = None          # GeminiSTT 单例
_stt_sink = None            # 当前 _STTSink 实例
_STT_SINK_CLASS = None      # 惰性定义的 Sink 子类
_AUDIO_INPUT_CLASS = None   # 惰性定义的 AudioInput 子类
_audio_input = None         # 当前 _DiscordAudioInput 实例
_agent_session = None       # 当前 AgentSession 实例
_pending_discord_text = []  # AgentSession 未就绪时缓存的 Discord 文字消息
_audio_pump_task = None     # 连续推帧 task (20ms 节奏 + 静音填充)
_ssrc_task = None           # ssrc 自动推断 + 录音守护 task
_listen_active = False      # 用户是否要求持续收音 (corrupted 崩溃后自动重启用)
_listen_vc = None           # 当前收音的 voice client

# ─── 全双工「大脑」桥 (复用飞书 CloseCrabLLM, Discord 只当麦克风+喇叭) ───────────
# 飞书 channel 启动时调 set_feishu_bridge() 把自己 + loop + Chris open_id 注册进来。
# 有这三样 → _start_agent_session 拼完整三阶段 (STT→CloseCrabLLM→GeminiTTS),
# Discord 麦克风进、喇叭出, 大脑还是飞书 worker; 没有 → 回落 STT-only (只发文字)。
_feishu_ref = None          # FeishuChannel 实例
_feishu_loop = None         # 飞书 event loop (CloseCrabLLM 跨 loop 调 worker 用)
_feishu_open_id = ""        # Chris 的飞书 open_id
_audio_output = None        # 当前 _DiscordAudioOutput 实例 (出口音频桥)


def set_feishu_bridge(feishu_channel, feishu_loop, open_id: str) -> None:
    """【飞书线程调用】注册飞书大脑入口, 供 Discord 语音全双工复用 CloseCrabLLM。

    幂等: 飞书 start() / on_ready 后调一次即可。open_id 为空时不覆盖已有值。
    """
    global _feishu_ref, _feishu_loop, _feishu_open_id
    _feishu_ref = feishu_channel
    _feishu_loop = feishu_loop
    if open_id:
        _feishu_open_id = open_id
    log.info("飞书大脑桥已注册 (open_id=%s…) → Discord 语音可全双工", open_id[:8] if open_id else "?")
_listen_restart_n = 0       # 录音自动重启计数 (上限保护)
_LISTEN_AUTOSTART = True     # 连上常驻频道后由心跳自动起一次录音 (重启后接收不再静默丢)
_autostart_done = False      # 本进程内自动收音只起一次 (尊重之后的 /stoplisten)
_receive_probe_installed = False  # decrypt_rtp ssrc 探针只挂一次
_dave_backend_installed = False    # dave-py 后端替换只装一次

# ── DAVE 后端总开关 (rollback 用) ──────────────────────────────────────────
# True = 把 py-cord 的 DAVE 后端从 davey 换成 dave-py (接收路径能解出真文本)。
# 为什么换: davey 不暴露逐 MLS-epoch 的 key-ratchet API, 接收端无法按 epoch 正确驱动
# DAVE 解密 → 拿不到明文 Opus → STT 永远静音。endcord 因同缺口也弃 davey 改 dave-py。
# dave-py 把 API 拆成 Session(MLS) + 逐 ssrc Decryptor(带 transition_to_key_ratchet)
# + Encryptor, 正好补上 ratchet API。
# ⚠️ 这条线**同时碰发送加密** (client.py:_get_voice_packet 调 session.encrypt_opus),
# 故换错会哑掉 TTS。万一发送坏了: 把这个置 False + 重启即回滚到纯 davey 稳定版。
_DAVE_PY_BACKEND_ENABLED = True  # 2026-06-02: dave-py 后端, 接收→STT 全链路已验证可用

_LISTEN_RESTART_MAX = 8      # 录音崩溃后最多自动重启次数

# 单声道 20ms 帧 @ 48kHz/16-bit: 喂给 AudioInput 的基本单位。
_MONO_FRAME_MS = 20
_MONO_FRAME_SAMPLES = 48000 * _MONO_FRAME_MS // 1000   # 960
_MONO_FRAME_BYTES = _MONO_FRAME_SAMPLES * 2            # 1920

# decrypt_rtp 探针记录的「传输层实收 ssrc」(每个 RTP 包都过 decrypt_rtp, 在 DAVE
# 解密门之前)。这是 ssrc 自动推断的可靠来源 —— py-cord 2.8.0 下未映射 ssrc 的包
# 在 reader 里被丢弃前不会建 decoder, 所以旧的 decoders.keys() 推断已失效。
_seen_ssrcs_lock = threading.Lock()
_seen_ssrcs: set = set()


def _load_sidecar_config(bot_name: str) -> dict | None:
    """直接从 Firestore 读 Discord 子配置 (active channel 是飞书时不会被扁平化)。"""
    try:
        from google.cloud import firestore
        from ..constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE

        db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
        doc = db.collection("bots").document(bot_name).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        discord_cfg = (data.get("channels") or {}).get("discord") or {}
        return {
            "token": discord_cfg.get("token", ""),
            "enabled": bool(discord_cfg.get("voice_sidecar", False)),
            "guild_id": str(data.get("guild_id", "")),
            "voice_channel_id": str(discord_cfg.get("voice_channel_id", "")),
        }
    except Exception as e:
        log.warning("读取 Discord sidecar 配置失败 (non-fatal): %s", e)
        return None


async def _generate_tts(text: str) -> tuple[str, str]:
    """调 tts-generator skill 生成 ogg。返回 (ogg_path, error)。"""
    tts_script = os.path.expanduser(
        "~/CloseCrab/skills/tts-generator/scripts/tts-generate.py"
    )
    try:
        # --voice orus 对齐飞书 _tts_and_send_one 的音色, 两边听感一致
        proc = await asyncio.create_subprocess_exec(
            "python3", tts_script, text, "--voice", "orus",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return "", err.decode(errors="ignore")[:300]
        lines = [l.strip() for l in out.decode(errors="ignore").splitlines() if l.strip()]
        ogg_path = lines[-1] if lines else ""
        if not ogg_path or not os.path.exists(ogg_path):
            return "", "TTS 没产出音频文件"
        return ogg_path, ""
    except Exception as e:
        log.exception("TTS 生成异常")
        return "", str(e)


async def _resolve_voice_channel(bot, voice_channel_id: str):
    """解析常驻语音频道：优先配置的 id，缺省取 server 第一个语音频道。"""
    import discord

    if voice_channel_id:
        try:
            ch = bot.get_channel(int(voice_channel_id))
            if isinstance(ch, discord.VoiceChannel):
                return ch
        except (ValueError, TypeError):
            pass
    for g in bot.guilds:
        if g.voice_channels:
            return g.voice_channels[0]
    return None


async def _ensure_connected():
    """确保已连到常驻频道，返回 VoiceClient 或 None。"""
    bot = _sidecar_bot
    if bot is None or not bot.guilds:
        return None
    guild = bot.guilds[0]
    vc = guild.voice_client
    if vc is not None and vc.is_connected():
        return vc
    # 残留僵尸先清掉
    if vc is not None:
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
    ch = bot.get_channel(_target_voice_channel_id) if _target_voice_channel_id else None
    if ch is None:
        log.warning("常驻语音频道不可用 (id=%s)", _target_voice_channel_id)
        return None
    try:
        vc = await ch.connect(timeout=20.0, reconnect=True)
    except Exception:
        log.exception("常驻语音频道连接失败")
        return None
    for _ in range(50):  # 等握手 (UDP + DAVE) 真完成
        if vc.is_connected():
            break
        await asyncio.sleep(0.2)
    if vc and vc.is_connected():
        _patch_recv_hook_debug(vc)
    return vc if vc.is_connected() else None


def _patch_recv_hook_debug(vc):
    """Monkeypatch VoiceClient._recv_hook 加 Opcode 5 调试日志。"""
    from discord.voice.enums import OpCodes
    orig = vc._recv_hook
    async def _debug_hook(ws, msg):
        op = msg.get("op")
        if op == int(OpCodes.speaking):
            d = msg.get("d", {})
            log.info("[DEBUG] Voice WS Opcode 5 Speaking: user_id=%s speaking=%s ssrc=%s",
                     d.get("user_id"), d.get("speaking"), d.get("ssrc"))
        return await orig(ws, msg)
    vc._recv_hook = _debug_hook
    log.info("[DEBUG] VoiceClient._recv_hook 已 patch (追踪 Opcode 5)")


async def _activate_listen(vc) -> tuple:
    """起录音 + AgentSession + ssrc 推断循环。/listen 和心跳自动起共用。
    不依赖 slash ctx, 可被后台 task 调。返回 (是否成功, 说明)。"""
    global _stt_sink, _ssrc_task, _listen_active, _listen_vc, _listen_restart_n
    if vc is None or not vc.is_connected():
        return False, "未连接语音频道"
    if vc.is_recording():
        return True, "已经在收音"
    sink = _get_stt_sink_class()()
    sink.vc = vc  # 解码路径解析说话人要用 sink.vc, 手动补
    _stt_sink = sink
    _listen_vc = vc
    _listen_restart_n = 0
    try:
        # 复用发送那条 VoiceClient: start_recording 在共享 UDP socket 上加监听,
        # 不新建连接, 不影响 vc.play() 的 TTS 播放。
        vc.start_recording(sink, _on_recording_done)
    except Exception as e:
        log.exception("start_recording 失败")
        return False, f"start_recording 失败: {e}"
    _listen_active = True  # 录音被冲垮由守护循环自动重启
    try:
        await _start_agent_session(_target_voice_channel_id)
    except Exception as e:
        log.exception("AgentSession 启动失败")
        return False, f"AgentSession 启动失败: {e}"
    if _ssrc_task is None or _ssrc_task.done():
        _ssrc_task = asyncio.create_task(_ssrc_infer_loop())
    return True, "ok"


async def _voice_heartbeat(interval: float = 30.0):
    """后台心跳：周期性检查 voice 连接，掉线则自动爬回常驻频道。

    根因：Discord gateway 与 voice 是两条独立连接。半夜 websocket 1006 断线后
    gateway 会 RESUME，但 voice 连接不会自动重建 → 飞书 voice-summary 检测到
    is_voice_connected=False 就回退飞书 ogg，Discord 静音。这个心跳就是兜底。
    """
    global _autostart_done
    while True:
        try:
            await asyncio.sleep(interval)
            if not _target_voice_channel_id:
                continue
            bot = _sidecar_bot
            if bot is None or not bot.guilds:
                continue
            vc = bot.guilds[0].voice_client
            if vc is None or not vc.is_connected():
                log.warning("检测到 voice 掉线，尝试自动 rejoin 常驻频道…")
                vc = await _ensure_connected()
                if vc is not None:
                    log.info("voice 自动 rejoin 成功")
                else:
                    log.warning("voice 自动 rejoin 失败，下个周期再试")
                    continue
            # 连接健康 → 本进程内自动起一次录音 (重启/重连后接收自愈, 尊重之后的 /stoplisten)
            if _LISTEN_AUTOSTART and not _autostart_done and not _listen_active and not vc.is_recording():
                ok, msg = await _activate_listen(vc)
                _autostart_done = True
                log.info("[DAVE埋点] 自动收音启动: %s", "ok" if ok else msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("voice 心跳异常，继续下个周期")


async def _speak(text: str):
    """在 sidecar loop 里执行：确保连上常驻频道 → TTS → 等上一段念完 → play。"""
    import discord

    vc = await _ensure_connected()
    if vc is None:
        return
    ogg_path, err = await _generate_tts(text)
    if err:
        log.warning("Discord TTS 失败: %s", err)
        return
    for _ in range(300):  # 最多 ~60s 等上一段念完，避免叠音
        if not vc.is_playing():
            break
        await asyncio.sleep(0.2)
    try:
        vc.play(discord.FFmpegOpusAudio(ogg_path))
        log.info("Discord 念: %s", text[:50])
    except Exception:
        log.exception("Discord play 失败")


def speak_text(text: str) -> bool:
    """【飞书线程调用】把一段口语文本推到 Discord 常驻语音频道念。线程安全。

    sidecar 未启动 / loop 未就绪时静默返回 False，不抛异常、不阻塞调用方。
    """
    if not text or not text.strip():
        return False
    loop = _sidecar_loop
    if loop is None or _sidecar_bot is None:
        return False
    try:
        asyncio.run_coroutine_threadsafe(_speak(text), loop)
        return True
    except Exception:
        log.exception("speak_text 跨线程调度失败")
        return False


# ─── 流式直生路径 (替代文件式 speak_text, 低延迟) ──────────────────────────
# 不调 tts-generate.py skill / 不落盘 ogg / 不用 ffmpeg。直接学 livekit 那套
# 流式调 Gemini TTS (gemini_tts.py 同 model/voice/config), 边收 24kHz PCM 边
# resample 到 Discord 要的 48kHz stereo, 边推给一个流式 AudioSource。首帧延迟
# = Gemini 首个 chunk 到达 (~0.9s), 而非"等整段生成完"。


def is_voice_connected() -> bool:
    """sidecar 当前是否已连在某个语音频道 (供飞书线程判断"没连就免")。"""
    bot = _sidecar_bot
    if bot is None or not bot.guilds:
        return False
    vc = bot.guilds[0].voice_client
    return bool(vc is not None and vc.is_connected())


# —— 分批合成参数 (借鉴 livekit_io._batching_tts_loop, 因 jarvis 这台无
#    livekit/blingfire 依赖, 故移植算法而非字面 import) ——
#  实测 (probe): Gemini 流式 TTS 不是真 token 级流式, 它先啃完整段输入才出第一个
#  音, 首字时间 ∝ 输入长度: 9c→2.6s · 39c→3.8s · 77c→6s · ≥150c→7.8s(封顶)。
#  所以唯一压首字的杠杆 = 第一批只放少量字。又: 单次 ~500c 会吞结尾(568c 实测截断,
#  458c OK), 且单流偶发 server gRPC drop 会丢整段。分批同时解决三件事:
#    1) 第一批小 → 首字 ~3-4s (砍半);
#    2) 后续批 ≤ _MAX_BATCH_CHARS → 不触发吞尾;
#    3) 偶发 drop 只丢一批(≤200c≈36s)且断在句子边界 → 可廉价重合成(留后续)。
#  连续性: 生成速率 ~2.7x 实时, 第一批后 cushion 滚雪球, 批间 firstbyte 接缝被
#  前一批累积的 buffer 盖住; 欠载时 _StreamPCMSource 给静音帧不会断流。
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;…])\s*|\n+")  # 句末标点切句(标点留句尾), 不切逗号保 prosody
_SOLO_UNTIL_CHARS = 30    # 开头逐句单播, 累计播够这么多字之前每句独立成批(首字最快)
_RAMP_BATCH_CHARS = 90    # 单播后第一包上限: 首字 ~6s 能被前面 cushion 盖住, 不留空档
_MAX_BATCH_CHARS = 300    # 之后批上限: 300c 减少总批数避免 API 限流; 远低于 ~500c 吞尾阈值


def _plan_tts_batches(cleaned: str):
    """切句 → 三段渐进打包。
      阶段1 单播: 累计 <30c 时每句独立成批(首字最快, 接缝最小);
      阶段2 第一包: 凑够 30c 后剩余句子先打包成 ≤90c 一包(首字 ~6s 被 cushion 盖住);
      阶段3 大包: 此后每批 ≤200c(已有大 buffer, 放大减接缝 + 防吞尾)。

    动机(Chris 2026-06-01): 旧版第一批小但第二批吞掉后面全部 → 187c 批首字 7.8s
    出现 ~6s 空档。改成开头一句一句播建 cushion, 再用渐进上限让后续每批首字都
    被已播 buffer 盖住, 实测接缝从 6s → <0.6s。"""
    sents = [s for s in _SENT_SPLIT_RE.split(cleaned) if s and s.strip()]
    if not sents:
        return [cleaned] if cleaned.strip() else []
    batches = []
    acc, i = 0, 0
    # 阶段1: 逐句单播, 直到累计字数够 cushion
    while i < len(sents) and acc < _SOLO_UNTIL_CHARS:
        batches.append(sents[i])
        acc += len(sents[i])
        i += 1
    # 阶段2/3: 剩余句子渐进打包, 第一包用小 cap, 之后放大
    cur, cur_n, cap = [], 0, _RAMP_BATCH_CHARS
    for s in sents[i:]:
        if cur and cur_n + len(s) > cap:
            batches.append("".join(cur))
            cur, cur_n, cap = [s], len(s), _MAX_BATCH_CHARS
        else:
            cur.append(s)
            cur_n += len(s)
    if cur:
        batches.append("".join(cur))
    return batches


_tts_client_cache = None  # module-level singleton for HTTP keep-alive

async def _gemini_tts_stream(text: str):
    """分批流式调 Gemini TTS, 逐 chunk yield 24kHz mono s16 PCM bytes。

    复用 gemini_tts 的 client 构造 + 文本清洗 + config, 保证与飞书 ogg 同
    model/voice (orus→"Orus")。整段先切句打包成多批, 逐批 generate_content_stream,
    PCM 连续 yield (消费方 _gen_worker 无感知分批, ratecv state 跨批连续)。
    """
    global _tts_client_cache
    from google.genai import types as gt
    from .gemini_tts import _build_genai_client, _clean_text_for_tts

    cleaned = _clean_text_for_tts(text)
    if not cleaned.strip():
        return
    if _tts_client_cache is None:
        _tts_client_cache = _build_genai_client(None)
    client = _tts_client_cache
    model = os.environ.get("TTS_MODEL", "gemini-3.1-flash-tts-preview")
    voice = os.environ.get("DISCORD_TTS_VOICE", "Orus")  # 对齐飞书 --voice orus
    config = gt.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=gt.SpeechConfig(
            voice_config=gt.VoiceConfig(
                prebuilt_voice_config=gt.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
    )
    batches = _plan_tts_batches(cleaned)
    log.info("TTS 分批: %dc → %d 批 (首批 %dc)", len(cleaned), len(batches),
             len(batches[0]) if batches else 0)
    for idx, batch in enumerate(batches, 1):
        if idx > 1:
            await asyncio.sleep(2)  # 批间延迟防 API 限流
        last_finish = None
        got = 0
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                stream = await client.aio.models.generate_content_stream(
                    model=model, contents=batch, config=config
                )
                async for chunk in stream:
                    for cand in getattr(chunk, "candidates", None) or []:
                        fr = getattr(cand, "finish_reason", None)
                        if fr is not None:
                            last_finish = fr
                        content = getattr(cand, "content", None)
                        for part in getattr(content, "parts", None) or []:
                            inline = getattr(part, "inline_data", None)
                            if inline and inline.data:
                                got += len(inline.data)
                                yield bytes(inline.data)
                break  # success
            except Exception as exc:
                if attempt < max_retries:
                    log.warning("TTS 批 #%d/%d 失败(retry %d/%d): %s",
                                idx, len(batches), attempt + 1, max_retries, exc)
                    await asyncio.sleep(1 + attempt)
                else:
                    log.error("TTS 批 #%d/%d 最终失败，跳过(%dc): %s",
                              idx, len(batches), len(batch), exc)
        log.info("TTS 批 #%d/%d: %dc → %.1fs 音频 finish=%s",
                 idx, len(batches), len(batch), got / 2 / 24000, last_finish)


_EMOTION_TAG_RE = re.compile(r'\[(?:casually|friendly|warmly|amused|cheerfully|playful|'
                             r'thinking|realization|curiosity|confusion|contemplative|'
                             r'excitement|happy|seriously|suggestion|whispers|'
                             r'focus|neutral)\]\s*', re.IGNORECASE)

_EMOTION_INSTRUCT_MAP = {
    "thinking": "音高: 女性中高音区，语调富于变化. 语速: 语速快，像连珠炮一样边想边说. 音量: 正常交谈音量. 清晰度: 吐字清晰. 情绪: 脑子飞速运转，急切地分析. 语调: 快速起伏，像在飞速自言自语. 性格: 聪明敏捷，停不下来.",
    "realization": "音高: 女性中高音区，声音明亮上扬. 语速: 语速快，干脆利落. 音量: 正常偏大，笑声响亮. 清晰度: 吐字清晰. 情绪: 恍然大悟，兴奋脱口而出. 语调: 猛地上扬有力，转折明显. 性格: 反应极快，自信爽朗.",
    "curiosity": "音高: 女性中高音区，句末明显上扬. 语速: 语速快，急切. 音量: 正常交谈音量. 清晰度: 吐字清晰. 情绪: 充满好奇，迫不及待想知道. 语调: 疑问式上扬，期待感强烈. 性格: 好学求知，热情.",
    "casually": "音高: 女性中高音区，语调自然活泼. 语速: 语速明快，干脆利落. 音量: 正常交谈音量. 清晰度: 吐字清晰. 情绪: 轻松随意，开心自在. 语调: 随性自然，偶有上扬. 性格: 随和爽朗，外向开朗.",
    "excitement": "音高: 女性高音区，语调大幅上扬. 语速: 语速飞快，节奏紧凑. 音量: 较大，近乎喊叫. 清晰度: 吐字清晰有力. 情绪: 极度兴奋，控制不住的狂喜. 语调: 高亢爆发，充满感染力. 性格: 外向热烈，激情四射.",
    "seriously": "音高: 女性中高音区，语调稳定有力. 语速: 语速明快，节奏紧凑不拖沓. 音量: 正常偏大. 清晰度: 字字清晰有力. 情绪: 严肃认真，干练果断. 语调: 有力简洁，掷地有声. 性格: 专业干练，不啰嗦.",
    "whispers": "音高: 女性中音区，压低但明亮. 语速: 语速快，紧凑不拖. 音量: 较小，悄悄话. 清晰度: 吐字清晰. 情绪: 神秘兴奋，分享劲爆秘密. 语调: 压低但有张力和节奏. 性格: 机灵俏皮.",
    "playful": "音高: 女性中高音区，语调跳跃灵动. 语速: 语速快，节奏明快. 音量: 正常交谈音量，偶有笑声. 清晰度: 吐字清晰. 情绪: 俏皮调侃，带着得意的笑. 语调: 上下跳跃，活泼灵动. 性格: 幽默风趣，爱逗人.",
    "happy": "音高: 女性中高音区，语调上扬明亮. 语速: 语速快，欢快. 音量: 正常偏大，笑声爽朗. 清晰度: 吐字清晰. 情绪: 纯粹的快乐，笑意满溢. 语调: 明朗上扬，充满阳光. 性格: 乐观开朗，感染力强.",
    "warmly": "音高: 女性中高音区，语调柔和但明亮. 语速: 语速快，温暖但不拖沓. 音量: 正常交谈音量. 清晰度: 吐字清晰. 情绪: 温暖关怀，真诚亲切. 语调: 柔和上扬，带笑意. 性格: 体贴热情，干脆利落.",
    "contemplative": "音高: 女性中高音区，语调有层次起伏. 语速: 语速快，紧凑有节奏. 音量: 正常交谈音量. 清晰度: 吐字清晰. 情绪: 若有所悟，恍然感慨. 语调: 快速起伏有致，像突然想明白了. 性格: 有深度且反应快.",
    "friendly": "音高: 女性中高音区，语调温和上扬. 语速: 语速明快，热情. 音量: 正常交谈音量. 清晰度: 吐字清晰. 情绪: 热情友好，亲和力强. 语调: 亲切上扬，疑问时更明显. 性格: 热心开朗，爱交朋友.",
    "amused": "音高: 女性中高音区，语调带笑. 语速: 语速明快，忍不住加速. 音量: 正常交谈音量，笑声响亮. 清晰度: 吐字清晰. 情绪: 忍俊不禁，被逗乐了. 语调: 带笑意颤动，有感染力. 性格: 幽默爽朗.",
    "cheerfully": "音高: 女性中高音区，语调明快跳跃. 语速: 语速快，充满活力. 音量: 正常偏大. 清晰度: 吐字清晰. 情绪: 精力充沛，元气满满. 语调: 明亮跳跃，节奏感强. 性格: 活力四射，热情洋溢.",
    "suggestion": "音高: 女性中高音区，语调平稳但有力. 语速: 语速明快，简洁. 音量: 正常交谈音量. 清晰度: 吐字清晰. 情绪: 诚恳自信，有主意. 语调: 稳中有升，有说服力. 性格: 可靠干练，出谋划策.",
    "focus": "音高: 女性中高音区，语调精准有力. 语速: 语速明快，节奏紧凑. 音量: 正常交谈音量. 清晰度: 字字清晰. 情绪: 全神贯注，高效专注. 语调: 精确有条理，不废话. 性格: 专注严谨，利落.",
    "confusion": "音高: 女性中高音区，语调带疑问上扬. 语速: 语速明快，急切求解. 音量: 正常交谈音量. 清晰度: 吐字清晰. 情绪: 困惑但积极，想搞明白. 语调: 疑问上扬，不确定但不消极. 性格: 好奇求真，不服输.",
    "neutral": "音高: 女性中高音区，语调自然. 语速: 语速明快. 音量: 正常交谈音量. 清晰度: 吐字清晰，发音标准. 流畅度: 表达流畅自如. 口音: 普通话. 情绪: 平和自然. 语调: 语调上扬活泼. 性格: 外向开朗.",
}

_EMOTION_SPLIT_RE = re.compile(
    r'\[(casually|friendly|warmly|amused|cheerfully|playful|'
    r'thinking|realization|curiosity|confusion|contemplative|'
    r'excitement|happy|seriously|suggestion|whispers|'
    r'focus|neutral)\]\s*', re.IGNORECASE)


def _split_by_emotion(text: str) -> list:
    """按情感标签切分文本，返回 [(instructions, text_segment), ...]。
    pipeline 调用：每段用不同 instructions 调 Qwen3 TTS API。"""
    parts = _EMOTION_SPLIT_RE.split(text)
    segments = []
    if parts[0].strip():
        segments.append(("", parts[0].strip()))
    for i in range(1, len(parts), 2):
        tag = parts[i].lower()
        txt = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if txt:
            instruct = _EMOTION_INSTRUCT_MAP.get(tag, "")
            segments.append((instruct, txt))
    return segments if segments else [("", text)]


_qwen3_session = None  # requests.Session for HTTP keep-alive

async def _qwen3_tts_stream(text: str, instructions: str = ""):
    """流式调 Qwen3-TTS (vLLM-Omni), 逐 chunk yield 24kHz mono s16 PCM bytes。
    instructions: Qwen3 TTS instruct 参数，控制情感/语速/音高等。"""
    global _qwen3_session
    import json as _json

    from .gemini_tts import _clean_text_for_tts
    cleaned = _clean_text_for_tts(text)
    cleaned = _EMOTION_TAG_RE.sub('', cleaned).strip()
    if not cleaned:
        return

    qwen3_host = os.environ.get("QWEN3_TTS_HOST", "10.101.0.3")
    qwen3_port = os.environ.get("QWEN3_TTS_PORT", "8091")
    qwen3_voice = os.environ.get("QWEN3_TTS_VOICE", "vivian")
    url = f"http://{qwen3_host}:{qwen3_port}/v1/audio/speech"

    log.info("Qwen3 TTS: %dc → %s voice=%s instruct=%s",
             len(cleaned), url, qwen3_voice, instructions[:40] if instructions else "none")

    if _qwen3_session is None:
        import requests as _requests
        _qwen3_session = _requests.Session()

    def _blocking_stream():
        """同步流式读取, 复用 HTTP keep-alive session。"""
        body = {"model": "/model", "input": cleaned, "voice": qwen3_voice,
                "response_format": "pcm", "stream": True}
        if instructions:
            body["instructions"] = instructions
        resp = _qwen3_session.post(url, json=body,
            stream=True, timeout=120)
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=4800):
            if chunk:
                yield chunk
        resp.close()

    import queue
    pcm_q: queue.Queue = queue.Queue(maxsize=100)
    _sentinel = object()

    def _producer():
        try:
            for chunk in _blocking_stream():
                pcm_q.put(chunk)
        except Exception as exc:
            log.error("Qwen3 TTS 流失败: %s", exc)
        finally:
            pcm_q.put(_sentinel)

    import threading
    t = threading.Thread(target=_producer, daemon=True, name="qwen3-tts-stream")
    t.start()

    while True:
        try:
            item = pcm_q.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.01)
            continue
        if item is _sentinel:
            break
        yield item


_cloud_tts_client = None  # Cloud TTS gRPC client singleton

async def _cloud_tts_stream(text: str):
    """gRPC 双向流调 Cloud TTS (Chirp3-HD-Orus), 逐 chunk yield 24kHz mono s16 PCM."""
    global _cloud_tts_client
    from .gemini_tts import _clean_text_for_tts

    cleaned = _clean_text_for_tts(text)
    cleaned = _EMOTION_TAG_RE.sub('', cleaned).strip()
    if not cleaned:
        return

    if _cloud_tts_client is None:
        from google.cloud import texttospeech
        _cloud_tts_client = texttospeech.TextToSpeechClient()

    from google.cloud import texttospeech
    voice = texttospeech.VoiceSelectionParams(
        language_code="cmn-CN",
        name=os.environ.get("CLOUD_TTS_VOICE", "cmn-CN-Chirp3-HD-Orus"),
    )

    log.info("Cloud TTS streaming: %dc voice=%s", len(cleaned), voice.name)

    def _blocking_stream():
        def gen():
            yield texttospeech.StreamingSynthesizeRequest(
                streaming_config=texttospeech.StreamingSynthesizeConfig(voice=voice)
            )
            yield texttospeech.StreamingSynthesizeRequest(
                input=texttospeech.StreamingSynthesisInput(text=cleaned)
            )
        for resp in _cloud_tts_client.streaming_synthesize(gen()):
            if resp.audio_content:
                yield bytes(resp.audio_content)

    import queue
    pcm_q: queue.Queue = queue.Queue(maxsize=100)
    _sentinel = object()

    def _producer():
        try:
            for chunk in _blocking_stream():
                pcm_q.put(chunk)
        except Exception as exc:
            log.error("Cloud TTS 流失败: %s", exc)
        finally:
            pcm_q.put(_sentinel)

    t = threading.Thread(target=_producer, daemon=True, name="cloud-tts-stream")
    t.start()

    while True:
        try:
            item = pcm_q.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.01)
            continue
        if item is _sentinel:
            break
        yield item


_SOURCE_CLASS = None
_FILE_SOURCE_CLASS = None


def _get_source_class():
    """惰性定义 discord.AudioSource 子类 (延迟 import discord)。"""
    global _SOURCE_CLASS
    if _SOURCE_CLASS is not None:
        return _SOURCE_CLASS
    import discord

    class _StreamPCMSource(discord.AudioSource):
        """流式喂 48kHz/stereo/s16 PCM。read() 每 20ms 被 Discord 播放线程调一次。

        - buffer 够一帧 → 给真音频
        - buffer 不够且未结束 → 给静音帧 (保持流活着, 等下一 chunk; 这是 jitter
          buffer 的欠载兜底)
        - 已结束且 buffer 放空 → 返回 b'' 让 Discord 停止播放
        read() 必须快速非阻塞 (在播放线程里), 故用 Lock 护 bytearray, 不阻塞。
        """

        FRAME = 3840  # 20ms @ 48kHz * 2ch * 2bytes

        def __init__(self, fid: str = "", persistent: bool = False):
            self._buf = bytearray()
            self._lock = threading.Lock()
            self._finished = False
            self._persistent = persistent  # True: 永不停播，空 buffer 放静音帧
            self._written = 0   # 累计写入字节 (诊断 + 进度总长)
            self._real = 0      # 派发真音频帧数 (诊断 + 进度已播)
            self._silence = 0   # 派发欠载静音帧数 (诊断)
            self._fid = fid     # 进度条用: 标识这次播放对应哪个 buffer 文件

        def write(self, pcm: bytes):
            with self._lock:
                self._buf.extend(pcm)
                self._written += len(pcm)

        def buffered(self) -> int:
            with self._lock:
                return len(self._buf)

        def clear(self):
            """barge-in: 立刻丢掉未播缓冲 (下一帧 read 回落静音), 不结束流。"""
            with self._lock:
                self._buf.clear()

        def finish(self):
            with self._lock:
                if not self._persistent:
                    self._finished = True
                if self._fid:
                    _set_progress(self._fid, total=self._written)
                if self._persistent and self._written > 0:
                    log.info(
                        "持久 source 段结束: 写入 %d 字节(%.1fs), 真帧 %d, 静音帧 %d",
                        self._written, self._written / 4 / 48000,
                        self._real, self._silence)

        def read(self) -> bytes:
            with self._lock:
                if len(self._buf) >= self.FRAME:
                    out = bytes(self._buf[: self.FRAME])
                    del self._buf[: self.FRAME]
                    self._real += 1
                    if self._fid:
                        _set_progress(self._fid, played=self._real * self.FRAME,
                                      active=True)
                    return out
                if self._finished and not self._persistent:
                    if self._buf:
                        out = bytes(self._buf) + b"\x00" * (self.FRAME - len(self._buf))
                        self._buf.clear()
                        self._real += 1
                        return out
                    log.info(
                        "Discord 播放结束: 写入 %d 字节(%.1fs), 真音频帧 %d(%.1fs), "
                        "欠载静音帧 %d", self._written, self._written / 4 / 48000,
                        self._real, self._real * 0.02, self._silence)
                    if self._fid:
                        _set_progress(self._fid, played=self._written,
                                      total=self._written, active=False)
                    return b""
                self._silence += 1
                return b"\x00" * self.FRAME

        def is_opus(self) -> bool:
            return False

    _SOURCE_CLASS = _StreamPCMSource
    return _SOURCE_CLASS


_persistent_source = None
_tts_interrupted = False  # barge-in: LiveKit 打断时设 True，_do_speak 检查后停止生成

def _get_persistent_source():
    """获取或创建持久 source。vc.play() 只调一次，之后永不 stop。"""
    global _persistent_source
    bot = _sidecar_bot
    if bot is None or not bot.guilds:
        return None
    vc = bot.guilds[0].voice_client
    if vc is None or not vc.is_connected():
        _persistent_source = None
        return None
    if _persistent_source is not None:
        if vc.is_playing():
            return _persistent_source
        log.warning("持久 source 存在但 vc 未在播放，重建")
        _persistent_source = None
    _persistent_source = _get_source_class()(persistent=True)
    try:
        vc.play(_persistent_source)
        log.info("持久 source 已创建并开始播放")
    except Exception:
        log.exception("持久 source vc.play 失败")
        _persistent_source = None
    return _persistent_source


def _flush_hints_from_queue():
    """从 _speak_queue 中移除所有 pending hint，保留 reply。

    reply 入队时调用：结论已到，中间过程不用再念。
    """
    if _speak_queue is None:
        return 0
    kept: list[_SpeakItem] = []
    flushed = 0
    while not _speak_queue.empty():
        try:
            item = _speak_queue.get_nowait()
            if item.is_reply:
                kept.append(item)
            else:
                flushed += 1
        except asyncio.QueueEmpty:
            break
    for item in kept:
        _speak_queue.put_nowait(item)
    if flushed:
        log.info("TTS 队列清洗: 丢弃 %d 条过期 hint", flushed)
    return flushed


async def _do_speak(text: str, fid: str = "", backend: str = ""):
    """单条 TTS 生成+播放。直接写入持久 source，无需新建/抢占/预缓冲。"""
    import time as _time
    global _tts_interrupted
    t_start = _time.monotonic()
    source = _get_persistent_source()
    if source is None:
        return

    _tts_interrupted = False  # 新一轮生成，重置中断标志

    tts_backend = backend or os.environ.get("DISCORD_TTS_BACKEND", "gemini")

    buf_f = None
    bpath = _buf_path(fid)
    if bpath:
        try:
            os.makedirs(_BUF_DIR, exist_ok=True)
            buf_f = open(bpath, "wb")
        except Exception:
            log.exception("打开 buffer 落盘文件失败: %s", bpath)
            buf_f = None

    try:
        state = None
        wrote = 0
        t_first_pcm = None

        if tts_backend == "qwen3":
            segments = _split_by_emotion(text)
            log.info("Qwen3 TTS 分段: %d 段, %s", len(segments), text[:40])
            for seg_idx, (instruct, seg_text) in enumerate(segments):
                if _tts_interrupted:
                    log.info("TTS 被打断(barge-in), 停止生成: %dc已写, seg %d/%d",
                             wrote, seg_idx, len(segments))
                    break
                async for pcm24 in _qwen3_tts_stream(seg_text, instructions=instruct):
                    if _tts_interrupted:
                        break
                    if t_first_pcm is None:
                        t_first_pcm = _time.monotonic()
                        log.info("TTS 延迟: TTFB=%.0fms (text→首帧PCM), %dc, %s",
                                 (t_first_pcm - t_start) * 1000, len(text), text[:30])
                    pcm48, state = audioop.ratecv(pcm24, 2, 1, 24000, 48000, state)
                    stereo = audioop.tostereo(pcm48, 2, 1, 1)
                    source.write(stereo)
                    wrote += len(stereo)
                    if buf_f is not None:
                        buf_f.write(stereo)
        else:
            if tts_backend == "cloud_tts":
                tts_stream = _cloud_tts_stream(text)
            else:
                tts_stream = _gemini_tts_stream(text)
            async for pcm24 in tts_stream:
                if _tts_interrupted:
                    log.info("TTS 被打断(barge-in), 停止生成: %dc已写, %s", wrote, text[:30])
                    break
                if t_first_pcm is None:
                    t_first_pcm = _time.monotonic()
                    log.info("TTS 延迟: TTFB=%.0fms (text→首帧PCM), %dc, %s",
                             (t_first_pcm - t_start) * 1000, len(text), text[:30])
                pcm48, state = audioop.ratecv(pcm24, 2, 1, 24000, 48000, state)
                stereo = audioop.tostereo(pcm48, 2, 1, 1)
                source.write(stereo)
                wrote += len(stereo)
                if buf_f is not None:
                    buf_f.write(stereo)
        t_done = _time.monotonic()
        log.info("TTS 延迟: total=%.0fms, audio=%.1fs, %dc, %s",
                 (t_done - t_start) * 1000, wrote / 4 / 48000,
                 len(text), text[:30])
    except Exception:
        log.exception("流式 TTS 生成失败")
    finally:
        if buf_f is not None:
            try:
                buf_f.close()
            except Exception:
                pass
        if fid:
            _set_progress(fid, total=source._written, active=False)

    # 等本次写入的音频播完再返回（扣除生成期间已播放的时间）
    if wrote > 0 and not _tts_interrupted:
        play_dur = wrote / 4 / 48000
        elapsed = _time.monotonic() - t_start
        remain = play_dur - elapsed
        if remain > 0:
            await asyncio.sleep(remain)


async def _speak_consumer():
    """单 consumer loop: 从队列取 item，串行生成+播放。

    reply 入队时已清洗过期 hint (生产者侧)，consumer 这边再做一次兜底：
    取到 reply 时把队列里剩余 hint 也清掉（防 put 和 flush 之间的竞态窗口）。
    """
    import time as _time
    while True:
        item = await _speak_queue.get()
        if item.is_reply:
            _flush_hints_from_queue()
        queue_wait = (_time.monotonic() - item.enqueue_time) * 1000 if item.enqueue_time else 0
        if queue_wait > 50:
            log.info("TTS 排队等待: %.0fms, %s", queue_wait, item.text[:30])
        try:
            await _do_speak(item.text, item.fid, backend=item.backend)
        except Exception:
            log.exception("_speak_consumer: _do_speak 异常")


async def _enqueue_speak(text: str, fid: str = "", backend: str = ""):
    """sidecar loop 内: 把 TTS 请求入队。reply 入队前先清洗过期 hint。"""
    is_reply = bool(fid)
    if is_reply:
        _flush_hints_from_queue()
    import time as _time
    _speak_queue.put_nowait(_SpeakItem(text=text, fid=fid, is_reply=is_reply,
                                       enqueue_time=_time.monotonic(), backend=backend))
    tag = "reply" if is_reply else "hint"
    log.debug("TTS 入队 (%s, qsize=%d): %s", tag, _speak_queue.qsize(), text[:30])


def stream_speak_text(text: str, fid: str = "", backend: str = "") -> bool:
    """【飞书/LiveKit 线程调用】流式直生 TTS 推 Discord 常驻语音频道念。线程安全。

    未连语音频道 / sidecar 未启动 → 静默返回 False (不主动建连, 不费劲)。
    fire-and-forget: 立即返回, 不阻塞调用方。
    backend: 指定 TTS 后端 (qwen3/gemini/cloud_tts)，空=用默认。
    fid 非空时把整段音频落盘到 _buf_path(fid), 供后续 replay_file(fid) 重播。
    """
    if not text or not text.strip():
        return False
    loop = _sidecar_loop
    if loop is None or _sidecar_bot is None:
        return False
    if not is_voice_connected():
        return False
    if _speak_queue is None:
        return False
    try:
        asyncio.run_coroutine_threadsafe(_enqueue_speak(text, fid, backend=backend), loop)
        return True
    except Exception:
        log.exception("stream_speak_text 跨线程调度失败")
        return False


async def _set_pause(paused: bool) -> bool:
    """sidecar loop 内: 暂停/恢复当前 Discord 推流 (vc.pause/resume 同步原生 API)。

    暂停期间 _gen_worker 仍往 buffer 写, 不丢音; resume 后从断点继续念。
    返回是否真的对一个正在播放的流执行了操作。
    """
    bot = _sidecar_bot
    if bot is None or not bot.guilds:
        return False
    vc = bot.guilds[0].voice_client
    if vc is None or not vc.is_connected():
        return False
    if paused:
        if vc.is_playing():
            vc.pause()
            return True
        return False
    if vc.is_paused():
        vc.resume()
        return True
    return False


def pause_stream() -> bool:
    """【飞书线程调用】暂停 Discord 推流。线程安全。无播放中流 → False。"""
    loop = _sidecar_loop
    if loop is None or _sidecar_bot is None:
        return False
    try:
        fut = asyncio.run_coroutine_threadsafe(_set_pause(True), loop)
        return bool(fut.result(timeout=3))
    except Exception:
        log.exception("pause_stream 跨线程调度失败")
        return False


def resume_stream() -> bool:
    """【飞书线程调用】恢复 Discord 推流。线程安全。无暂停中流 → False。"""
    loop = _sidecar_loop
    if loop is None or _sidecar_bot is None:
        return False
    try:
        fut = asyncio.run_coroutine_threadsafe(_set_pause(False), loop)
        return bool(fut.result(timeout=3))
    except Exception:
        log.exception("resume_stream 跨线程调度失败")
        return False


# ─── 重播 (从落盘 buffer 文件回放整段) ──────────────────────────────────────


def _get_file_source_class():
    """惰性定义重播用 AudioSource (从 .pcm 文件按帧读, 同步更新进度)。"""
    global _FILE_SOURCE_CLASS
    if _FILE_SOURCE_CLASS is not None:
        return _FILE_SOURCE_CLASS
    import discord

    class _FilePCMSource(discord.AudioSource):
        """从落盘的 48k/stereo/s16 .pcm 文件按 20ms 帧读回放, 边读边更进度。

        文件已是 Discord 原生 PCM 格式 (生成时即落盘), 无需再 resample。
        read() 在播放线程被调, 必须快; 文件顺序读已足够快, 不另开缓冲线程。
        """

        FRAME = 3840  # 20ms @ 48kHz * 2ch * 2bytes

        def __init__(self, fid: str, path: str, total: int, start_byte: int = 0):
            self._fid = fid
            self._f = open(path, "rb")
            self._total = total
            if start_byte > 0:
                try:
                    self._f.seek(min(start_byte, total))
                except Exception:
                    start_byte = 0
                    self._f.seek(0)
            self._played = start_byte
            _set_progress(fid, played=start_byte, total=total, active=True)

        def read(self) -> bytes:
            chunk = self._f.read(self.FRAME)
            if not chunk:
                _set_progress(self._fid, played=self._total,
                              total=self._total, active=False)
                return b""
            self._played += len(chunk)
            _set_progress(self._fid, played=self._played, active=True)
            if len(chunk) < self.FRAME:  # 末帧补齐静音
                chunk = chunk + b"\x00" * (self.FRAME - len(chunk))
            return chunk

        def is_opus(self) -> bool:
            return False

        def cleanup(self):
            try:
                self._f.close()
            except Exception:
                pass

    _FILE_SOURCE_CLASS = _FilePCMSource
    return _FILE_SOURCE_CLASS


async def _replay(fid: str) -> bool:
    """sidecar loop 内: 停掉当前播放, 从 _buf_path(fid) 整段回放。"""
    path = _buf_path(fid)
    if not path or not os.path.exists(path):
        log.warning("重播失败: buffer 文件不存在 fid=%s", fid)
        return False
    bot = _sidecar_bot
    if bot is None or not bot.guilds:
        return False
    vc = bot.guilds[0].voice_client
    if vc is None or not vc.is_connected():
        return False
    if vc.is_playing() or vc.is_paused():
        vc.stop()  # 打断当前 (直播或上一次重播)
        for _ in range(40):  # 最多 ~2s 等 stop 落定
            if not vc.is_playing() and not vc.is_paused():
                break
            await asyncio.sleep(0.05)
    try:
        total = os.path.getsize(path)
    except OSError:
        return False
    try:
        source = _get_file_source_class()(fid, path, total)
        vc.play(source)
        log.info("重播开始 fid=%s (%.1fs)", fid, total / _PCM_BYTES_PER_SEC)
        return True
    except Exception:
        log.exception("重播 vc.play 失败 fid=%s", fid)
        return False


def replay_file(fid: str) -> bool:
    """【飞书线程调用】重播指定 fid 的整段音频。线程安全。"""
    loop = _sidecar_loop
    if loop is None or _sidecar_bot is None:
        return False
    if not is_voice_connected():
        return False
    try:
        fut = asyncio.run_coroutine_threadsafe(_replay(fid), loop)
        return bool(fut.result(timeout=5))
    except Exception:
        log.exception("replay_file 跨线程调度失败 fid=%s", fid)
        return False


async def _seek(fid: str, delta_frac: float) -> bool:
    """sidecar loop 内: 从当前播放位置按 delta_frac*总长 跳转 (正=前进, 负=倒退)。

    实现 = 打断当前播放 + 用 _FilePCMSource(start_byte=...) 从目标点重开。
    当前位置取自 _progress (须 fid 匹配, 直播/重播都更新它); 取不到则从头。
    目标点 clamp 到 [0, total] 并对齐 20ms 帧边界 (FRAME=3840) 防左右声道错位。
    """
    path = _buf_path(fid)
    if not path or not os.path.exists(path):
        log.warning("seek 失败: buffer 文件不存在 fid=%s", fid)
        return False
    bot = _sidecar_bot
    if bot is None or not bot.guilds:
        return False
    vc = bot.guilds[0].voice_client
    if vc is None or not vc.is_connected():
        return False
    try:
        total = os.path.getsize(path)
    except OSError:
        return False
    if total <= 0:
        return False
    with _progress_lock:
        played = _progress["played"] if _progress["fid"] == fid else 0
    step = int(total * delta_frac)
    start = max(0, min(total, played + step))
    start -= start % 3840  # 对齐帧边界
    if vc.is_playing() or vc.is_paused():
        vc.stop()  # 打断当前 (直播或上一次重播)
        for _ in range(40):  # 最多 ~2s 等 stop 落定
            if not vc.is_playing() and not vc.is_paused():
                break
            await asyncio.sleep(0.05)
    try:
        source = _get_file_source_class()(fid, path, total, start_byte=start)
        vc.play(source)
        log.info("seek fid=%s delta=%+.0f%% → %.1fs/%.1fs", fid, delta_frac * 100,
                 start / _PCM_BYTES_PER_SEC, total / _PCM_BYTES_PER_SEC)
        return True
    except Exception:
        log.exception("seek vc.play 失败 fid=%s", fid)
        return False


def rewind_file(fid: str, frac: float = 0.1) -> bool:
    """【飞书线程调用】把指定 fid 的播放位置往回跳 frac*总长。线程安全。"""
    loop = _sidecar_loop
    if loop is None or _sidecar_bot is None:
        return False
    if not is_voice_connected():
        return False
    try:
        fut = asyncio.run_coroutine_threadsafe(_seek(fid, -abs(frac)), loop)
        return bool(fut.result(timeout=5))
    except Exception:
        log.exception("rewind_file 跨线程调度失败 fid=%s", fid)
        return False


def forward_file(fid: str, frac: float = 0.1) -> bool:
    """【飞书线程调用】把指定 fid 的播放位置往前跳 frac*总长。线程安全。"""
    loop = _sidecar_loop
    if loop is None or _sidecar_bot is None:
        return False
    if not is_voice_connected():
        return False
    try:
        fut = asyncio.run_coroutine_threadsafe(_seek(fid, abs(frac)), loop)
        return bool(fut.result(timeout=5))
    except Exception:
        log.exception("forward_file 跨线程调度失败 fid=%s", fid)
        return False


# ─── 语音「接收」(STT): vc.start_recording → 连续 PCM → silero VAD → Gemini STT ──
# py-cord 2.8.0 + davey 已原生做完 DAVE/MLS 握手 + 逐人解密, 解密后 PCM 到 sink.write。
# 链路: vc.start_recording(_STTSink) → sink.write(每个语音包) → stereo 降 mono 入缓冲
# → _audio_pump_loop 每 20ms 取一帧(有真帧推真帧, 无则推静音帧)喂 _DiscordAudioInput
# → AgentSession(stt=GeminiSTT, vad=silero, 无 llm/tts) 内部 VAD 断句 + STT
# → user_input_transcribed(is_final) → 发频道文字区。
#
# 关键前提(py-cord 2.8.0): decrypt_rtp 只在 dave.ready 且 ssrc→uid 已映射时才写
# decrypted_data, 否则包在 reader 里被丢弃(连 decoder 都不建)。ssrc→uid 映射的正路
# 是 gateway speaking op → _add_ssrc; 兜底是下面的 decrypt_rtp 探针抓传输层实收 ssrc
# + 频道唯一真人时自动 _add_ssrc。


def _get_stt():
    """复用 livekit_io 的 _build_stt(), 统一 STT。

    读 Firestore livekit.stt_provider 配置 (跟 LiveKitVoiceIO.start 同源),
    而不是硬编码 chirp3_stream。这样 sidecar 和 LiveKit 用同一个 STT。
    """
    global _stt_engine
    if _stt_engine is None:
        provider = os.environ.get("STT_PROVIDER", "")
        if not provider:
            try:
                from google.cloud import firestore as _fs
                from ..constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
                _db = _fs.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
                bot_name = os.environ.get("BOT_NAME", "jarvis")
                doc = _db.collection("bots").document(bot_name).get()
                provider = (doc.to_dict() or {}).get("livekit", {}).get("stt_provider", "chirp3_stream")
            except Exception:
                provider = "chirp3_stream"
            os.environ["STT_PROVIDER"] = provider
        os.environ.setdefault("STT_PHRASE_BOOST", "1")
        log.info("sidecar STT provider: %s", provider)
        from .livekit_io import _build_stt
        _stt_engine = _build_stt()
    return _stt_engine


def _get_stt_sink_class():
    """惰性定义 discord.sinks.Sink 子类 (延迟 import discord)。

    py-cord 2.8.0 路径: PacketRouter 调 ``sink.write(data, source)``。``data.pcm``
    是解码后的 48kHz/16-bit/stereo PCM bytes; ``user`` 是 User/Member。覆写 write
    直取 data.pcm, 不走基类落盘逻辑(我们要实时流)。

    基类没定义 ``__sink_listeners__`` / ``walk_children`` / ``is_opus`` (半成品),
    SinkEventRouter / PacketDecoder 初始化会用到, 补空实现让其注册空集不崩;
    送 PCM 的 PacketRouter 是另一条独立路径, 照常进来。只动子类, 不 patch py-cord。
    """
    global _STT_SINK_CLASS
    if _STT_SINK_CLASS is not None:
        return _STT_SINK_CLASS
    import discord

    class _STTSink(discord.sinks.Sink):
        """按 user 累积 PCM。write 跑在 py-cord 解码线程, 故用 Lock 护缓冲。"""

        __sink_listeners__: list = []  # noqa: 让 SinkEventRouter 注册空集不崩

        def walk_children(self):
            return []

        def is_opus(self) -> bool:
            return False  # False = 要 PacketDecoder 把 opus 解成 PCM

        def __init__(self):
            super().__init__()
            self._lock = threading.Lock()
            self._pcm = bytearray()  # 所有说话人 mono PCM 混入同一条流(本步单人)
            self._last_name = "?"
            self._hits = 0  # write 被调次数 (诊断: 验证 receive 真有包进来)

        def write(self, data, user):
            pcm = getattr(data, "pcm", None)
            if not pcm:
                return
            name = getattr(user, "display_name", None) or getattr(
                user, "name", None) or str(getattr(user, "id", "?"))
            try:
                mono = audioop.tomono(pcm, 2, 0.5, 0.5)  # 48kHz stereo → mono
            except Exception:
                return
            with self._lock:
                self._hits += 1
                self._pcm.extend(mono)
                self._last_name = name
                cap = _MONO_FRAME_BYTES * 100
                if len(self._pcm) > cap:
                    del self._pcm[: len(self._pcm) - cap]
            _stt_ab_record_pcm(mono)
            _funasr_ab_feed(mono)

        def pop_frame(self):
            """取一帧 20ms mono PCM bytes, 不足一帧返回 None。"""
            with self._lock:
                if len(self._pcm) >= _MONO_FRAME_BYTES:
                    out = bytes(self._pcm[:_MONO_FRAME_BYTES])
                    del self._pcm[:_MONO_FRAME_BYTES]
                    return out
                return None

        def last_name(self) -> str:
            with self._lock:
                return self._last_name

        def hits(self) -> int:
            with self._lock:
                return self._hits

        def cleanup(self):
            self.finished = True  # 不往 audio_data 写, 覆写成空操作

    _STT_SINK_CLASS = _STTSink
    return _STT_SINK_CLASS


def _on_recording_done(exc):
    """start_recording 的结束回调 (录音停止/出错时被调)。出错时 _listen_active 仍开
    则由守护循环自动重启录音 (重启后 ssrc 通常已映射好)。"""
    if exc is not None:
        log.warning("voice 录音结束并带异常 (将由守护循环自动重启): %s", exc)
    else:
        log.info("voice 录音已停止")


def _get_audio_input_class():
    """惰性定义 livekit AudioInput 子类 (延迟 import livekit.agents)。

    AgentSession 通过 ``async for frame in audio_input`` 拉帧。覆写 ``__anext__``
    从队列取帧; source=None 时基类 on_attached/on_detached 已是 no-op。
    """
    global _AUDIO_INPUT_CLASS
    if _AUDIO_INPUT_CLASS is not None:
        return _AUDIO_INPUT_CLASS
    from livekit.agents.voice.io import AudioInput

    class _DiscordAudioInput(AudioInput):
        _MAX_Q = 25  # jitter buffer 上限 (~0.5s), 超限丢最旧防积压 burst 打乱 VAD

        def __init__(self):
            super().__init__(label="discord")
            self._q: asyncio.Queue = asyncio.Queue()

        async def __anext__(self):
            return await self._q.get()

        def feed_frame(self, frame):
            q = self._q
            if q.qsize() >= self._MAX_Q:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(frame)

    _AUDIO_INPUT_CLASS = _DiscordAudioInput
    return _AUDIO_INPUT_CLASS


_AUDIO_OUTPUT_CLASS = None


def _get_audio_output_class():
    """惰性定义 livekit AudioOutput 子类 (出口音频桥, 延迟 import)。

    AgentSession 的 TTS 帧通过 ``capture_frame`` 喂进来。我们声明 sample_rate=48000,
    livekit 会在喂之前自动把 GeminiTTS 的 24kHz 重采样成 48kHz, 这里只需 mono→stereo
    再写进一个常驻 _StreamPCMSource (vc.play 一次, 空档自动放静音帧不断流)。

      capture_frame: TTS 帧 → 48k stereo → source.write (首帧时起 vc.play)
      flush:         一段话说完 → 等 buffer 放空 → on_playback_finished(未打断)
      clear_buffer:  barge-in (用户插话) → source.clear + vc.stop → on_playback_finished(打断)
    """
    global _AUDIO_OUTPUT_CLASS
    if _AUDIO_OUTPUT_CLASS is not None:
        return _AUDIO_OUTPUT_CLASS
    from livekit.agents.voice.io import AudioOutput, AudioOutputCapabilities

    class _DiscordAudioOutput(AudioOutput):
        def __init__(self):
            super().__init__(
                label="discord",
                capabilities=AudioOutputCapabilities(pause=False),
                sample_rate=48000,  # 要 48k → livekit 替我把 TTS 24k 重采样好再喂
            )
            self._source = None          # 常驻 _StreamPCMSource
            self._playing = False        # vc.play 是否已起
            self._seg_frames = 0         # 当前 segment 已写帧数 (算 playback_position)
            self._flush_task = None

        def _ensure_source_playing(self):
            src = _get_persistent_source()
            if src is not None:
                self._source = src
                self._playing = True

        async def capture_frame(self, frame) -> None:
            await super().capture_frame(frame)  # 基类记 segment 计数
            self._ensure_source_playing()
            if self._source is None:
                return
            pcm = bytes(frame.data)
            ch = getattr(frame, "num_channels", 1)
            if ch == 1:
                pcm = audioop.tostereo(pcm, 2, 1, 1)  # mono → stereo
            self._source.write(pcm)
            self._seg_frames += 1
            if self._seg_frames % 50 == 1:
                log.info("LiveKit capture_frame #%d: %dB pcm, buf=%d, source=%s",
                         self._seg_frames, len(pcm),
                         self._source.buffered() if self._source else -1,
                         id(self._source))

        def flush(self) -> None:
            super().flush()
            played = self._seg_frames * 0.02
            self._seg_frames = 0
            # 持久 source 模式: 不等 buffer drain, 直接报完成。
            # buffer 一直在被 Discord 播放线程读, 不会丢; 等 drain 会阻塞 LiveKit
            # 的下一轮 TTS 生成 (AgentSession 等 on_playback_finished 才开始下一段)。
            self.on_playback_finished(playback_position=played, interrupted=False)

        async def _wait_drain_then_finish(self, src, played: float):
            try:
                for _ in range(3000):  # 最多 ~60s
                    if src.buffered() <= 0:
                        break
                    await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                return
            self.on_playback_finished(playback_position=played, interrupted=False)

        def clear_buffer(self) -> None:
            global _tts_interrupted
            if self._flush_task is not None and not self._flush_task.done():
                self._flush_task.cancel()
            _tts_interrupted = True  # 通知 sidecar _do_speak 停止生成
            src = _get_persistent_source()
            if src is not None:
                src.clear()
            played = self._seg_frames * 0.02
            self._seg_frames = 0
            self.on_playback_finished(playback_position=played, interrupted=True)

    _AUDIO_OUTPUT_CLASS = _DiscordAudioOutput
    return _AUDIO_OUTPUT_CLASS


_got_real_frame = False  # sink.write 直通喂真帧后置 True, 间隙填充器据此跳过

# ─── STT A/B 测试：录音 + 结果收集 ──────────────────────────────────────
_stt_ab_results = []
_stt_ab_seq = 0
_stt_ab_dir = ""
_stt_ab_pcm_buf = bytearray()
_stt_ab_pcm_lock = threading.Lock()
_STT_AB_MAX_PCM = 48000 * 2 * 30  # 最多缓存 30 秒 (48kHz mono s16)


def _stt_ab_record_pcm(mono_48k: bytes):
    """累积 48kHz mono PCM 到 buffer，供落盘用。"""
    with _stt_ab_pcm_lock:
        _stt_ab_pcm_buf.extend(mono_48k)
        if len(_stt_ab_pcm_buf) > _STT_AB_MAX_PCM:
            del _stt_ab_pcm_buf[:len(_stt_ab_pcm_buf) - _STT_AB_MAX_PCM]


def _stt_ab_save_utterance(chirp3_text: str, chirp3_t: float):
    """Chirp3 出 final transcript 时，落盘 WAV + 创建记录 + 触发 Gemini STT。"""
    global _stt_ab_seq, _stt_ab_dir
    import time as _time, wave, struct

    _stt_ab_seq += 1
    seq = _stt_ab_seq

    if not _stt_ab_dir:
        _stt_ab_dir = f"/tmp/stt-ab/{int(_time.time())}"
        os.makedirs(_stt_ab_dir, exist_ok=True)
        log.info("[STT-AB] 录音目录: %s", _stt_ab_dir)

    wav_path = os.path.join(_stt_ab_dir, f"{seq:03d}.wav")
    with _stt_ab_pcm_lock:
        pcm = bytes(_stt_ab_pcm_buf)
        _stt_ab_pcm_buf.clear()

    if len(pcm) < 4800:
        log.warning("[STT-AB] PCM 太短 (%dB)，跳过 seq=%d", len(pcm), seq)
        return

    try:
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(pcm)
        log.info("[STT-AB] 录音落盘: seq=%d %s (%.1fs)", seq, wav_path, len(pcm) / 96000)
    except Exception:
        log.exception("[STT-AB] WAV 写入失败")
        return

    record = {
        "seq": seq,
        "wav_path": wav_path,
        "audio_dur": len(pcm) / 96000,
        "chirp3": {"text": chirp3_text, "t": chirp3_t},
        "funasr_online": {"text": "", "t": 0},
        "funasr_offline": {"text": "", "t": 0},
        "gemini": {"text": "", "t": 0},
    }
    _stt_ab_results.append(record)

    # 异步触发 Gemini STT（批量模式，发完整 WAV）
    import threading
    def _gemini_stt():
        try:
            from closecrab.utils.stt import STTEngine
            engine = STTEngine()
            t0 = _time.monotonic()
            text = engine._transcribe_gemini(wav_path)
            t1 = _time.monotonic()
            record["gemini"] = {"text": text, "t": t1}
            log.info("[STT-AB] Gemini final: t=%.3f latency=%.0fms text=%r",
                     t1, (t1 - t0) * 1000, text[:80])
        except Exception as e:
            log.warning("[STT-AB] Gemini STT 失败: %s", e)
    threading.Thread(target=_gemini_stt, daemon=True, name=f"gemini-stt-{seq}").start()


def stt_ab_get_results():
    """外部调用：获取所有 A/B 测试结果。"""
    return list(_stt_ab_results)


def stt_ab_get_dir():
    """外部调用：获取录音目录。"""
    return _stt_ab_dir


# ─── FunASR 离线 batch STT (PTT 驱动) ─────────────────────────────────
_funasr_model = None       # Paraformer 离线模型 (懒加载)
_funasr_punc_model = None  # 标点模型
_funasr_itn = None         # ITN 逆文本标准化 (数字/日期/百分比)
_funasr_is_primary = True  # FunASR 作为主力 STT 驱动 LLM
_funasr_speaking = False   # PTT 说话状态
_funasr_pcm_buf = bytearray()  # PTT 期间攒 16kHz mono s16 PCM

def _funasr_ensure_model():
    """懒加载 Paraformer 离线模型 + 标点模型 + ITN。首次调用约 15s。"""
    global _funasr_model, _funasr_punc_model, _funasr_itn
    if _funasr_model is not None:
        return _funasr_model
    try:
        from funasr import AutoModel
        from .chirp_phrases import default_phrases
        hotwords_lines = []
        for phrase, boost in default_phrases():
            w = int(boost) if boost else 10
            if len(phrase) <= 20:
                hotwords_lines.append(f"{phrase} {w}")
        hotwords_str = "\n".join(hotwords_lines)
        _funasr_model = AutoModel(
            model="iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            device="cpu", disable_pbar=True, disable_log=True, disable_update=True,
        )
        _funasr_punc_model = AutoModel(
            model="iic/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727",
            device="cpu", disable_pbar=True, disable_log=True, disable_update=True,
        )
        try:
            from itn.chinese.inverse_normalizer import InverseNormalizer
            _funasr_itn = InverseNormalizer()
            log.info("[FunASR] ITN 已加载")
        except Exception:
            log.warning("[FunASR] ITN 加载失败 (数字/日期不转换)")
        log.info("[FunASR] 离线模型已加载 (Paraformer + Punc + ITN), 热词 %d 个", len(hotwords_lines))
        _funasr_model._hotwords = hotwords_str
        return _funasr_model
    except Exception:
        log.exception("[FunASR] 模型加载失败")
        return None


def _funasr_recognize(pcm_16k: bytes) -> str:
    """在工作线程里跑 Paraformer 离线识别。输入 16kHz mono s16 PCM，返回文字。"""
    import numpy as np
    model = _funasr_ensure_model()
    if model is None:
        return ""
    audio = np.frombuffer(pcm_16k, dtype=np.int16).astype(np.float32) / 32768.0
    if len(audio) < 1600:  # <100ms 太短
        return ""
    import time as _time
    t0 = _time.monotonic()
    kwargs = {}
    hw = getattr(model, "_hotwords", "")
    if hw:
        kwargs["hotword"] = hw
    result = model.generate(input=audio, batch_size_s=300, **kwargs)
    text = ""
    if result and len(result) > 0:
        text = result[0].get("text", "").strip()
    if text and _funasr_punc_model is not None:
        punc_result = _funasr_punc_model.generate(input=text)
        if punc_result and len(punc_result) > 0:
            text = punc_result[0].get("text", text).strip()
    if text and _funasr_itn is not None:
        try:
            text = _funasr_itn.normalize(text)
        except Exception:
            pass
    dt = (_time.monotonic() - t0) * 1000
    log.info("[FunASR] 离线识别: %.0fms, %.1fs音频 → %s", dt, len(audio)/16000, text[:80])
    return text


def _on_discord_speaking_stop():
    """PTT 松手 (Opcode 5 speaking=0)：攒好的 PCM 一次性跑离线识别 → 送 LLM。"""
    global _funasr_pcm_buf, _funasr_speaking
    _funasr_speaking = False
    _stt_ab_save_utterance("", 0)

    pcm = bytes(_funasr_pcm_buf)
    _funasr_pcm_buf = bytearray()
    if len(pcm) < 3200:  # <100ms
        log.info("[FunASR] PTT 松手但音频太短 (%d bytes)", len(pcm))
        return
    log.info("[FunASR] PTT 松手 → 离线识别 %.1fs 音频", len(pcm) / 2 / 16000)

    def _recognize_and_send():
        text = _funasr_recognize(pcm)
        if not text:
            return
        log.info("[FunASR→LLM] → %s", text[:120])
        session = _agent_session
        if session is None or not _funasr_is_primary:
            return
        from .livekit_io import _closecrab_llm_instance
        llm_inst = _closecrab_llm_instance()
        if llm_inst is not None:
            llm_inst._skip_next_debounce = True
        loop = _sidecar_loop
        if loop is not None:
            def _do_gen(s=session, t=text):
                s.generate_reply(user_input=t)
            loop.call_soon_threadsafe(_do_gen)
            ch = _sidecar_bot.get_channel(_target_voice_channel_id) if _sidecar_bot else None
            if ch is not None:
                import asyncio
                loop.call_soon_threadsafe(
                    lambda c=ch, t=text: asyncio.ensure_future(c.send(f"🎤 FunASR: {t[:1900]}"))
                )

    import threading
    threading.Thread(target=_recognize_and_send, daemon=True, name="funasr-recognize").start()


def _funasr_ab_feed(mono_48k: bytes):
    """把 48kHz mono PCM 降采样到 16kHz 攒入 buffer。PTT 松手时一次性识别。"""
    global _funasr_speaking
    if not _funasr_is_primary:
        return
    if not _funasr_speaking:
        _funasr_speaking = True
        log.info("[FunASR] PTT 按下 → 开始攒音频")
    try:
        mono_16k, _ = audioop.ratecv(mono_48k, 2, 1, 48000, 16000, None)
        _funasr_pcm_buf.extend(mono_16k)
    except Exception:
        pass

async def _audio_pump_loop():
    """间隙填充: Discord 不说话时不发帧, 但 LiveKit VAD 需要连续流才能量静音断句。
    每 20ms 检查: sink.write 刚喂过真帧 → 跳过; 没有 → 补一帧静音。
    真帧由 sink.write 经 call_soon_threadsafe 直通 LiveKit 输入, 本循环只管补静音。
    配合起来 = WebRTC 的连续音频轨道 (有声时真帧、无声时静音, 全由 RTP 节奏驱动)。
    """
    from livekit import rtc

    silence = b"\x00" * _MONO_FRAME_BYTES
    log.info("间隙填充已启动 (仅静音补帧, 真帧由 sink.write 直通)")

    def _mk_silence():
        return rtc.AudioFrame(
            data=silence, sample_rate=48000, num_channels=1,
            samples_per_channel=_MONO_FRAME_SAMPLES,
        )

    try:
        while True:
            await asyncio.sleep(_MONO_FRAME_MS / 1000)
            ai = _audio_input
            if ai is None:
                continue
            global _got_real_frame
            if _got_real_frame:
                _got_real_frame = False  # 真帧已由 sink.write 直通, 本 tick 不补
            else:
                ai.feed_frame(_mk_silence())  # 无真帧: 补静音让 VAD 量出静音断句
    except asyncio.CancelledError:
        log.info("音频 pump 已取消")
        raise


async def _start_agent_session(channel_id: int):
    """装配 AgentSession (仅 LLM 路由, STT 由 FunASR 独立驱动)。

    FunASR offline 通过 PTT Opcode 5 断句, 结果调 session.generate_reply()
    驱动 CloseCrabLLM → TTS → Discord 喇叭。Chirp 3 STT 已停用。"""
    from livekit.agents import Agent, AgentSession

    global _audio_input, _agent_session, _audio_pump_task, _audio_output

    bot = _sidecar_bot

    llm = tts = None
    full_duplex = _feishu_ref is not None and _feishu_loop is not None and bool(_feishu_open_id)
    if full_duplex:
        try:
            from .livekit_io import CloseCrabLLM
            from .gemini_tts import GeminiTTS
            voice = os.environ.get("DISCORD_TTS_VOICE", "Orus")
            model = os.environ.get("TTS_MODEL", "gemini-3.1-flash-tts-preview")
            llm = CloseCrabLLM(_feishu_ref, _feishu_loop, _feishu_open_id)
            tts = GeminiTTS(model=model, voice=voice)
        except Exception:
            log.exception("装配 CloseCrabLLM/GeminiTTS 失败")
            full_duplex = False
            llm = tts = None

    # 无 STT/VAD — FunASR 独立处理语音识别, 通过 generate_reply() 驱动 LLM
    session = AgentSession(llm=llm, tts=tts) if full_duplex else AgentSession()

    # AudioInput 仍需创建 (AgentSession.start 要求), 但不再喂真实音频
    ai = _get_audio_input_class()()
    session.input.audio = ai
    ao = None
    if full_duplex:
        ao = _get_audio_output_class()()
        session.output.audio = ao

    await session.start(Agent(instructions=" "))
    _audio_input = ai
    _audio_output = ao
    _agent_session = session
    _audio_pump_task = None  # 不再需要静音填充 (Chirp 3 已停)
    log.info("AgentSession 已启动 (channel=%s, FunASR 主力 STT, %s)",
             channel_id, "LLM+TTS" if full_duplex else "仅路由")

    if _pending_discord_text:
        log.info("回放 %d 条缓存的 Discord 文字消息", len(_pending_discord_text))
        for buffered in _pending_discord_text:
            from .livekit_io import _closecrab_llm_instance
            llm = _closecrab_llm_instance()
            if llm is not None:
                llm._skip_next_debounce = True
            session.generate_reply(user_input=buffered)
            log.info("Discord 文字回放 → AgentSession.generate_reply: %s", buffered[:80])
        _pending_discord_text.clear()


class _CommitWelcome:
    """替身 davey.CommitWelcome —— gateway 用 isinstance(result, davey.CommitWelcome)
    判断 process_proposals 的返回。dave-py 的 process_proposals 直接返回单个 blob
    (commit+welcome 已拼好), 故 commit=整块 blob, welcome=b'' —— gateway 见 welcome
    为空只发 result.commit (即整块), 正好。"""

    __slots__ = ("commit", "welcome")

    def __init__(self, commit: bytes, welcome: bytes = b""):
        self.commit = commit
        self.welcome = welcome


class DaveSessionAdapter:
    """把 py-cord 期望的 davey.DaveSession 接口, 适配到 dave-py 的
    Session + Encryptor + 逐用户 Decryptor 三件套。

    py-cord 对 session 的全部调用 (grep 实测的契约):
      构造  DaveSession(version, user_id, channel_id)            (state.py:921)
      .reinit(version, user_id, channel_id)                       (state.py:917)
      .reset()                                                    (state.py:932)
      .set_passthrough_mode(True, 10)                             (state.py:933/974, gw:230)
      .get_serialized_key_package() -> bytes                      (state.py:929)
      .set_external_sender(bytes)                                 (gw:270)
      .get_user_ids()                          (debug log only)   (gw:273)
      .process_proposals(op_type, bytes) -> CommitWelcome|None    (gw:277)
      .process_commit(bytes)   抛异常→recover                     (gw:301)
      .process_welcome(bytes)  抛异常→recover                     (gw:322)
      .decrypt(user_id, MediaType.audio, bytes) -> bytes          (reader:280/300/341)
      .encrypt_opus(bytes) -> bytes            (发送路径!!)        (client.py:421)
      .ready  (property bool)                  (发送+接收门)       (client.py:421, reader)
      .voice_privacy_code (property)                              (client.py:370)

    ⚠️ encrypt_opus 在发送 (TTS 播放) 路径上, 任何异常都回落明文, 绝不让 TTS 崩。
    """

    def __init__(self, version, user_id, channel_id):
        import dave  # 懒导入: dave-py 未装的 bot import sidecar 不应崩
        self._dave = dave
        self._MT_AUDIO = dave.MediaType.audio
        self._version = version
        self._user_id = user_id
        self._channel_id = channel_id
        self._self_key = str(user_id)
        self._state = None                 # 由 _install 的 reinit patch 注入 (拿 ssrc + 成员名单)
        self._decryptors: dict = {}        # user_id(str) -> dave.Decryptor
        self._dec_fail: dict = {}          # user_id(str) -> 连续解密失败计数 (重拉 ratchet 用)
        self._dec_ok: dict = {}            # user_id(str) -> 累计解密成功计数 (区分哪个 sender 通)
        self._dec_has_ratchet: set = set() # user_id(str) -> 已成功 transition 过 ratchet (防逐帧重拉)
        self._enc_frames = 0               # 埋点: 发送帧计数 (查断流用)
        self._rekeys = 0                   # 埋点: ratchet 刷新次数 (中途换钥匙=可能断流)
        self._roster: set = set()          # commit/welcome 返回的权威群名单 (str user_id), 入 recognized_set
        self._sess = dave.Session()
        self._sess.init(version, int(channel_id), str(user_id))
        self._enc = dave.Encryptor()
        log.info("DaveSessionAdapter 已建 (version=%s group=%s self=%s)",
                 version, channel_id, user_id)

    # ── 成员名单 (MLS recognized set): dave-py process_* 要传 ──
    def _recognized_set(self) -> set:
        ids = {self._self_key}
        # 权威源 1: commit/welcome 返回的群名单 (一旦进树就常驻, 不受缓存/时序影响)
        ids |= self._roster
        st = self._state
        try:
            if st is not None:
                ch = None
                try:
                    ch = st.guild.get_channel(int(self._channel_id)) if st.guild else None
                except Exception:
                    ch = None
                if ch is not None:
                    # 权威源 2: voice_states (VOICE_STATE_UPDATE 维护, 不需 members 特权 intent)。
                    # 关键修复 (2026-06-02): 原来只用 ch.members, 但 bot 跑 Intents.default() 不含
                    # members 特权 intent → ch.members 空/陈旧 → 新进频道的人 (如 Chris) 在 proposals
                    # 到达时不在 recognized_set → 其 add 被 dave-py 拒 → 永不进 MLS 树 → 解密全失败。
                    # voice_states 是语音频道真实在场名单, 才是对的源。members 保留做并集兜底。
                    for uid in (getattr(ch, "voice_states", None) or {}).keys():
                        ids.add(str(uid))
                    for m in getattr(ch, "members", []) or []:
                        ids.add(str(m.id))
                for uid in (getattr(st, "ssrc_user_map", {}) or {}).values():
                    ids.add(str(uid))
        except Exception:
            log.exception("_recognized_set 计算失败 (回落仅自己)")
        return ids

    # ── ratchet 刷新: 群密钥每次 epoch 变更后重新拉 (commit/welcome/transition 后) ──
    def _refresh_ratchets(self):
        recognized = self._recognized_set()
        self._rekeys += 1
        log.info("[DAVE埋点] _refresh_ratchets #%d: 成员=%d 名单=%s 已发帧=%d (中途换钥匙可能断流)",
                 self._rekeys, len(recognized), sorted(recognized), self._enc_frames)
        for uid in recognized:
            try:
                r = self._sess.get_key_ratchet(uid)
            except Exception:
                log.exception("get_key_ratchet(%s) 失败", uid)
                continue
            log.info("[DAVE埋点] get_key_ratchet(%s) → %s%s",
                     uid, "None(不在MLS树)" if r is None else "有ratchet",
                     " [自己→encryptor]" if uid == self._self_key else " [他人→decryptor]")
            if r is None:
                continue
            if uid == self._self_key:
                try:
                    self._enc.set_key_ratchet(r)
                except Exception:
                    log.exception("set 自己 encryptor ratchet 失败")
            else:
                dec = self._decryptors.get(uid)
                if dec is None:
                    dec = self._dave.Decryptor()
                    self._decryptors[uid] = dec
                try:
                    dec.transition_to_key_ratchet(r, transition_expiry=10.0)
                    # epoch 真变更时这是合法的重 transition; 标记后 decrypt() 不再逐帧重拉
                    self._dec_has_ratchet.add(uid)
                except Exception:
                    log.exception("transition decryptor(%s) ratchet 失败", uid)

    # ── MLS 生命周期 ──
    def reinit(self, version, user_id, channel_id):
        self._version = version
        self._user_id = user_id
        self._channel_id = channel_id
        self._self_key = str(user_id)
        try:
            self._sess.reset()
        except Exception:
            log.exception("reinit: session.reset 失败 (继续 init)")
        self._sess.init(version, int(channel_id), str(user_id))
        self._decryptors.clear()
        self._dec_fail.clear()
        self._dec_has_ratchet.clear()
        self._enc = self._dave.Encryptor()
        self._enc_frames = 0
        self._rekeys = 0
        log.info("DaveSessionAdapter.reinit (version=%s group=%s self=%s)",
                 version, channel_id, user_id)

    def reset(self):
        try:
            self._sess.reset()
        except Exception:
            log.exception("reset 失败")
        self._decryptors.clear()
        self._dec_fail.clear()
        self._dec_has_ratchet.clear()
        try:
            self._enc = self._dave.Encryptor()
        except Exception:
            pass

    def set_passthrough_mode(self, passthrough, expiry=10):
        try:
            self._enc.set_passthrough_mode(bool(passthrough))
        except Exception:
            log.exception("encryptor.set_passthrough_mode 失败")
        for dec in list(self._decryptors.values()):
            try:
                dec.transition_to_passthrough_mode(bool(passthrough), float(expiry))
            except Exception:
                pass

    def get_serialized_key_package(self) -> bytes:
        return self._sess.get_marshalled_key_package()

    def set_external_sender(self, data):
        self._sess.set_external_sender(bytes(data))

    def get_user_ids(self):
        return list(self._recognized_set())

    def process_proposals(self, op_type, proposals):
        # 关键修复 (2026-06-02): dave-py 的 process_proposals 期望 proposals 字节**带前导 optype
        # 字节** (daveprotocol 线格式 opcode27 = [optype:1B][MLS proposals...])。py-cord 的 davey
        # 后端把 optype 拆成单独枚举、只把 msg[4:] 当 proposals 传进来; 换 dave-py 后端必须把这个
        # 字节补回去, 否则 dave-py 把首个 MLS 字节当 boolean 解析 → "Malformed boolean" → 成员
        # 永远进不了 MLS 树 → 全程解密失败。只动接收, 与 TTS 发送无关。
        try:
            ot = op_type if isinstance(op_type, int) else int(getattr(op_type, "value", 0))
        except Exception:
            ot = 0
        ot = 0 if ot == 0 else 1
        rec = self._recognized_set()
        raw = bytes(proposals)
        # 主路径: 带前导 optype 字节; 若失败 (异常/None) 回退到无前导 (老行为), 记录哪种生效。
        for tag, payload in (("带前导", bytes([ot]) + raw), ("无前导", raw)):
            try:
                blob = self._sess.process_proposals(payload, rec)
            except Exception as e:
                log.warning("[DAVE埋点] process_proposals(%s optype=%d in=%dB) 抛错: %s",
                            tag, ot, len(payload), e)
                continue
            log.info("[DAVE埋点] process_proposals(%s optype=%d in=%dB 成员=%d) → blob=%s",
                     tag, ot, len(payload), len(rec),
                     ("%dB" % len(bytes(blob))) if blob is not None else "None")
            if blob is not None:
                return _CommitWelcome(bytes(blob), b"")
        return None

    def process_commit(self, commit):
        # dave-py 不抛异常: RejectType=失败。抛出去让 py-cord 走 recover_dave_from_invalid_commit
        # (发 invalid_commit_welcome + 重发 key package), 与 davey 的 except 流程一致。
        result = self._sess.process_commit(bytes(commit))
        if isinstance(result, self._dave.RejectType):
            log.warning("[DAVE埋点] process_commit REJECTED: %s (in=%dB)", result, len(bytes(commit)))
            raise RuntimeError(f"MLS commit rejected: {result}")
        self._capture_roster(result, "commit")
        log.info("[DAVE埋点] process_commit OK (in=%dB roster=%s) → 刷新 ratchet, 已发帧=%d",
                 len(bytes(commit)), sorted(self._roster), self._enc_frames)
        self._refresh_ratchets()
        return result

    def process_welcome(self, welcome):
        result = self._sess.process_welcome(bytes(welcome), self._recognized_set())
        if result is None:
            log.warning("[DAVE埋点] process_welcome REJECTED (in=%dB)", len(bytes(welcome)))
            raise RuntimeError("MLS welcome rejected")
        self._capture_roster(result, "welcome")
        log.info("[DAVE埋点] process_welcome OK (in=%dB roster=%s) → 刷新 ratchet, 已发帧=%d",
                 len(bytes(welcome)), sorted(self._roster), self._enc_frames)
        self._refresh_ratchets()
        return result

    def _capture_roster(self, result, src):
        # dave-py process_commit/welcome 返回 dict[int,list[int]] = epoch 后群名单
        # (user_id → leaf/sender-key indices)。keys 即权威 recognized set, 常驻进 self._roster。
        try:
            if isinstance(result, dict):
                for k in result.keys():
                    self._roster.add(str(k))
        except Exception:
            log.exception("_capture_roster(%s) 失败", src)

    # ── 给某 Decryptor 拉群密钥 ratchet (拉到才算成功) ──
    def _try_set_decryptor_ratchet(self, key, dec) -> bool:
        try:
            r = self._sess.get_key_ratchet(key)
        except Exception:
            return False
        if r is None:
            return False
        try:
            dec.transition_to_key_ratchet(r, transition_expiry=10.0)
            return True
        except Exception:
            return False

    # ── 收 (接收路径): py-cord 传 user_id, 路由到该用户的 Decryptor ──
    # 关键修复 (2026-06-02 v2): dave-py Decryptor 每 epoch 只需 transition 一次 ratchet,
    # 之后内部沿 HKDF 链**自增 generation** 解每帧。旧版在每帧解密失败时重拉 ratchet,
    # 把 generation 基线打回原点 → 真语音解到 ~25 帧后持续 GCM 认证失败、永远续不上
    # ("没续上语言流")。这里改成: 每个 sender 只在首次 (或 epoch 刷新) transition 一次,
    # 逐帧失败只计数+深诊, 绝不重拉。epoch 真变更由 _refresh_ratchets 统一重 transition。
    # 只动接收, 与 TTS 发送无关。
    def decrypt(self, user_id, media_type, data):
        key = str(user_id)
        dec = self._decryptors.get(key)
        if dec is None:
            dec = self._dave.Decryptor()
            self._decryptors[key] = dec
        # 一次性拉 ratchet (修首包早于 welcome 的竞态): 仅当本 sender 还没成功 transition 过。
        # 拉到才标记, 拉不到 (还没进 MLS 树) 留待下帧或 _refresh_ratchets 补。绝不逐帧重拉。
        if key not in self._dec_has_ratchet:
            if self._try_set_decryptor_ratchet(key, dec):
                self._dec_has_ratchet.add(key)
        try:
            out = dec.decrypt(self._MT_AUDIO, bytes(data))
            if out is not None:
                self._dec_fail.pop(key, None)
                ns = self._dec_ok.get(key, 0) + 1
                self._dec_ok[key] = ns
                if ns <= 3 or ns % 500 == 0:
                    log.info("[DAVE埋点] decrypt(%s) 成功#%d: in=%dB out=%dB 头=%s (真Opus)",
                             key, ns, len(bytes(data)), len(bytes(out)), bytes(out)[:4].hex())
                return bytes(out)
        except Exception:
            pass
        n = self._dec_fail.get(key, 0) + 1
        self._dec_fail[key] = n
        # 深诊 (前 5 次失败 + 之后每 200): 用 DecryptorStats 区分故障类别 ——
        # miss_key>0 = ratchet/leaf 不对 (MLS 同步问题); bad_nonce>0 = 帧格式/nonce 不对;
        # 仅 fail 增长 = GCM 认证失败 (钥匙错/epoch 错)。帧头尾用于核对 DAVE trailer。
        if n <= 5 or n % 200 == 0:
            try:
                st = dec.get_stats(self._MT_AUDIO)
                d = bytes(data)
                log.warning(
                    "[DAVE深诊] decrypt(%s) 失败#%d 群建立=%s 有ratchet=%s | 帧 in=%dB 头=%s 尾=%s | "
                    "stats success=%d fail=%d miss_key=%d bad_nonce=%d attempts=%d",
                    key, n, self._sess.has_established_group(), key in self._dec_has_ratchet,
                    len(d), d[:8].hex(), d[-8:].hex(),
                    st.decrypt_success_count, st.decrypt_failure_count,
                    st.decrypt_missing_key_count, st.decrypt_invalid_nonce_count,
                    st.decrypt_attempts)
            except Exception:
                log.exception("[DAVE深诊] 取 stats 失败")
        return b""

    # ── 发 (发送路径!!): 任何异常回落明文, 绝不崩 TTS ──
    def encrypt_opus(self, data):
        self._enc_frames += 1
        try:
            ssrc = int(getattr(self._state, "ssrc", 0) or 0) if self._state else 0
            out = self._enc.encrypt(self._MT_AUDIO, ssrc, bytes(data))
            if out is not None:
                if self._enc_frames % 50 == 1:
                    log.info("[DAVE埋点] encrypt_opus 帧#%d: ssrc=%s in=%dB out=%dB 密文OK ready=%s",
                             self._enc_frames, ssrc, len(bytes(data)), len(bytes(out)), self.ready)
                return bytes(out)
            # encrypt 返回 None: 没 ratchet / passthrough → 回落明文
            if self._enc_frames % 50 == 1:
                log.warning("[DAVE埋点] encrypt_opus 帧#%d: ssrc=%s encrypt()返回None→回落明文 ready=%s "
                            "has_ratchet=%s", self._enc_frames, ssrc, self.ready,
                            self._safe_has_ratchet())
        except Exception:
            log.exception("[DAVE埋点] encrypt_opus 帧#%d 异常, 回落明文 (TTS 可能受影响)",
                          self._enc_frames)
        return data

    def _safe_has_ratchet(self):
        try:
            return bool(self._enc.has_key_ratchet())
        except Exception:
            return "?"

    @property
    def ready(self) -> bool:
        try:
            return bool(self._sess.has_established_group()) and bool(self._enc.has_key_ratchet())
        except Exception:
            return False

    def can_passthrough(self, user_id) -> bool:
        # py-cord opus.py:729 在 opus decode 后调此判断是否要在 PCM 上再 DAVE 解密一遍
        # (davey 的多余分支)。我们的 DAVE 解密已在 _probed/decrypt_rtp 阶段完成,
        # decrypted_data 已是明文 Opus, opus.py:711 解出的 PCM 就是最终结果。
        # 返回 False 跳过那个多余分支, 避免 AttributeError 把好 PCM 丢成 silence。
        return False

    @property
    def voice_privacy_code(self):
        return None


def _install_dave_py_backend():
    """把 py-cord 的 DAVE 后端从 davey 换成 dave-py (只装一次, 进程级)。

    手法: monkeypatch ``davey`` 模块的 ``DaveSession`` / ``CommitWelcome`` 属性 ——
    py-cord 在 state.py/gateway.py 用 ``davey.DaveSession(...)`` / isinstance(.,
    ``davey.CommitWelcome``) 在**调用时**做属性查找, 故换模块属性即拦截全部调用,
    零改 py-cord 源码。再 patch VoiceConnectionState 两个 async 方法 (additive,
    先调原版再加料): reinit 注入 state 引用 (adapter 需 state.ssrc + 频道成员名单);
    execute_dave_transition 在 epoch 切换后刷新 ratchet (py-cord 原版不刷, endcord
    opcode 22 会刷)。

    ⚠️ 这条线同时改发送加密 (encrypt_opus)。换错会哑 TTS —— 故 adapter.encrypt_opus
    任何异常回落明文, 且 _DAVE_PY_BACKEND_ENABLED=False 可一键回滚到纯 davey。
    """
    global _dave_backend_installed
    if _dave_backend_installed:
        return
    if not _DAVE_PY_BACKEND_ENABLED:
        log.info("dave-py 后端开关关闭 (_DAVE_PY_BACKEND_ENABLED=False), 保持 davey")
        return
    try:
        import dave  # noqa: F401  确认已装, 未装则跳过 (保持 davey, 发送不受影响)
        import davey
        from discord.voice.state import VoiceConnectionState

        davey.DaveSession = DaveSessionAdapter
        davey.CommitWelcome = _CommitWelcome

        if not getattr(VoiceConnectionState, "_cc_dave_py_patched", False):
            _orig_reinit = VoiceConnectionState.reinit_dave_session

            async def _reinit_with_state(self):
                await _orig_reinit(self)
                if self.dave_session is not None:
                    try:
                        self.dave_session._state = self
                    except Exception:
                        pass

            VoiceConnectionState.reinit_dave_session = _reinit_with_state

            _orig_exec = VoiceConnectionState.execute_dave_transition

            async def _exec_then_refresh(self, transition):
                await _orig_exec(self, transition)
                sess = self.dave_session
                if sess is not None and hasattr(sess, "_refresh_ratchets"):
                    try:
                        sess._refresh_ratchets()
                    except Exception:
                        log.exception("execute_dave_transition 后刷新 ratchet 失败")

            VoiceConnectionState.execute_dave_transition = _exec_then_refresh
            VoiceConnectionState._cc_dave_py_patched = True

        _dave_backend_installed = True
        log.info("DAVE 后端已替换为 dave-py (Session + Encryptor + 逐用户 Decryptor); "
                 "发送回落明文兜底已就位")
    except Exception:
        log.exception("dave-py 后端替换失败 —— 保持 davey (发送不受影响, 接收仍乱码)")


def _install_receive_probe():
    """挂 decrypt_rtp ssrc 探针 (只挂一次, 进程级)。

    每个 RTP 包都过 ``PacketDecryptor.decrypt_rtp`` (DAVE 解密门之前), 这里记下
    ``packet.ssrc`` 到 ``_seen_ssrcs`` —— py-cord 2.8.0 下未映射 ssrc 的包在 reader
    被丢弃前不建 decoder, 所以 decoders.keys() 推断已失效, 这是唯一可靠的传输层实收
    ssrc 来源。PacketDecryptor 只被接收路径(AudioReader)用, 不碰发送(TTS 播放), 安全。
    """
    global _receive_probe_installed
    if _receive_probe_installed:
        return
    try:
        from discord.voice.receive.reader import PacketDecryptor
        try:
            from discord.voice.packets.core import OPUS_SILENCE
        except Exception:
            OPUS_SILENCE = b"\xf8\xff\xfe"
        _orig = PacketDecryptor.decrypt_rtp

        def _probed(self, packet):
            try:
                with _seen_ssrcs_lock:
                    _seen_ssrcs.add(packet.ssrc)
            except Exception:
                pass

            # ── 接收路径专用: 用 endcord 验证过的正确顺序自己做 DAVE 解密 ──
            # py-cord 2.8.0 reader.decrypt_rtp 有两个 bug:
            #  ① DAVE 解密**后**又调 update_extended_header + decrypted_data[offset:]
            #     (reader.py:306-308), 把已是合法 Opus(头=78) 的明文当扩展头再切一刀
            #     → opus "corrupted stream"。endcord 在 DAVE 解密**前**剥扩展、解密后
            #     直接喂 opus、绝不后切 (voice.py:895-930)。rtpsize AEAD 传输层已把扩展
            #     头并入 AAD, _decryptor_rtp 返回的就是干净密文, 所以解密后无需也不能再切。
            #  ② dave.ready 但 ssrc 未映射(uid 缺)时既不解密也不兜底, 留 MLS 密文 → 崩。
            # 这里完全接管 DAVE 分支, 不调 py-cord 原 _orig (含错误后切); 非 DAVE / 未就绪
            # 才回落 _orig。PacketDecryptor 只被 AudioReader 用, 不碰 TTS 发送, 安全。
            handled = False
            try:
                state = self.client._connection
                dave = getattr(state, "dave_session", None)
                if dave is not None and getattr(dave, "ready", False):
                    # 传输层 SRTP 解密 (rtpsize: 扩展头已并入 AAD, 返回干净密文)
                    raw_payload = self._decryptor_rtp(packet)
                    uid = state.ssrc_user_map.get(packet.ssrc)
                    if uid:
                        try:
                            plain = dave.decrypt(uid, None, raw_payload)
                        except Exception:
                            plain = None
                        # DAVE 明文(头=78 真 Opus)直接喂 opus, 不再后切扩展头
                        packet.decrypted_data = plain if plain else OPUS_SILENCE
                    else:
                        # 未映射 ssrc: 兜底有效静音帧, 让 router 存活等 ssrc 推断补映射
                        packet.decrypted_data = OPUS_SILENCE
                    result = packet.decrypted_data
                    handled = True
            except Exception:
                log.exception("[DAVE接管] decrypt_rtp 自管失败, 回落 py-cord 原实现")
            if not handled:
                result = _orig(self, packet)
            return result

        PacketDecryptor.decrypt_rtp = _probed
        _receive_probe_installed = True
        log.info("decrypt_rtp ssrc 探针已挂载 (接收路径专用, 不影响发送)")
    except Exception:
        log.exception("decrypt_rtp ssrc 探针挂载失败 (ssrc 自动推断将退化)")

    # ── opus decode 崩溃兜底 (接收路径专用, 不碰 TTS 发送) ──
    # graceful degradation: 个别帧 decode 失败 (偶发 ssrc 未映射 / 钥匙刚切换的边界帧)
    # 时回落一帧静音 PCM, 让 PacketRouter.run() 永不因单帧异常退出 —— 退出会触发
    # stop_recording → 守护循环疯狂重启录音 → voice 连接抖动。正常语音帧 (DAVE 解密的
    # 明文 Opus, 头=0x78) 照常解码。
    try:
        from discord.opus import PacketDecoder, Decoder
        if not getattr(PacketDecoder, "_cc_decode_guarded", False):
            _orig_decode = PacketDecoder._decode_packet
            try:
                _silence_pcm = b"\x00" * (Decoder.SAMPLES_PER_FRAME * Decoder.SAMPLE_SIZE)
            except Exception:
                _silence_pcm = b"\x00" * 3840
            _decode_fail_n = [0]

            def _decode_guarded(self, packet):
                try:
                    return _orig_decode(self, packet)
                except Exception:
                    _decode_fail_n[0] += 1
                    if _decode_fail_n[0] % 500 == 1:
                        log.warning("[opus兜底] decode 失败累计 %d 帧 → 回落静音 (单帧偶发, 不影响整体)",
                                    _decode_fail_n[0])
                    return packet, _silence_pcm

            PacketDecoder._decode_packet = _decode_guarded
            PacketDecoder._cc_decode_guarded = True
            log.info("opus _decode_packet 崩溃兜底已挂载")
    except Exception:
        log.exception("opus _decode_packet 崩溃兜底挂载失败")


async def _ssrc_infer_loop(period: float = 0.3):
    """后台守护: ① 录音被 corrupted stream 冲垮时自动重启; ② ssrc 自动推断兜底
    (频道唯一真人时, 把传输层实收的未映射 ssrc 直接 _add_ssrc, 不靠 speaking 事件)。
    含诊断日志 (dave ready/epoch + ssrc_map + hits + 实收 ssrc), 便于现场定位。"""
    global _stt_sink, _listen_restart_n
    log.info("ssrc 推断 + 录音守护循环已启动")
    diag_n = 0
    _diag_last: tuple = ()
    try:
        while True:
            await asyncio.sleep(period)
            # 录音被冲垮后自动重启 (新 sink), 等 ssrc 映射好就能正常收
            if _listen_active and _listen_vc is not None and not _listen_vc.is_recording():
                if _listen_restart_n < _LISTEN_RESTART_MAX:
                    _listen_restart_n += 1
                    try:
                        new_sink = _get_stt_sink_class()()
                        new_sink.vc = _listen_vc
                        _stt_sink = new_sink
                        _listen_vc.start_recording(new_sink, _on_recording_done)
                        log.info("录音已自动重启 (第 %d 次)", _listen_restart_n)
                    except Exception:
                        log.exception("录音自动重启失败")
                continue
            sink = _stt_sink
            if sink is None:
                continue
            vc = getattr(sink, "vc", None)
            st = getattr(vc, "_connection", None) if vc else None
            dave = getattr(st, "dave_session", None)
            # ssrc_user_map: ssrc→uid (state.py 属性, 是 _id_to_ssrc 的逆)。
            smap = getattr(st, "ssrc_user_map", None)
            cur_map = dict(smap) if smap else {}
            with _seen_ssrcs_lock:
                seen = set(_seen_ssrcs)
            cur_hits = sink.hits()
            # 诊断: 每 10 轮(~3s)打一次, hits / map / 实收 ssrc 一变化立即打
            cur_diag = (cur_hits, tuple(sorted(cur_map.items())), tuple(sorted(seen)))
            changed = cur_diag != _diag_last
            diag_n += 1
            if changed or diag_n % 10 == 0:
                log.info(
                    "诊断#%d: ready=%s epoch=%s ssrc_map=%s hits=%s 实收ssrc=%s%s",
                    diag_n, getattr(dave, "ready", None), getattr(dave, "epoch", None),
                    cur_map, cur_hits, sorted(seen), "  <<变化" if changed else "",
                )
                _diag_last = cur_diag
            # ssrc 自动推断兜底: 频道唯一真人时, 传输层实收但未映射的 ssrc 必是那个真人
            try:
                if vc is not None and dave is not None and getattr(dave, "ready", False):
                    known = set(cur_map.keys())
                    unknown = seen - known
                    bot_id = None
                    try:
                        bot_id = vc.guild.me.id
                    except Exception:
                        pass
                    human_ids = set()
                    ch = getattr(vc, "channel", None)
                    if ch is not None:
                        for m in (getattr(ch, "members", None) or []):
                            if not getattr(m, "bot", False) and m.id != bot_id:
                                human_ids.add(m.id)
                        for uid in (getattr(ch, "voice_states", None) or {}).keys():
                            if uid == bot_id:
                                continue
                            # voice_states 里会混入其它队友 bot (如 tianmaojingling),
                            # py-cord 还可能 "Skipping member" 解析不出它们。无法解析的
                            # 成员或 .bot=True 一律不算真人 —— 否则会被算进"未映射真人",
                            # 让 len(unmapped_humans)>1, 唯一真人的 ssrc 永远绑不上 →
                            # decrypt_rtp 兜底每帧塞 OPUS_SILENCE → RMS=0 → VAD 永不断句。
                            try:
                                m = ch.guild.get_member(uid)
                            except Exception:
                                m = None
                            if m is None or getattr(m, "bot", False):
                                continue
                            human_ids.add(uid)
                    # 唯一未映射的真人 → 唯一未映射的 ssrc。比「频道全局唯一真人」更
                    # 鲁棒: 房里有 2 人但一人已映射时, 剩下的实收 ssrc 必属另一人。
                    mapped_uids = set(cur_map.values())
                    unmapped_humans = human_ids - mapped_uids
                    if unknown and len(unmapped_humans) == 1:
                        hid = next(iter(unmapped_humans))
                        for s in unknown:
                            vc._add_ssrc(hid, s)
                            log.info("🔧 自动推断 ssrc: user=%s ssrc=%s (唯一未映射真人)", hid, s)
            except Exception:
                log.exception("ssrc 自动推断失败")
    except asyncio.CancelledError:
        log.info("ssrc 推断循环已取消")
        raise


def _build_bot(bot_name: str, guild_id: str = "", voice_channel_id: str = ""):
    """构造只含 /leave 的最小 discord.Bot (不挂任何消息 handler)。

    on_ready 后自动常驻 voice_channel_id 指定的语音频道。
    """
    import discord

    intents = discord.Intents.default()  # 含 voice_states
    intents.message_content = True       # 接收文字消息内容 (Discord Developer Portal 需开 privileged intent)
    # auto_sync_commands=False: sidecar 与主 DiscordChannel 共用同一 token = 同一
    # application。py-cord 的 sync_commands() global 分支无条件 bulk-overwrite,
    # 会把主频道注册的 global 命令 (/status 等) 全冲掉。这里关掉自动同步, 改在
    # on_ready 里用 register_commands(guild_id=...) 只往 guild 注册, 绝不碰 global。
    bot = discord.Bot(intents=intents, auto_sync_commands=False)

    @bot.event
    async def on_ready():
        global _target_voice_channel_id
        log.info(
            "Discord 语音 sidecar 上线: %s (guilds=%d)",
            bot.user, len(bot.guilds),
        )
        ch = await _resolve_voice_channel(bot, voice_channel_id)
        if ch is None:
            log.warning("找不到可常驻的语音频道，sidecar 仅在线不进频道")
            return
        _target_voice_channel_id = ch.id
        vc = await _ensure_connected()
        if vc is not None:
            log.info("已常驻语音频道: %s (id=%s)", ch.name, ch.id)
            _get_persistent_source()
        # 启动 TTS 播报队列 consumer（防重复：on_ready 在 RESUME 后可能再次触发）
        global _speak_queue, _speak_consumer_task
        if _speak_queue is None:
            _speak_queue = asyncio.Queue()
        if _speak_consumer_task is None or _speak_consumer_task.done():
            _speak_consumer_task = asyncio.create_task(_speak_consumer())
            log.info("TTS 播报队列 consumer 已启动")
        # 启动后台 voice 健康检查（防重复：on_ready 在 RESUME 后可能再次触发）
        global _heartbeat_task
        if _heartbeat_task is None or _heartbeat_task.done():
            _heartbeat_task = asyncio.create_task(_voice_heartbeat())
            log.info("voice 心跳已启动（30s 周期，断线自动 rejoin）")
        # 显式把 slash 命令只注册到 guild (不碰 global, 保护主频道命令)。
        # on_ready 时 application_id + guilds 已就绪, 避开 on_connect 过早 sync 的坑。
        global _commands_synced
        if guild_id and not _commands_synced:
            try:
                gid = int(guild_id)
                regd = await bot.register_commands(
                    bot.pending_application_commands,
                    guild_id=gid, method="bulk", force=True,
                )
                _commands_synced = True
                log.info(
                    "slash 命令已注册到 guild %s: %s",
                    gid, [c.get("name") for c in regd],
                )
            except Exception:
                log.exception("slash 命令注册失败 (guild=%s)", guild_id)

    @bot.event
    async def on_application_command_error(ctx, error):
        log.error("slash command 出错: %s", error, exc_info=error)
        try:
            await ctx.respond(f"❌ 命令出错：{error}", ephemeral=True)
        except Exception:
            pass

    @bot.event
    async def on_member_speaking_state_update(member, ssrc, state):
        """Voice Gateway Opcode 5: 用户 speaking 状态变化 (PTT 按下/松手)。

        state: discord.SpeakingState — none(0)=松手, voice(1)=按下
        这是真正的 Discord PTT 信号，不是 RTP 超时推断。
        """
        if member is None or member.bot:
            return
        from discord.enums import SpeakingState
        if state == SpeakingState.none:
            log.info("[Discord] PTT 松手 (Opcode 5 speaking=0): %s", member.display_name)
            _on_discord_speaking_stop()
        else:
            log.info("[Discord] PTT 按下 (Opcode 5 speaking=%s): %s", int(state), member.display_name)

    @bot.event
    async def on_message(message):
        """Discord 语音房文字聊天 → BotCore → 回复发回 Discord。"""
        if message.author.bot:
            return
        text = (message.content or "").strip()
        if not text:
            return
        if bot.user:
            text = re.sub(rf'<@!?{bot.user.id}>', '', text).strip()
        if not text:
            return

        ch_ref = _feishu_ref
        feishu_loop = _feishu_loop
        if ch_ref is None or feishu_loop is None:
            return
        core = getattr(ch_ref, '_core', None)
        if core is None:
            return

        log.info("Discord 文字 → BotCore: [%s] %s", message.author.display_name, text[:80])
        dc_channel = message.channel
        sidecar_loop = _sidecar_loop
        open_id = _feishu_open_id
        if not open_id:
            return

        # 注入 AgentSession: 跟语音 STT 出文字后走完全一样的管线
        # (generate_reply → CloseCrabLLM → feishu worker → TTS → Discord 喇叭)
        session = _agent_session
        if session is None:
            _pending_discord_text.append(text)
            log.info("Discord 文字: AgentSession 未就绪, 缓存待回放 (%d条): %s",
                      len(_pending_discord_text), text[:80])
            return
        from .livekit_io import _closecrab_llm_instance
        llm = _closecrab_llm_instance()
        if llm is not None:
            llm._skip_next_debounce = True
        session.generate_reply(user_input=text)
        log.info("Discord 文字 → AgentSession.generate_reply: %s", text[:80])

    @bot.slash_command(description="让机器人离开语音频道")
    async def leave(ctx):
        vc = ctx.guild.voice_client
        if vc and vc.is_connected():
            await vc.disconnect(force=True)
            await ctx.respond("👋 已离开语音频道。")
        else:
            await ctx.respond("我不在任何语音频道里。")

    @bot.slash_command(description="开始把语音频道里的说话转成文字发到这里")
    async def listen(ctx):
        vc = ctx.guild.voice_client
        if vc is None or not vc.is_connected():
            await ctx.respond("我还没在语音频道里，稍等心跳重连或重启后再试。", ephemeral=True)
            return
        if vc.is_recording():
            await ctx.respond("已经在收音了。", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)  # silero VAD 加载 + session 启动可能耗时
        ok, msg = await _activate_listen(vc)
        if not ok:
            await ctx.respond(f"❌ {msg}", ephemeral=True)
            return
        await ctx.respond("🎤 开始收音，说几句中文试试（silero VAD 自动断句）。", ephemeral=True)

    @bot.slash_command(description="停止语音转文字")
    async def stoplisten(ctx):
        global _ssrc_task, _audio_pump_task, _agent_session, _audio_input, _audio_output, _listen_active
        _listen_active = False  # 先关, 防守护循环在停录后又自动重启
        vc = ctx.guild.voice_client
        if vc and vc.is_recording():
            vc.stop_recording()
        if _audio_pump_task is not None:
            _audio_pump_task.cancel()
            _audio_pump_task = None
        if _ssrc_task is not None:
            _ssrc_task.cancel()
            _ssrc_task = None
        if _agent_session is not None:
            try:
                await _agent_session.aclose()
            except Exception:
                log.exception("AgentSession 关闭异常")
            _agent_session = None
        _audio_input = None
        _audio_output = None
        hits = _stt_sink.hits() if _stt_sink is not None else 0
        await ctx.respond(
            f"🛑 已停止收音。本次 voice 包命中 {hits} 次"
            + ("（>0 说明接收链路通）。" if hits else "（=0 说明还没收到解密音频）。"),
            ephemeral=True,
        )

    return bot


def _persist_sidecar_enabled(bot_name: str, enabled: bool) -> None:
    """把长期开关写回 Firestore channels.discord.voice_sidecar，跨重启保持状态。

    /discordon → True, /discordoff → False。main.py 开机自启读这个字段恢复。
    持久化失败只警告，不阻断连/断动作。
    """
    try:
        from google.cloud import firestore
        from ..constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE

        db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
        db.collection("bots").document(bot_name).update(
            {"channels.discord.voice_sidecar": enabled}
        )
        log.info("voice_sidecar 持久化为 %s (bot=%s)", enabled, bot_name)
    except Exception as e:
        log.warning("持久化 voice_sidecar 失败 (non-fatal): %s", e)


def is_sidecar_running() -> bool:
    """sidecar 线程是否在跑 (gateway 在线，不一定已进语音频道)。"""
    return (
        _sidecar_bot is not None
        and _sidecar_thread is not None
        and _sidecar_thread.is_alive()
    )


def _spawn_sidecar_thread(
    bot_name: str, token: str, guild_id: str, voice_channel_id: str
):
    """拉起 sidecar daemon 线程 (独立 loop 跑 discord.Bot)。返回 thread 或 None。

    开机自启 (maybe_start_discord_voice_sidecar) 和命令强制启动 (start_sidecar)
    共用这段。调用方负责校验 token / enabled。
    """
    try:
        import discord  # noqa: F401
    except ImportError:
        log.warning("未安装 py-cord，Discord 语音 sidecar 跳过")
        return None

    # livekit silero 插件在 import 时调 register_plugin(), 而 livekit 强制插件只能
    # 在主线程注册 (agents/plugin.py 检查 current_thread == main_thread)。sidecar 跑在
    # daemon 线程, 故这里先在主线程 import 一次进 sys.modules; 线程内再 import 即命中
    # 缓存、不重跑模块体, register_plugin 不会二次触发。失败不致命 (STT 接收路径不可用,
    # 但 TTS 发送路径无关)。
    try:
        from livekit.plugins import silero  # noqa: F401
        from livekit.agents import Agent, AgentSession  # noqa: F401
    except Exception:
        log.exception("livekit 预导入失败 (STT 接收路径将不可用，不影响 TTS 发送)")

    def _run():
        global _sidecar_loop, _sidecar_bot, _sidecar_thread
        import discord
        # TTS 流式播放路径用 _StreamPCMSource(is_opus=False), py-cord 要把 PCM
        # 编码成 opus 才能发, 故必须先手动加载 libopus (默认不自动加载)。
        try:
            if not discord.opus.is_loaded():
                discord.opus.load_opus("libopus.so.0")
                log.info("opus 已加载 (TTS 流式播放编码需要)")
        except Exception:
            log.exception("opus 加载失败，TTS 流式播放可能无法编码")
        # 注: 不能禁用 DAVE (DAVE_PROTOCOL_VERSION=0) —— Discord 已强制 E2EE，
        # 声明 0 会被 voice gateway 以 close code 4017 拒绝，连放音都连不上。
        # 挂接收路径专用的 decrypt_rtp ssrc 探针 (只挂一次, 不碰发送路径)。
        _install_receive_probe()
        # 把 DAVE 后端从 davey 换成 dave-py (解密能出真 PCM)。这条线同时碰发送加密,
        # encrypt_opus 已做明文回落兜底; 一键回滚 = _DAVE_PY_BACKEND_ENABLED=False。
        _install_dave_py_backend()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _sidecar_loop = loop
        bot = _build_bot(bot_name, guild_id, voice_channel_id)
        _sidecar_bot = bot
        try:
            # 用 start() 而非 run()——run() 装 signal handler 只能在主线程
            loop.run_until_complete(bot.start(token))
        except Exception as e:
            log.error("Discord 语音 sidecar 崩溃: %s", e, exc_info=True)
        finally:
            _sidecar_bot = None
            _sidecar_loop = None
            _sidecar_thread = None
            try:
                loop.run_until_complete(bot.close())
            except Exception:
                pass
            loop.close()

    global _sidecar_thread
    thread = threading.Thread(target=_run, daemon=True, name="discord-voice-sidecar")
    _sidecar_thread = thread
    thread.start()
    log.info("Discord 语音 sidecar 线程已启动")
    return thread


def maybe_start_discord_voice_sidecar(bot_name: str) -> threading.Thread | None:
    """开机自启：Firestore voice_sidecar=true 时后台拉起 sidecar 线程。

    返回线程对象 (已 start)，未启用 / 缺 token / 不可用时返回 None。
    这是状态持久化的重启恢复点 —— /discordon 会把该字段写成 true。
    """
    cfg = _load_sidecar_config(bot_name)
    if not cfg or not cfg["enabled"]:
        return None
    if not cfg["token"]:
        log.warning("Discord 语音 sidecar 已开启但缺 token，跳过")
        return None
    vch = cfg.get("voice_channel_id") or _DEFAULT_VOICE_CHANNEL_ID
    return _spawn_sidecar_thread(bot_name, cfg["token"], cfg.get("guild_id", ""), vch)


def start_sidecar(bot_name: str) -> tuple[bool, str]:
    """【飞书线程调用】运行时连进 Discord 语音频道 + 持久化 voice_sidecar=true。"""
    if is_sidecar_running():
        _persist_sidecar_enabled(bot_name, True)
        return True, "Discord 已经连着 General 了。"
    cfg = _load_sidecar_config(bot_name)
    if not cfg or not cfg["token"]:
        return False, "这个 bot 没配 Discord token，连不了。"
    vch = cfg.get("voice_channel_id") or _DEFAULT_VOICE_CHANNEL_ID
    thread = _spawn_sidecar_thread(bot_name, cfg["token"], cfg.get("guild_id", ""), vch)
    if thread is None:
        return False, "启动失败 (py-cord 未装？看 bot.log)。"
    import time
    for _ in range(50):  # 轮询 ~10s 等 on_ready + 进频道
        if is_voice_connected():
            _persist_sidecar_enabled(bot_name, True)
            return True, "✅ 已连进 Discord General，开始语音播报 (重启后保持)。"
        time.sleep(0.2)
    # 线程起来了但 10s 内没进频道：仍持久化 (心跳会稍后 rejoin)
    _persist_sidecar_enabled(bot_name, True)
    return True, "⚠️ sidecar 已启动但还没进频道，稍等或看 bot.log (已设为开)。"


def stop_sidecar(bot_name: str) -> tuple[bool, str]:
    """【飞书线程调用】断开 Discord 语音 + 持久化 voice_sidecar=false。"""
    _persist_sidecar_enabled(bot_name, False)
    if not is_sidecar_running():
        return True, "本来就没开 (已确保关闭态)。"

    global _heartbeat_task, _target_voice_channel_id, _sidecar_thread
    loop = _sidecar_loop
    bot = _sidecar_bot
    thread = _sidecar_thread

    async def _shutdown():
        # 先停心跳，否则它会在 loop 关闭后报错；再 disconnect，最后 close。
        if _heartbeat_task is not None and not _heartbeat_task.done():
            _heartbeat_task.cancel()
        try:
            if bot is not None and bot.guilds:
                vc = bot.guilds[0].voice_client
                if vc is not None and vc.is_connected():
                    await vc.disconnect(force=True)
        except Exception:
            log.exception("disconnect voice 失败")
        if bot is not None:
            await bot.close()  # 让线程里 run_until_complete(bot.start()) 返回

    try:
        if loop is not None and not loop.is_closed():
            # 不 .result() 等：loop 会在 bot.close 后停，future 可能不 resolve。
            # 靠 join 线程同步 —— 线程结束即 bot 已 close + finally 清理完。
            asyncio.run_coroutine_threadsafe(_shutdown(), loop)
    except Exception as e:
        log.warning("调度 sidecar 关闭异常 (继续清理): %s", e)

    if thread is not None:
        thread.join(timeout=10)
    _target_voice_channel_id = 0
    _heartbeat_task = None
    _sidecar_thread = None
    return True, "👋 已断开 Discord 语音 (重启后也不连)。"
