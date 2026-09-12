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
import collections
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

# 收音链路的故障是**静默**的: py-cord 在 voice/receive/reader.py 里把 DAVE 解密
# 异常整个吞掉, 只 `_log.debug("Ignoring exception while decoding DAVE packet")`,
# 然后塞一帧静音接着跑 —— 表现是「音质变差」, 日志里一个字都没有。
# 这个开关只在排查时打开 (VOICE_RX_DEBUG=1), 平时不开: DEBUG 级别下每个 20ms
# 的 RTP 包都会打一行, 一分钟三千行, 会把 bot.log 冲垮。
if os.environ.get("VOICE_RX_DEBUG") == "1":
    for _n in ("discord.voice.receive.reader", "discord.opus",
               "discord.voice.receive.router"):
        logging.getLogger(_n).setLevel(logging.DEBUG)
    log.warning("VOICE_RX_DEBUG=1: py-cord 收音链路已开 DEBUG (日志量很大, 排查完请关掉)")

# ─── 语音 buffer 落盘 + 重播 ──────────────────────────────────────────────
# Chris 2026-06-01: 好不容易生成的音频别播完就丢, 整段存成一个文件; 点重播就把
# 这个文件重新 streaming 到同一个 Discord 语音入口 (暂停/继续复用 vc.pause/resume)。
# 文件 = 实际推给 Discord 的 48kHz/stereo/s16 raw PCM (跟 _StreamPCMSource 一致),
# 重播直接 _FilePCMSource 顺读, 不重新调 Gemini, 也不丢音。fid 编码进飞书重播按钮。
_BUF_DIR = "/tmp/jarvis-tts-buf"
_TTS_CACHE_DIR = os.path.expanduser("~/.closecrab/tts-cache")
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


def _cache_key_for_batch(text: str, voice: str) -> str:
    import hashlib
    return hashlib.sha256(f"48s|{voice.lower()}|{text}".encode()).hexdigest()


_CACHE_MAX_CHARS = 30


def _cache_get_pcm(text: str, voice: str) -> bytes | None:
    """读缓存: 48kHz stereo s16le PCM raw 文件。超过 30 字跳过 (长文本不会重复)。"""
    if len(text) > _CACHE_MAX_CHARS:
        return None
    key = _cache_key_for_batch(text, voice)
    path = os.path.join(_TTS_CACHE_DIR, f"{key}.pcm")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


def _cache_save_pcm(text: str, voice: str, pcm: bytes):
    """存缓存: 48kHz stereo s16le PCM raw 文件。超过 30 字跳过。"""
    if not pcm or len(text) > _CACHE_MAX_CHARS:
        return
    key = _cache_key_for_batch(text, voice)
    path = os.path.join(_TTS_CACHE_DIR, f"{key}.pcm")
    os.makedirs(_TTS_CACHE_DIR, exist_ok=True)
    try:
        with open(path, "wb") as f:
            f.write(pcm)
    except Exception:
        pass


def _buf_path(fid: str) -> str:
    """fid → buffer 文件绝对路径。fid 不合法返回空串 (防路径穿越)。"""
    if not fid or not _FID_RE.match(fid):
        return ""
    return os.path.join(_BUF_DIR, f"{fid}.pcm")


_BUF_RETENTION_SEC = 7 * 86400   # 重播缓存保留 7 天
_BUF_GC_MIN_INTERVAL = 3600      # 最多每小时扫一次目录
_last_buf_gc = [0.0]


def _maybe_gc_buf_dir() -> None:
    """按需清掉过期的重播缓存 (每小时最多扫一次, 落盘时顺带触发)。

    这个目录**原先完全没有清理**: 2026-08-10 查的时候攒了 2359 个文件 / 46GB,
    最老的是一个月前的 —— 未压缩 48k/stereo/s16 PCM, 一分钟音频约 11MB,
    按当时的用量每天涨 1.5GB 左右。

    保留 7 天的依据: 重播按钮只在语音卡片上, 实际会被点的是刚发出的那条;
    缓存不在时 `_replay()` 会 log warning 并返回 False, 不会崩。

    zello sidecar 往同一个目录落盘, 共用这里的清理 —— 只要有一侧在跑就够了。
    """
    now = time.time()
    if now - _last_buf_gc[0] < _BUF_GC_MIN_INTERVAL:
        return
    _last_buf_gc[0] = now

    cutoff = now - _BUF_RETENTION_SEC
    removed = freed = 0
    try:
        with os.scandir(_BUF_DIR) as it:
            for entry in it:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if st.st_mtime >= cutoff:
                        continue
                    os.unlink(entry.path)
                    removed += 1
                    freed += st.st_size
                except OSError:
                    continue          # 正在被别的进程写/已消失, 跳过就好
    except OSError:
        return                        # 目录还不存在: 下次落盘会建
    if removed:
        log.info("TTS 重播缓存 GC: 删除 %d 个文件, 释放 %.2f GB",
                 removed, freed / 1024 ** 3)

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
_feishu_chat_id = ""        # Chris 的飞书 p2p chat_id (oc_ 开头, 发卡片/语音用)
_audio_output = None        # 当前 _DiscordAudioOutput 实例 (出口音频桥)


def set_feishu_bridge(feishu_channel, feishu_loop, open_id: str, chat_id: str = "") -> None:
    """【飞书线程调用】注册飞书大脑入口, 供 Discord 语音全双工复用 CloseCrabLLM。

    幂等: 飞书 start() / on_ready 后调一次即可。open_id 为空时不覆盖已有值。
    """
    global _feishu_ref, _feishu_loop, _feishu_open_id, _feishu_chat_id
    _feishu_ref = feishu_channel
    _feishu_loop = feishu_loop
    if open_id:
        _feishu_open_id = open_id
    if chat_id:
        _feishu_chat_id = chat_id
    log.info("飞书大脑桥已注册 (open_id=%s… chat_id=%s…) → Discord 语音可全双工",
             open_id[:8] if open_id else "?", chat_id[:8] if chat_id else "?")
_listen_restart_n = 0       # 录音自动重启计数 (上限保护)
_LISTEN_AUTOSTART = True     # 自动收音开启 (收到音频转 OGG 直推飞书)
_autostart_done = False      # 本进程内自动收音只起一次 (尊重之后的 /stoplisten)
_receive_probe_installed = False  # decrypt_rtp ssrc 探针只挂一次
_dave_backend_installed = False    # dave-py 后端替换只装一次
_ssrc_rotate_patched = False       # SSRC 换号清理补丁只挂一次
_ssrc_rotations = 0                # 换号次数, 进诊断日志 —— 频繁换号本身就是信号

# ── 「连着但聋」自愈 (DAVE 掉出 MLS 树) ─────────────────────────────────────
# ready 连续为 False 这么久 → 判定这条语音连接已经废了, 强制整条重建。
# 25 秒的余量: 正常一次 MLS 握手 (key package → proposals → commit) 实测 <100ms,
# 25 秒够容忍任何抖动, 又不至于让人对着麦克风白说半分钟。
_DAVE_UNREADY_GRACE_S = float(os.environ.get("DAVE_UNREADY_GRACE_S", "25"))
# 两次强制重连之间的最短间隔。**兼作限流** —— 对端一直不搭理时这就是唯一的刹车,
# 所以不设「重试上限」: 上限意味着某个时刻起彻底放弃, 而这个 bug 的全部危害就是
# 静默地永远聋下去 (2026-09-11 的四小时失聪、13:00 那次撞满重启配额, 都是这么来的)。
_DAVE_REJOIN_COOLDOWN_S = float(os.environ.get("DAVE_REJOIN_COOLDOWN_S", "60"))
# 开机自启后判定「语音到底起没起来」的观察窗。必须**大于**一次 py-cord 语音握手
# 超时 (20s) 加上至少两轮心跳重试 (30s/轮), 否则量到的是「还没轮到重试」而不是
# 「连不上」。取 180s: 实测 bunny 那次在第 3 次重试 (约 3 分钟) 上恢复。
_BOOT_VERIFY_TIMEOUT = float(os.environ.get("VOICE_BOOT_VERIFY_TIMEOUT", "180"))
_BOOT_VERIFY_POLL = float(os.environ.get("VOICE_BOOT_VERIFY_POLL", "5"))
_dave_unready_since = None   # ready 首次转 False 的 monotonic 时刻
_dave_last_rejoin = 0.0      # 上次强制重连的 monotonic 时刻
_dave_rejoin_n = 0           # 强制重连累计次数 (只用于日志)
# 有人正在重建语音连接 —— 其它自愈路径这一轮全部让开。
# 不是性能优化, 是正确性: 重连中途 vc.is_connected() 本来就是 False, 别的自愈看见
# 会当成故障也去重连, 把正在进行的握手掐断。2026-09-11 02:50:27 实测: 守护循环刚
# 断开准备重连, 心跳 (30s 一轮) 正好撞上, 于是
#   02:50:30 我这条握手完成 → 02:50:31 心跳 Terminating voice handshake
#   → ClientConnectionResetError: Cannot write to closing transport
# 两个自愈单看都对, 合在一起互相击落。
_voice_reconnecting = False

# ── 「树是好的、可就是没声」自愈 (第二条判据) ───────────────────────────────
# 上面那条只看 `dave.ready`。2026-09-11 18:10 那次双向不通，**ready 一直是 True、
# epoch=1**，所以上面那条永远为假、一次都没触发过 —— 不是自愈坏了，是它监视的是
# 「树建没建起来」，而这次树好好的，坏的是别处：ssrc 对不上。
#
#   ssrc_map={10468:bunny, 10616:Chris}   实收ssrc=[10356, 770909262, ...]
#   hits=0
#
# 映射表里那两个门牌号一个都没实收到，10356 是 Chris 换掉的旧号。包进来了，认不出
# 是谁发的，全被当陌生人丢掉，一帧 PCM 都到不了 sink。
#
# **判据不能是「hits 长时间不涨」** —— 没人说话时它本来就不涨，那样会在安静的房间
# 里每分钟掐一次线，比原来的病还糟（上面 `_channel_human_ids` 那条判据就是为了躲
# 同一个坑加的）。要的是一个「此刻确实该有声音」的独立信号。
#
# 用 Discord 的 **speaking 事件**（voice websocket op 5）：它由服务端下发，说明
# 「这个人开始发音频了」，跟我们的 UDP 收没收到、解没解得开完全无关。于是：
#
#   服务端说他在说话  +  过了宽限期 hits 一帧没涨  →  这条路是聋的
#
# 宽限期取 5 秒: 人说一个字就够 20 帧，5 秒还是 0 帧不可能是抖动。
_DEAF_GRACE_S = float(os.environ.get("VOICE_DEAF_GRACE_S", "5"))
_speak_start_ts = 0.0        # 最近一次真人被通报「开始说话」的 monotonic 时刻
_speak_start_hits = None     # 那一刻 sink.hits() 的值; None = 没有待判的说话事件

# ── DAVE 后端总开关 (rollback 用) ──────────────────────────────────────────
# True = 把 py-cord 的 DAVE 后端从官方 davey 换成第三方 dave-py。
#
# ══════════════════════════════════════════════════════════════════════════
#  结论先写在最前面 (2026-09-11 定案, Chris 拍板)
# ══════════════════════════════════════════════════════════════════════════
#
#   **以后固定用官方 davey。这个开关默认 0, 保留只为应急一键回滚。**
#
# 而真正值得记住的是: 我们当初弃用官方库的那个理由, **最后被证明不是病因**。
# 三个月里换库、打补丁、再换回来, 兜了一整圈, 真凶始终在我们自己这一侧 ——
# 收到的 RTP 包尾部有填充字节, 谁都没切, 直接喂给了 DAVE。
#
# 下面是完整经过, 写长是因为这个坑值得。
#
# ── 第一幕: 官方库「解不了密」, 于是换第三方 (2026-06) ───────────────────
#
# `e4bc1ca` (06-02) 复活 DAVE 加密语音接收。当时的症状是: 从「Chris 说话」到
# 「bot 收到明文 Opus」这条链路**整条不通**, STT 永远静音。
#
# 当时给出的解释是「davey 不暴露逐 MLS-epoch 的 key-ratchet API, 接收端无法
# 按 epoch 驱动解密」。endcord 那个项目也因为同样的缺口弃了 davey 改用 dave-py,
# 这条旁证让判断显得很扎实。dave-py 把 API 拆成 Session(MLS) + 逐 ssrc
# Decryptor(带 transition_to_key_ratchet) + Encryptor, 看起来正好补上缺口。
#
# ⚠️ 这个 monkeypatch **同时碰发送加密** (client.py:_get_voice_packet 调
# session.encrypt_opus), 所以它一换就是双向, 换错会连 TTS 一起哑掉。
#
# ── 第二幕: 前提不成立了, 换回来 —— 结果还是坏 (2026-09-11 上午) ─────────
#
# `2ddcf16` 把后端换回官方 davey。理由是那条「不暴露 ratchet API」今天已经不成立:
#   - davey 0.1.6 的 DaveSession 直接给 decrypt(user_id, media_type, data),
#     ratchet 在库内部按 user_id 自己管, 调用方不需要逐 epoch 驱动。
#   - py-cord 2.8.1 的接收器 (voice/receive/reader.py:290 decrypt_rtp) 本来就
#     调 dave.decrypt 解每个 RTP 包 —— **接收在上游早就是原生支持的了。**
# 而补丁的代价看得见: 递钥匙胚子 / 收欢迎信 / 提交这整套 MLS 握手全走第三方实现,
# 屋里的真人用官方实现。当天 bunny 整天 epoch=None, 一个 MLS 组都没进去过。
#
# 换回去之后 epoch 确实变成了数字 —— 树建起来了。**但音频还是断断续续**,
# 约 40% 的帧解不开, 报 `UnencryptedWhenPassthroughDisabled`, 直译是
# 「帧尾找不到那个『我是加密帧』的标记」。于是又滚回 dave-py, 音质好一些,
# 可仍然吞掉 29 帧。**两个后端都吞帧, 只是吞的比例不同。**
#
# 这一步是转折: 两个独立实现同时出错, 病因大概率不在实现里。
#
# ── 第三幕: 真凶是 RTP 尾部填充 (`2bcec5d`) ──────────────────────────────
#
# 给失败帧打上帧尾十六进制 (`833c02e` 按「成败 x 扩展头 x 帧尾」分桶) 之后,
# 尾巴长这样: `0x11`×17、`0x12`、`0x16`、`0x10`、`0x2f`×47、`0x17` ——
# **一串重复的同一个字节, 而且那个字节的值恰好等于重复的次数。** 这是教科书
# 里 PKCS#7 式填充的样子, **密文不可能长成这样**。
#
# 于是回头看 RTP 头第一个字节的 P 位 (RFC 3550 §5.1): 置起时末字节给出填充长度
# (含自身)。py-cord 在 `packets/rtp.py:100` 把 `packet.padding` 解出来了,
# **之后再没用过** —— 它原来的路是直接喂 Opus, 而 Opus 容忍尾部垃圾。
# **DAVE 不容忍**: 它那个「我是加密帧」的标记 (`0xFAFA`) 就在帧尾, 被填充埋住了。
#
# `_strip_rtp_padding` 因此诞生, 详细实测数据在那个函数的 docstring 里。一句话:
# dave-py 从 577成功/29失败 变成 290/0, 官方 davey 从约 40% 失败变成 370/0。
#
# ── 第四幕: 那官方库这三个月到底修了什么? (查证结果) ─────────────────────
#
# 直觉会说「大概官方后来把这个 bug 修了」。**查了, 不是。**
#
# davey 的版本时间线 (github.com/Snazzah/davey releases):
#   py-0.1.5  2026-03-29     ← 我们 06-02 复活 DAVE 时 PyPI 上的最新版
#   py-0.1.6  2026-06-22     ← 目前装的
# 两版之间只有**一个**实质提交: PR #17 "libdave parity and video processing"
# (43febf0, 06-20 合入), 一共动了 5 个文件:
#   - codec_utils.rs      H.265 的 NAL 单元该走明文却走了密文     → 视频, 与音频无关
#   - encryptor.rs        密文缓冲区尺寸计算                       → 发送侧
#   - frame_processors.rs u8 溢出 (>255 截断) + do_reconstruct 边界 → 主要是发送侧
#   - decryptor.rs        passthrough 过期时间 max→min             → 只影响透传窗口
#   - session.rs          1 行
# **没有一条针对「帧尾找不到 DAVE 标记」这个症状。**
#
# 更硬的证据在时间上: 本机 davey 是 09-11 **04:26** 升到 0.1.6 的, 而
# `2ddcf16` 切回官方是同一天上午之后 —— **切回去时用的就已经是 0.1.6, 照样 40%
# 失败**。所以「官方现在能用了」不是官方修的, 是我们自己切掉了填充。
#
# ── 一条还没钉死的因果 (诚实标注) ────────────────────────────────────────
#
# `855a8fb` 那轮官方 davey 跑到零失败时, **一帧带填充的都没收到**, 切填充那段
# 代码压根没被触发 —— 它证明的是「官方库现在好了」, 不是「好是因为它」。
# 18:29 那轮补上了一半: 首次在官方 davey 上收到 `有填充/尾fafa/成功×3`, 带填充
# 的帧真的走了这条路并且解开了。**但要彻底钉死, 得等一轮带填充的帧出现时把
# `_strip_rtp_padding` 临时短路, 看失败会不会回来。** 那会牺牲几十秒音质,
# Chris 09-11 18:36 判断不值得 ——「现在工作的端到端都挺好的, 就不用再改了」。
# 旁证仍在: 官方库当年失败那批帧的尾巴是 `ffff` / `0e0e` / `1919` / `1010`,
# **每一个都是同一字节重复两遍** —— 填充是一串相同字节, 只看末两位就是双写。
#
# ── 留下的教训 ───────────────────────────────────────────────────────────
#
# 1. **「换个库试试」会掩盖真因。** 换库让症状从 100% 坏变成 40% 坏, 看起来
#    像进步, 实际只是换了个对垃圾字节容忍度不同的实现, 真因原封不动。
# 2. **两个独立实现同时出错 = 病因在它们外面。** 这个信号出现得很早, 但被
#    「第三方比官方好」的叙事盖过去了。
# 3. **判据要能自证伪。** 是「按包形状分桶」这个改动 (`833c02e`) 直接把答案
#    摆到脸上 —— 在那之前只有一个失败计数, 看一百遍也看不出填充。
#
# 坏了就 `export DAVE_PY_BACKEND=1` 重启回滚, 不必改代码。
# ⚠️ `~/.zshenv` 里必须**显式写 0 而不是把那行注释掉** —— run.sh 只 source
# 不 unset, 注释掉的话首轮 source 留在外层壳里的 =1 会一直生效, exit-42 只换
# 内层 python 进程, 换不掉它。这个坑当天真踩了一次。
_DAVE_PY_BACKEND_ENABLED = os.environ.get("DAVE_PY_BACKEND", "0") == "1"

# ── Opus FEC 丢包恢复开关 ──────────────────────────────────────────────
# True = 检测 RTP 序列号 gap 时用 decode(fec=True) 恢复丢失帧。
# 实测 2026-06-06: 主要改善来自稳定网络 (不移动), FEC 恢复效果有限 (只救 1 帧,
# 连续丢 10+ 帧的 5G 基站切换场景救不了)。Chris 要求先关掉。
_FEC_ENABLED = False

_LISTEN_RESTART_MAX = 8      # 录音崩溃后最多自动重启次数
# 这个预算防的是「录音一起来就崩」的死循环，**不是**一天累计只准重启 8 次。
# 录音连续正常这么久就把预算还回去 —— 否则一次掉线重连烧掉几次配额后，
# 机器人会在某个未来时刻悄悄变成「能说不能听」，而且日志里一句错都不再打。
_LISTEN_HEALTHY_RESET_S = 60.0
_listen_ok_since: float | None = None   # 录音连续正常的起点 (time.monotonic)

# 单声道 20ms 帧 @ 48kHz/16-bit: 喂给 AudioInput 的基本单位。
_MONO_FRAME_MS = 20
_MONO_FRAME_SAMPLES = 48000 * _MONO_FRAME_MS // 1000   # 960
_MONO_FRAME_BYTES = _MONO_FRAME_SAMPLES * 2            # 1920

# decrypt_rtp 探针记录的「传输层实收 ssrc」(每个 RTP 包都过 decrypt_rtp, 在 DAVE
# 解密门之前)。这是 ssrc 自动推断的可靠来源 —— py-cord 2.8.0 下未映射 ssrc 的包
# 在 reader 里被丢弃前不会建 decoder, 所以旧的 decoders.keys() 推断已失效。
_seen_ssrcs_lock = threading.Lock()
_seen_ssrcs: set = set()

# DAVE 解密失败的**原因**计数。
#
# 为什么非要单独记一份：`_probed` 里那个 `except Exception: plain = None` 会把
# 失败的包换成一帧静音接着跑（跟 py-cord reader.py:315 是同一个套路，只不过这条
# 是我们自己写的）。davey 的 get_decryption_stats 只给出「失败了多少次」，
# **给不出为什么** —— 而「密钥轮换没跟上」和「包本身损坏」这两种下一步动作完全
# 不同。不记原因就只能盯着一个失败计数干瞪眼。
#
# 按原因字符串分桶而不是只留最后一条：失败往往是混合的，只看最后一条会把偶发的
# 那种当成主因。
_dave_fail_lock = threading.Lock()
_dave_fail_reasons: "collections.Counter[str]" = collections.Counter()


# 「成败 × 包形状」分桶。
#
# 实测失败原因**只有一种**：`DecryptionFailed(UnencryptedWhenPassthroughDisabled)`
# —— davey 认为这一帧根本没被 DAVE 加密。可是同一个人同一句话里另有 121 帧解开了，
# 所以「对端没加密」讲不通，更可能是**我们递进去的字节不对**。
#
# 两个候选差异，一次量两个，因为它们互相能证伪：
#
#   ① RTP 扩展头。Discord 只在一部分包上带扩展头（音量指示那些）。若失败整齐地
#      落在带扩展头那一栏，病因就定死了。
#   ② 帧尾两字节。DAVE 的「这是加密帧」标记在**帧尾**，不在帧头 —— 所以
#      `UnencryptedWhenPassthroughDisabled` 的字面意思就是「尾部没找到标记」。
#      成功帧与失败帧的尾字节一比，立刻能看出是**整类帧不带标记**（协议/版本问题）
#      还是**帧被截断了**（`reader.py:430` 那句来路不明的 `return result[8:]`）。
#
# 不写死期望值（比如「标记应该是 0xFAFA」）—— 只把实际观察到的分布打出来，
# 让数据自己说话。写死期望值等于把待验证的假设塞进了测量工具本身。
_dave_shape_stats: "collections.Counter[str]" = collections.Counter()


# Opus TOC 分桶：**发送端到底用了什么模式和带宽**。
#
# 起因：2026-09-11 Chris 说回放「码率低、失真」。测下来全天 10 份录音在
# 12 kHz 以上一律 0.000% 能量，一份例外都没有 —— 明确的硬墙。
#
# 12 kHz 这个数字**有两个都说得通的来源**，不能靠「听起来像常识」二选一：
#   ① Opus superwideband（RFC 6716 表 2：config 12-13 = Hybrid/SWB）
#   ② 某处 24 kHz 的重采样中间层（奈奎斯特恰好也是 12 kHz）
# ② 已经用读代码排除了：录音支路是 `tomono(48kHz stereo)` 直接落盘
# （`_utterance_write` 写 `setframerate(48000)`），中间没有任何 ratecv。
#
# 但排除②不等于证明①。真正的判据在**每个 Opus 包的第一个字节**：
# TOC 的高 5 位就是 config number，RFC 6716 表 2 把它一一对应到
# 模式 × 带宽 × 帧长。这是发送端自己声明的，不是我们从波形上反推的。
#
# 只在 DAVE 解密成功后记 —— 那时候才是真正的 Opus 明文。
_opus_toc_stats: "collections.Counter[str]" = collections.Counter()

# RFC 6716 §3.1 表 2。写成区间查表而不是 if-else 链，是为了跟 RFC 里那张表
# 逐行对得上，改错了一眼能看出来。
_OPUS_TOC_TABLE = [
    (0, 3, "SILK", "NB 4kHz"),
    (4, 7, "SILK", "MB 6kHz"),
    (8, 11, "SILK", "WB 8kHz"),
    (12, 13, "Hybrid", "SWB 12kHz"),
    (14, 15, "Hybrid", "FB 20kHz"),
    (16, 19, "CELT", "NB 4kHz"),
    (20, 23, "CELT", "WB 8kHz"),
    (24, 27, "CELT", "SWB 12kHz"),
    (28, 31, "CELT", "FB 20kHz"),
]


# 实际码率。TOC 只说「编码器允许多宽」，说不出「它真花了多少比特」——
# 2026-09-11 那次就是栽在这上面：TOC 报 fullband 20 kHz，可波形在 12 kHz
# 有个 22 dB 的坎。带宽档位是**上限**不是实际投入，两者要分开量。
#
# Discord 是 20 ms 一帧、50 帧/秒，所以 kbps = 平均载荷字节 × 8 × 50 / 1000。
# 只统计有效载荷（DAVE 解密后的 Opus 明文），不含 RTP 头和加密开销。
_opus_bytes_total = 0
_opus_frames_total = 0


def _record_opus_toc(plain: bytes | None) -> None:
    """记一帧 Opus 的 TOC（模式/带宽/声道）和载荷大小。空包和异常一律忽略。

    这条路在每个音频包上跑，所以刻意做成纯查表 + 一次加锁，不做任何解析。
    """
    global _opus_bytes_total, _opus_frames_total
    if not plain:
        return
    try:
        toc = plain[0]
        cfg = toc >> 3
        stereo = bool(toc & 0x04)
        mode = bandwidth = "?"
        for lo, hi, m, bw in _OPUS_TOC_TABLE:
            if lo <= cfg <= hi:
                mode, bandwidth = m, bw
                break
        key = f"{mode}/{bandwidth}/{'立体声' if stereo else '单声道'}/cfg{cfg}"
        with _dave_fail_lock:
            _opus_toc_stats[key] += 1
            _opus_bytes_total += len(plain)
            _opus_frames_total += 1
    except Exception:
        pass


# ── 每句话单独记一份 Opus 账 ────────────────────────────────────────────
#
# 上面那份 `_opus_toc_stats` 是**从进程启动起累计**的，所以它只能回答
# 「这场会话整体是什么模式」，回答不了「为什么第 1 句干净、第 6 句糊」。
#
# 2026-09-12 就卡在这里：同一次会话 19 句里只有 2 句 4-6 kHz 是正常的
# （比主频段低 20 dB），其余全部塌到低 30~39 dB。累计账全程报
# Hybrid/FB 20kHz、95 kbps 一动不动，于是「发送端到底做了什么不一样的事」
# 完全无从查起 —— 缺的不是分析，是**按句分组的原始数据**。
#
# 记两样，各回答一个问题：
#   ssrc  → 这句是从哪条发送连接来的。会话里同时挂着 11 个 ssrc，
#           如果那两句干净的来自不同 ssrc，就是「另一台设备/另一条连接」，
#           跟音频处理链无关。
#   字节  → 编码器**真花了多少比特**。TOC 只说「允许到 20 kHz」，是上限不是投入。
#           糊的那些如果照样 238 B/帧，说明比特花了却没内容，问题在编码器入口之前；
#           如果字节明显掉下去，那是编码器自己在降档，问题在发送端客户端。
_utt_opus: "collections.Counter[int]" = collections.Counter()   # ssrc → 帧数
_utt_opus_bytes = 0
_utt_lost = 0          # 本句 RTP 序列号缺口累计（= 真丢了多少帧）
_utt_gaps: "collections.Counter[int]" = collections.Counter()   # 缺口长度 → 出现次数
_utt_loss_pos: list = []   # [(已收帧数, 缺口长度)] —— 用来看丢包是不是往后半句堆

# ── RTCP：把「端到端丢包」拆成两段 ──────────────────────────────────────
#
# 上面数的序列号缺口是**端到端**的：缺一个号只说明这个包没到我这儿，
# 分不出是「手机 → Discord 服务器」丢的，还是「服务器 → 我们」丢的。
# 2026-09-12 Chris 直接问到这一点，而当时我确实回答不了。
#
# 答案一直躺在垃圾桶里：Discord 每隔几秒发一个 RTCP Sender Report，
# py-cord 只打一行「Received unexpected rtcp packet type=200」就扔了 ——
# 日志里已经攒了七万多条。那里面有两样正好缺的东西：
#
#   info.packet_count → 服务器**自称发了多少个**。跟我们实收数一比，
#                       「服务器 → 我们」这一段就单独量出来了。
#   reports[].total_lost / perc_loss
#                     → 报告块，讲的是**发这份报告的人自己收到了什么**。
#                       如果里面出现 Chris 上行那条流的 ssrc，那就是服务器
#                       亲口说它从手机那边丢了多少 —— 上行那一段的直接证据。
#
# 先只记不判：报告块里到底出现哪些 ssrc 得看真实数据，靠猜会猜错。
_rtcp_sr: dict = {}     # 发报方 ssrc → (packet_count, octet_count)
_rtcp_rr: dict = {}     # 被报告的 ssrc → (perc_loss_8bit, total_lost, last_seq)


def _utt_opus_note(ssrc: int, nbytes: int) -> None:
    """在每个成功解密的包上跑，所以只做加法，不做任何解析。"""
    global _utt_opus_bytes
    with _dave_fail_lock:
        _utt_opus[ssrc] += 1
        _utt_opus_bytes += nbytes


def _utt_loss_note(gap: int) -> None:
    """记一次 RTP 序列号缺口。

    为什么非要数序列号：2026-09-12 Chris 手动在 iPhone 上从 96k 一路降到 8k，
    想验证「码率太高时 5G 上行拥塞反而传不过来」。这个假设**只能靠丢包数回答**，
    而我一开始想用「每秒收到多少帧」去估，算出来有超过 50 帧/秒的——
    物理上不可能（20 ms 一帧就是 50 帧/秒封顶），说明时间窗对不齐，那条路是死的。

    序列号不一样：它是发送端自己打的连续编号，缺一个就是丢一个，不受采样窗口影响。

    注意别跟 `_fec_recover_n` 混为一谈 —— 那个只统计「FEC 成功补回来的」，
    gap 超过 50 或者 FEC 关着的时候一个都不记。这里要的是**丢了多少**，
    不是**救回来多少**，所以无条件先记。

    除了总数还要记**缺口长度的分布**，因为这一项直接决定「要不要把 FEC 打开」：
    Opus 的带内 FEC（LBRR）是把**上一帧**的低码率副本塞进当前包里，所以
    一个收到的包只能往回补**一帧**。
      gap=1  → 后面那个包里带着它的副本，能补回来
      gap≥2  → 这一串里只有最后一帧补得回来，前面的彻底没了
    也就是说 FEC 的上限收益 = 「gap=1 的次数 ÷ 总丢帧数」。
    只看 19% 这个总数是决定不了开不开的 —— 全是长串的话开了也基本白开，
    而它要多花约 20~30% 的码率，在本来就拥塞的上行上反而可能是负收益。

    还要记**这个缺口出现在句子的第几段**。2026-09-12 Chris 报的现象是
    「句子说长了，前半句还行，后半句开始着急、草草发完」，并猜是 5G 上行
    发着发着就堵了。这个猜测有个很锐利的预言：丢包应该**往后半句堆**。
    总丢包率是把整句摊平的，正好把这个信号平均掉 —— 所以要按位置分段记。

    位置用「到目前为止收了多少帧」表示，不用墙钟时间：说话有停顿，
    墙钟会把停顿也算进去，而我们要问的是「说到这句的几成时开始丢」。
    """
    global _utt_lost
    if gap <= 0:
        return
    with _dave_fail_lock:
        _utt_lost += gap
        _utt_gaps[gap if gap <= 4 else (10 if gap <= 10 else 99)] += 1
        _utt_loss_pos.append((sum(_utt_opus.values()), gap))


def _utt_opus_take() -> str:
    """取走并清空本句的账。清空是必须的 —— 不清就又变成累计账，等于没加。"""
    global _utt_opus_bytes, _utt_lost
    with _dave_fail_lock:
        items = _utt_opus.most_common(3)
        n = sum(_utt_opus.values())
        b = _utt_opus_bytes
        lost = _utt_lost
        gaps = dict(_utt_gaps)
        pos = list(_utt_loss_pos)
        _utt_opus.clear()
        _utt_opus_bytes = 0
        _utt_lost = 0
        _utt_gaps.clear()
        _utt_loss_pos.clear()
    if not n:
        return "无包"
    src = ",".join(f"{s}×{c}" for s, c in items)
    # 丢包率分母用「收到 + 丢掉」，也就是发送端本来打算发的总数。
    # 拿收到数当分母会把丢包率算小，丢得越狠低估越多。
    loss = f" 丢{lost}({lost/(n+lost)*100:.1f}%)" if lost else " 丢0"
    if gaps:
        _名 = {1: "1", 2: "2", 3: "3", 4: "4", 10: "5-10", 99: ">10"}
        loss += " 缺口[" + " ".join(
            f"{_名[k]}×{gaps[k]}" for k in sorted(gaps)) + "]"
    # 分四段看丢包落在句子的哪一截。分母是每段**本该有的帧数**
    # （该段收到的 + 该段丢掉的），不是整句平均 —— 否则丢得多的那段
    # 因为收得少，反而显得占比小，把要找的趋势正好抹反。
    if pos and n >= 40:
        q = [[0, 0] for _ in range(4)]          # [收到, 丢掉]
        for i in range(n):
            q[min(3, i * 4 // n)][0] += 1
        for at, g in pos:
            q[min(3, at * 4 // n)][1] += g
        loss += " 分段" + "/".join(
            f"{(l/(r+l)*100):.0f}%" if (r + l) else "-" for r, l in q)
    return (f"ssrc={src} {n}帧 均{b/n:.0f}B/帧≈{b*8*50/n/1000:.0f}kbps{loss}"
            f" | {_rtcp_summary()}")


def _rtcp_summary() -> str:
    """把服务器自己报的账摊开。**不做减法，只并排放。**

    很想直接算「端到端丢包 − 服务器→我们丢包 = 上行丢包」，但那个减法暂时不能做：
    SR 的 packet_count 是**从会话开始累计**的，而我们的帧数是按句清零的，
    两个口径不一样，相减出来是个没有意义的数。要拆段得先把 SR 也做成差分，
    等看清报告块里到底有哪些 ssrc 再决定怎么对齐 —— 先把原始数摆出来。
    """
    with _dave_fail_lock:
        sr = dict(_rtcp_sr)
        rr = dict(_rtcp_rr)
    if not sr and not rr:
        return "RTCP 无"
    part = []
    if sr:
        part.append("服务器自称发" + ",".join(
            f"{s}:{c[0]}包" for s, c in sr.items()))
    if rr:
        # perc_loss 是 8 bit 定点小数（RFC 3550 §6.4.1），除以 256 才是比例
        part.append("报告块" + ",".join(
            f"{s}:丢{v[1]}({v[0]/256*100:.1f}%)" for s, v in rr.items()))
    return " ".join(part)


def _opus_toc_summary(top: int = 4) -> str:
    with _dave_fail_lock:
        items = _opus_toc_stats.most_common(top)
        n, b = _opus_frames_total, _opus_bytes_total
    if not items:
        return "-"
    # 平均值单独打出来，不要只打 kbps —— 静音帧只有几个字节，会把均值拉低，
    # 看得到字节数才判得出「码率低」是真低还是被静音帧稀释的。
    rate = f" 均{b/n:.0f}B/帧≈{b*8*50/n/1000:.0f}kbps" if n else ""
    return " ".join(f"{k}×{c}" for k, c in items) + rate


class DavePyDecryptFailed(Exception):
    """dave-py 的 Decryptor 返回了 None。

    单独立一个类型是为了在账本的「失败原因」栏里跟 davey 的 `DecryptionFailed`
    区分开 —— 两个后端并排比时，混成一个 `Exception` 就分不清是谁在报。
    """


def _strip_rtp_padding(padding: bool, payload: bytes) -> bytes:
    """P 位置起时切掉 RTP 尾部填充，再交给 DAVE。

    RTP 头第一个字节的 **P 位**（`0b00100000`）表示「这个包尾部有填充」，
    最后一个字节写着填了多少字节（**含它自己**，RFC 3550 §5.1）。

    **py-cord 解析了 `packet.padding` 但从来不切** —— `packets/rtp.py:100`
    只赋了个值，全仓库没有第二处引用。它原来那条路把 payload 直接喂 Opus
    解码器，Opus 能容忍尾巴上的垃圾，所以不切也没人发现。**DAVE 容忍不了**：
    它的「我是加密帧」标记压在**帧尾**，填充盖在标记上面，marker 就找不着了，
    于是报 `UnencryptedWhenPassthroughDisabled` —— 字面意思正是
    「尾部没找到标记」。

    实测失败帧的末 16 字节全是同一个值，而那个值恰好是填充长度
    （`0x11`→17 个 0x11，`0x2f`→47 个 0x2f）—— PKCS#7 式填充的教科书形态。
    **密文不可能长成这样**，这是判定的关键证据。

    只在 P 位置起时动手，长度还要落在合法区间。**不做「尾部有连续相同字节
    就切」的猜测** —— 那会把恰好如此的合法密文切坏，而切坏的表现同样是一帧
    静音，两种病因在日志里长得一模一样，等于自己给自己埋雷。

    **实测验证（2026-09-11 17:18 HKT，dave-py 后端，同一位说话人同类语句）**：

        改前  成功577 / 失败29 / 透传0    包形状里根本没有「有填充」这一维
        改后  成功290 / 失败 0 / 透传0    有填充/尾fafa/成功×43

    改后那 43 帧带填充的全部解开，失败清零 —— 「有填充」桶的出现和「失败」桶
    的消失是同一次测量里同时发生的，所以不是碰巧好了。

    **透传恒为 0 是这条结论的另一半**：dave-py 是真在解密，不是绕过去当明文
    放行。所以官方 davey 的病因也不是「透传被关了」，而是同一处没切填充 ——
    这个切填充的动作在 `_probed` 里，两个后端共用一条路。

    **官方 davey 回归（2026-09-11 17:47 HKT，`DAVE_PY_BACKEND=0`）**：

        成功370 / 失败0 / 透传0    包形状：无填充/尾fafa×370 无填充/尾fffe×10
        丢包告警 0（本进程）        全零帧 8/293 ≈ 2.7%（切换前 25/531 ≈ 4.7%）

    官方 davey 从约 40% 失败变成零失败，dave-py 可以退役了。

    **但这一轮不能单独用来证明「是切填充治好的官方库」** —— 它一帧带填充的都
    没收到，切填充这段代码压根没被触发。它证明的是「官方库现在好了」，不是
    「好是因为它」。旁证在帧尾：官方库当年失败那批的尾巴是 `ffff` / `0e0e` /
    `1919` / `1010`，**每一个都是同一字节重复两遍** —— 填充是一串相同字节，
    只看末两位看到的就是双写，这正是填充的指纹。要钉死得等下次真出现带填充
    的帧，把这个函数临时短路，看失败会不会回来。

    为什么这一轮一帧填充都没有，未知。填充是发送端按需要加的，可能跟客户端
    这次协商的码率或网络状况有关 —— **没查实之前不编理由**。

    **补上一半（2026-09-11 18:29 HKT，官方 davey）**：

        成功255 / 失败0 / 透传0     hits=216   全零帧 15
        包形状：有扩展头/无填充/尾fafa×252
                有扩展头/无填充/尾fffe×16
                **有扩展头/有填充/尾fafa/成功×3**   ← 第一次踩到

    官方 davey 上首次真的收到带填充的帧，走了这个函数，全部解开。上一轮那句
    「这段代码压根没被触发」到此不再成立。

    **仍然不是完整的因果证明** —— 只有 3 帧，而且没做反向对照。要钉死得在
    带填充的帧出现时把这个函数临时短路，看失败会不会回来。Chris 09-11 18:36
    判断不值得为此牺牲几十秒音质，所以这条留白是**知情的选择，不是遗漏**。
    """
    if not padding or not payload:
        return payload
    pad_len = payload[-1]
    if pad_len < 1 or pad_len > len(payload):
        return payload          # 越界 = P 位不可信，原样放行比切坏强
    return payload[:-pad_len]


def _record_dave_result(
    ok: bool,
    extended: bool,
    payload: bytes | None,
    exc: BaseException | None = None,
    padded: bool = False,
) -> None:
    tail = payload[-2:].hex() if payload and len(payload) >= 2 else "??"
    key = (
        f"{'有扩展头' if extended else '无扩展头'}"
        f"/{'有填充' if padded else '无填充'}"
        f"/尾{tail}/{'成功' if ok else '失败'}"
    )
    with _dave_fail_lock:
        _dave_shape_stats[key] += 1
        if exc is not None:
            _dave_fail_reasons[f"{type(exc).__name__}: {str(exc)[:60]}"] += 1


def _dave_fail_summary(top: int = 3) -> str:
    """失败原因 Top-N，进诊断日志。没有失败就返回 '-'（别打噪音）。"""
    with _dave_fail_lock:
        items = _dave_fail_reasons.most_common(top)
    return " | ".join(f"{r}×{n}" for r, n in items) or "-"


def _dave_shape_summary(top: int = 6) -> str:
    """包形状 Top-N，进诊断日志。一个都没有就返回 '-'。

    top 给到 6 是因为**至少要能同时看到成功桶和失败桶** —— 只打 3 个的话，
    失败占多数时会把成功那栏整个挤掉，而这个测量的全部意义就在于两栏对比。
    """
    with _dave_fail_lock:
        items = _dave_shape_stats.most_common(top)
    return " ".join(f"{k}×{n}" for k, n in items) or "-"


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
            # 音色跟 token / 频道号一样是**每 bot 一份**的配置，就该跟它们放在一起。
            # 以前它是 run.sh 里的一个 export，而环境变量是会被继承的 —— 谁先起过
            # bunny，同一个 shell 再起别人就全变成 Aoede，五个 bot 一个声音且无任何报错。
            "tts_voice": str(discord_cfg.get("tts_voice") or ""),
        }
    except Exception as e:
        log.warning("读取 Discord sidecar 配置失败 (non-fatal): %s", e)
        return None



async def _resolve_voice_channel(bot, voice_channel_id: str):
    """解析常驻语音频道。**只认显式配置**，解析不到就报错返回 None。

    以前这里有两层静默兜底：没配就用一个硬编码的公共频道，配了但解析不到就抓
    guild 里的第一个语音频道。两层都是「悄悄连到别的地方」—— 频道号打错一位，
    bot 会一声不吭地待在另一个房间，日志里一切正常，只能靠耳朵发现。
    现在三个 bot 的频道号都显式配在 Firestore 里，没有谁需要靠猜。

    缓存未命中单独处理：get_channel 走本地缓存，可能只是还没同步；那种情况
    补一次 API fetch，而不是退回去随便找一个频道。
    """
    import discord

    if not voice_channel_id:
        log.error("未配置 voice_channel_id（Firestore bots/<bot>.channels.discord），"
                  "不连任何语音频道 —— 宁可不出声，也不要连错房间")
        return None
    try:
        cid = int(voice_channel_id)
    except (ValueError, TypeError):
        log.error("voice_channel_id 不是数字: %r", voice_channel_id)
        return None

    ch = bot.get_channel(cid)
    if ch is None:
        try:
            ch = await bot.fetch_channel(cid)
        except Exception as e:
            log.error("语音频道 %s 取不到（不存在？没权限？）: %s", cid, e)
            return None
    if not isinstance(ch, discord.VoiceChannel):
        log.error("频道 %s 不是语音频道，实际是 %s", cid, type(ch).__name__)
        return None
    return ch


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
    # 先把**服务端**的语音状态清干净，再连。
    #
    # 上面那个 vc.disconnect 只管本地对象；进程刚起来时 guild.voice_client 是 None,
    # 于是什么都没清 —— 可 Discord 那边完全可能还记着上个进程留下的语音状态
    # (exit-42 重启没走完优雅下线, 或者上一次握手半途超时)。这时候再发
    # VOICE_STATE_UPDATE, Discord 认为状态没变化, **不回 VOICE_SERVER_UPDATE**,
    # py-cord 就死等在 got_both_voice_updates 上, 20s 后超时 —— 而且会一直这样,
    # 心跳重试多少次都一样, 因为每次重试都撞同一个幽灵。
    #
    # 2026-09-11 实测: bunny 连续 6 次握手超时, 堆栈全停在 _wait_for_state
    # (got_both_voice_updates)。发一次 channel=None 等于告诉 Discord「先当我不在」,
    # 之后那次 join 才是一次真正的状态变更。
    #
    # 没有幽灵时这一步是无害的空操作 —— 上面已经 return 掉了「已连上」的情况。
    try:
        await guild.change_voice_state(channel=None)
        await asyncio.sleep(1.0)   # 给服务端一点时间落状态
    except Exception:
        log.debug("清服务端语音状态失败 (忽略, 继续尝试连接)", exc_info=True)
    try:
        vc = await ch.connect(timeout=20.0, reconnect=True)
    except Exception:
        log.exception("常驻语音频道连接失败")
        return None
    for _ in range(50):  # 等握手 (UDP + DAVE) 真完成
        if vc.is_connected():
            break
        await asyncio.sleep(0.2)
    return vc if vc.is_connected() else None


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
    global _autostart_done, _voice_reconnecting
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
                # 守护循环那边正在重建连接 —— 现在的「掉线」是它制造的中间态,
                # 插一脚进去只会把它的握手掐断 (见 _voice_reconnecting 那段注释)。
                if _voice_reconnecting:
                    continue
                log.warning("检测到 voice 掉线，尝试自动 rejoin 常驻频道…")
                _voice_reconnecting = True
                try:
                    vc = await _ensure_connected()
                finally:
                    _voice_reconnecting = False
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


_tts_client_per_loop: dict = {}  # per-event-loop genai client (aiohttp session 绑定 loop)

async def _generate_batch_pcm(client, model, config, batch: str, voice: str,
                              idx: int, total: int) -> tuple[bytes, str]:
    """生成单个 batch 的完整 48kHz stereo PCM (含缓存检查 + retry + Qwen3 fallback)。"""
    cached = _cache_get_pcm(batch, voice)
    if cached is not None:
        log.info("TTS 批 #%d/%d: cache hit (%dc → %.1fs)",
                 idx, total, len(batch), len(cached) / 4 / 48000)
        return cached, "cache"

    import time as _t_mod
    chunks_24k = []
    last_finish = None
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            _t_api = _t_mod.monotonic()
            log.info("TTS API 调用 (prefetch): 批 #%d/%d, %dc, attempt %d",
                     idx, total, len(batch), attempt + 1)
            stream = await client.aio.models.generate_content_stream(
                model=model, contents=batch, config=config
            )
            _t_first_chunk = None
            async for chunk in stream:
                for cand in getattr(chunk, "candidates", None) or []:
                    fr = getattr(cand, "finish_reason", None)
                    if fr is not None:
                        last_finish = fr
                    content = getattr(cand, "content", None)
                    for part in getattr(content, "parts", None) or []:
                        inline = getattr(part, "inline_data", None)
                        if inline and inline.data:
                            if _t_first_chunk is None:
                                _t_first_chunk = _t_mod.monotonic()
                                log.info("TTS API 首帧 (prefetch): 批 #%d/%d, TTFB=%.0fms, %dc",
                                         idx, total, (_t_first_chunk - _t_api) * 1000, len(batch))
                            chunks_24k.append(bytes(inline.data))
            if chunks_24k:
                log.info("TTS API 完成 (prefetch): 批 #%d/%d, 总耗时=%.0fms, %dc",
                         idx, total, (_t_mod.monotonic() - _t_api) * 1000, len(batch))
                break
            log.warning("TTS 批 #%d/%d Gemini 返回 0 字节 (%.0fms, finish=%s), retry %d/%d",
                        idx, total, (_t_mod.monotonic() - _t_api) * 1000,
                        last_finish, attempt + 1, max_retries)
            if attempt < max_retries:
                await asyncio.sleep(1 + attempt)
        except Exception as exc:
            if attempt < max_retries:
                log.warning("TTS 批 #%d/%d 失败(%.0fms, retry %d/%d): %s",
                            idx, total, (_t_mod.monotonic() - _t_api) * 1000,
                            attempt + 1, max_retries, exc)
                await asyncio.sleep(1 + attempt)
            else:
                log.error("TTS 批 #%d/%d Gemini 最终失败 (%.0fms), fallback Qwen3 (%dc)",
                          idx, total, (_t_mod.monotonic() - _t_api) * 1000, len(batch))
    if not chunks_24k:
        try:
            log.info("TTS 批 #%d/%d fallback → Qwen3 (%dc)", idx, total, len(batch))
            async for pcm_chunk in _qwen3_tts_stream(batch):
                chunks_24k.append(pcm_chunk)
            last_finish = "Qwen3-fallback"
        except Exception as qe:
            log.error("TTS 批 #%d/%d Qwen3 fallback 也失败: %s", idx, total, qe)

    pcm_24k = b"".join(chunks_24k)
    if pcm_24k:
        pcm48, _ = audioop.ratecv(pcm_24k, 2, 1, 24000, 48000, None)
        stereo = audioop.tostereo(pcm48, 2, 1, 1)
        _cache_save_pcm(batch, voice, stereo)
        log.info("TTS 批 #%d/%d: %dc → %.1fs 音频 finish=%s",
                 idx, total, len(batch), len(stereo) / 4 / 48000, last_finish)
        return stereo, str(last_finish)
    log.info("TTS 批 #%d/%d: %dc → 0s 音频 finish=%s", idx, total, len(batch), last_finish)
    return b"", str(last_finish)


async def _gemini_tts_stream(text: str):
    """分批流式调 Gemini TTS, 逐 chunk yield 48kHz stereo s16 PCM bytes。

    内部完成 24kHz mono → 48kHz stereo 转换, caller 直接 write 不需转换。
    优化: PCM 缓存 (命中跳过 API) + 批次预取 (后台并行生成下一批)。
    """
    import time as _t_mod
    from google.genai import types as gt
    from .gemini_tts import _build_genai_client, _clean_text_for_tts

    cleaned = _clean_text_for_tts(text)
    if not cleaned.strip():
        return
    model = os.environ.get("TTS_MODEL", "gemini-3.1-flash-tts-preview")
    voice = tts_voice()
    loop_id = id(asyncio.get_running_loop())
    client = _tts_client_per_loop.get(loop_id)
    if client is None:
        client = _build_genai_client(None)
        _tts_client_per_loop[loop_id] = client
        log.info("TTS genai client 创建: loop=%x", loop_id)
        try:
            _warmup_config = gt.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=gt.SpeechConfig(
                    voice_config=gt.VoiceConfig(
                        prebuilt_voice_config=gt.PrebuiltVoiceConfig(voice_name=voice)
                    ),
                    language_code="zh-CN",
                ),
            )
            _warmup_stream = await client.aio.models.generate_content_stream(
                model=model, contents="。", config=_warmup_config
            )
            async for _wc in _warmup_stream:
                break
            log.info("TTS warmup 完成")
        except Exception as _we:
            log.warning("TTS warmup 失败 (non-fatal): %s", _we)
    config = gt.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=gt.SpeechConfig(
            voice_config=gt.VoiceConfig(
                prebuilt_voice_config=gt.PrebuiltVoiceConfig(voice_name=voice)
            ),
            language_code="zh-CN",
        ),
    )
    batches = _plan_tts_batches(cleaned)
    n = len(batches)
    log.info("TTS 分批: %dc → %d 批 (首批 %dc)", len(cleaned), n,
             len(batches[0]) if batches else 0)

    current_prefetch = None  # asyncio.Task for the batch we're about to yield

    for idx in range(n):
        batch = batches[idx]

        if current_prefetch is not None:
            # This batch was prefetched — await result and yield
            pcm, _ = await current_prefetch
            current_prefetch = None
            if pcm:
                yield pcm
            # Prefetch NEXT batch AFTER current finishes (avoids concurrent API calls)
            if idx + 1 < n:
                await asyncio.sleep(0.05)
                current_prefetch = asyncio.create_task(
                    _generate_batch_pcm(client, model, config, batches[idx + 1],
                                        voice, idx + 2, n)
                )
        else:
            # First batch (or no prefetch): check cache, then stream for low latency
            cached = _cache_get_pcm(batch, voice)
            if cached is not None:
                log.info("TTS 批 #%d/%d: cache hit (%dc → %.1fs)",
                         idx + 1, n, len(batch), len(cached) / 4 / 48000)
                yield cached
            else:
                pcm_accum = []
                last_finish = None
                _cv_state = None
                max_retries = 2
                for attempt in range(max_retries + 1):
                    try:
                        _t_api = _t_mod.monotonic()
                        log.info("TTS API 调用: 批 #%d/%d, %dc, attempt %d",
                                 idx + 1, n, len(batch), attempt + 1)
                        stream = await client.aio.models.generate_content_stream(
                            model=model, contents=batch, config=config
                        )
                        _t_first_chunk = None
                        async for chunk in stream:
                            for cand in getattr(chunk, "candidates", None) or []:
                                fr = getattr(cand, "finish_reason", None)
                                if fr is not None:
                                    last_finish = fr
                                content = getattr(cand, "content", None)
                                for part in getattr(content, "parts", None) or []:
                                    inline = getattr(part, "inline_data", None)
                                    if inline and inline.data:
                                        if _t_first_chunk is None:
                                            _t_first_chunk = _t_mod.monotonic()
                                            log.info("TTS API 首帧: 批 #%d/%d, TTFB=%.0fms, %dc",
                                                     idx + 1, n, (_t_first_chunk - _t_api) * 1000, len(batch))
                                        d = bytes(inline.data)
                                        pcm48, _cv_state = audioop.ratecv(d, 2, 1, 24000, 48000, _cv_state)
                                        stereo = audioop.tostereo(pcm48, 2, 1, 1)
                                        pcm_accum.append(stereo)
                                        yield stereo
                        if pcm_accum:
                            log.info("TTS API 完成: 批 #%d/%d, 总耗时=%.0fms, %dc → %.1fs音频",
                                     idx + 1, n, (_t_mod.monotonic() - _t_api) * 1000,
                                     len(batch), sum(len(c) for c in pcm_accum) / 4 / 48000)
                            break
                        log.warning("TTS 批 #%d/%d 返回 0 字节 (%.0fms), retry %d/%d",
                                    idx + 1, n, (_t_mod.monotonic() - _t_api) * 1000,
                                    attempt + 1, max_retries)
                        if attempt < max_retries:
                            await asyncio.sleep(1 + attempt)
                    except Exception as exc:
                        if attempt < max_retries:
                            log.warning("TTS 批 #%d/%d 失败(%.0fms, retry %d): %s",
                                        idx + 1, n, (_t_mod.monotonic() - _t_api) * 1000,
                                        attempt + 1, exc)
                            await asyncio.sleep(1 + attempt)
                        else:
                            log.error("TTS 批 #%d/%d Gemini 最终失败 (%.0fms)",
                                      idx + 1, n, (_t_mod.monotonic() - _t_api) * 1000)
                if not pcm_accum:
                    _cv_state2 = None
                    try:
                        async for pcm24 in _qwen3_tts_stream(batch):
                            pcm48, _cv_state2 = audioop.ratecv(pcm24, 2, 1, 24000, 48000, _cv_state2)
                            stereo = audioop.tostereo(pcm48, 2, 1, 1)
                            pcm_accum.append(stereo)
                            yield stereo
                    except Exception:
                        pass
                full_stereo = b"".join(pcm_accum)
                if full_stereo:
                    _cache_save_pcm(batch, voice, full_stereo)
                log.info("TTS 批 #%d/%d: %dc → %.1fs finish=%s",
                         idx + 1, n, len(batch),
                         len(full_stereo) / 4 / 48000 if full_stereo else 0, last_finish)
            # First batch done, start prefetch for next (sequential, no concurrent API)
            if idx + 1 < n:
                await asyncio.sleep(0.05)
                current_prefetch = asyncio.create_task(
                    _generate_batch_pcm(client, model, config, batches[idx + 1],
                                        voice, idx + 2, n)
                )


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
    """gRPC 双向流调 Cloud TTS (Chirp3-HD-Orus), 逐 chunk yield 48kHz stereo s16 PCM."""
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

    _cv_state = None
    while True:
        try:
            item = pcm_q.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.01)
            continue
        if item is _sentinel:
            break
        pcm48, _cv_state = audioop.ratecv(item, 2, 1, 24000, 48000, _cv_state)
        yield audioop.tostereo(pcm48, 2, 1, 1)


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

        _IDLE_STOP_FRAMES = 100  # 2s of silence (100 × 20ms) → stop playback

        def __init__(self, fid: str = "", persistent: bool = False):
            self._buf = bytearray()
            self._lock = threading.Lock()
            self._finished = False
            self._persistent = persistent  # True: idle 后自动停播，新音频到时重新 play
            self._written = 0   # 累计写入字节 (诊断 + 进度总长)
            self._real = 0      # 派发真音频帧数 (诊断 + 进度已播)
            self._silence = 0   # 派发欠载静音帧数 (诊断)
            self._consec_silence = 0  # 连续静音帧计数 (idle 检测)
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
                    self._consec_silence = 0
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
                self._consec_silence += 1
                if self._persistent and self._consec_silence >= self._IDLE_STOP_FRAMES and not _tts_active:
                    log.info("持久 source idle 2s, 停播释放 speaking 状态 "
                             "(真帧 %d, 静音帧 %d)", self._real, self._silence)
                    return b""
                return b"\x00" * self.FRAME

        def is_opus(self) -> bool:
            return False

    _SOURCE_CLASS = _StreamPCMSource
    return _SOURCE_CLASS


_persistent_source = None
_tts_interrupted = False  # barge-in: LiveKit 打断时设 True，_do_speak 检查后停止生成
_tts_active = False       # _do_speak 运行中: 抑制 source idle 停播 (防批间间隔触发 idle)

def _get_persistent_source():
    """获取或创建持久 source。idle 2s 自动停播, 新音频到时重新 vc.play()。"""
    global _persistent_source
    bot = _sidecar_bot
    if bot is None or not bot.guilds:
        return None
    vc = bot.guilds[0].voice_client
    if vc is None or not vc.is_connected():
        _persistent_source = None
        return None
    if _persistent_source is not None:
        if vc.is_paused():
            # 上一轮被暂停了 — stop 彻底停掉旧音频，不 resume（用户已暂停说明不想听了）。
            # 清空 buffer + 重新 play，让新一轮的 TTS 从头开始。
            vc.stop()
            _persistent_source._consec_silence = 0
            _persistent_source._written = 0
            _persistent_source._real = 0
            _persistent_source._silence = 0
            _persistent_source.clear()
            try:
                vc.play(_persistent_source)
                log.info("持久 source: 旧暂停已 stop, 重新 play (新一轮对话)")
            except Exception:
                log.exception("持久 source stop→play 失败, 重建")
                _persistent_source = None
                return None
            return _persistent_source
        if vc.is_playing():
            return _persistent_source
        # idle 停播后重新 play: 清空残留 buffer + reset idle 计数器
        _persistent_source._consec_silence = 0
        _persistent_source._written = 0
        _persistent_source._real = 0
        _persistent_source._silence = 0
        _persistent_source.clear()  # 丢掉上一轮残留的 PCM，防止播旧音频
        try:
            vc.play(_persistent_source)
            log.info("持久 source idle 后唤醒, 重新 vc.play() (buffer已清)")
            return _persistent_source
        except Exception:
            log.exception("持久 source 唤醒失败, 重建")
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

    global _tts_active
    _tts_interrupted = False  # 新一轮生成，重置中断标志
    _tts_active = True        # 抑制 source idle 停播

    tts_backend = backend or os.environ.get("DISCORD_TTS_BACKEND", "gemini")

    buf_f = None
    bpath = _buf_path(fid)
    if bpath:
        try:
            os.makedirs(_BUF_DIR, exist_ok=True)
            _maybe_gc_buf_dir()      # 顺带回收过期缓存, 自带每小时节流
            buf_f = open(bpath, "wb")
        except Exception:
            log.exception("打开 buffer 落盘文件失败: %s", bpath)
            buf_f = None

    # 决定输出路径: Discord vc 连着 → Discord play; 否则 → Zello playback loop
    _dc_connected = is_voice_connected()
    try:
        from . import zello_voice_sidecar as _zsv
        _zello_online = _zsv.is_connected()
    except Exception:
        _zello_online = False
    _use_dc = _dc_connected
    _use_zl = not _dc_connected and _zello_online

    try:
        wrote = 0
        t_first_pcm = None

        if tts_backend == "qwen3":
            segments = _split_by_emotion(text)
            state = None
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
                        if _use_dc:
                            source = _get_persistent_source() or source
                    pcm48, state = audioop.ratecv(pcm24, 2, 1, 24000, 48000, state)
                    stereo = audioop.tostereo(pcm48, 2, 1, 1)
                    if _use_dc:
                        source.write(stereo)
                    elif _use_zl:
                        _zsv.zello_buf_write_threadsafe(stereo)
                    wrote += len(stereo)
                    if buf_f is not None:
                        buf_f.write(stereo)
        else:
            if tts_backend == "cloud_tts":
                tts_stream = _cloud_tts_stream(text)
            else:
                tts_stream = _gemini_tts_stream(text)
            async for stereo in tts_stream:
                if _tts_interrupted:
                    log.info("TTS 被打断(barge-in), 停止生成: %dc已写, %s", wrote, text[:30])
                    break
                if t_first_pcm is None:
                    t_first_pcm = _time.monotonic()
                    log.info("TTS 延迟: TTFB=%.0fms (text→首帧PCM), %dc, %s",
                             (t_first_pcm - t_start) * 1000, len(text), text[:30])
                    if _use_dc:
                        source = _get_persistent_source() or source
                if _use_dc:
                    source.write(stereo)
                elif _use_zl:
                    _zsv.zello_buf_write_threadsafe(stereo)
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
            _set_progress(fid, total=wrote, active=False)
        if _use_zl:
            try:
                _zsv.zello_signal_done_threadsafe()
            except Exception:
                pass

    _tts_active = False  # TTS 生成结束，允许 source idle 停播

    # 等本次写入的音频播完再返回（扣除生成期间已播放的时间）
    if wrote > 0 and not _tts_interrupted:
        play_dur = wrote / 4 / 48000
        elapsed = _time.monotonic() - t_start
        remain = play_dur - elapsed
        if remain > 0:
            await asyncio.sleep(remain)


_current_speak_task: "asyncio.Task | None" = None


async def _speak_consumer():
    """单 consumer loop: 从队列取 item，串行生成+播放。

    reply 入队时已清洗过期 hint (生产者侧)，consumer 这边再做一次兜底：
    取到 reply 时把队列里剩余 hint 也清掉（防 put 和 flush 之间的竞态窗口）。
    """
    global _current_speak_task
    import time as _time
    while True:
        item = await _speak_queue.get()
        if item.is_reply:
            _flush_hints_from_queue()
        queue_wait = (_time.monotonic() - item.enqueue_time) * 1000 if item.enqueue_time else 0
        # 丢弃过期的 hint（工具提示），但 reply（正文内容）永不丢弃。
        # 用户暂停播放时，当前 _do_speak 会阻塞，reply 在队列中等待可能超过 8s。
        # 如果丢弃 reply，用户恢复后就听不到正文了。
        if queue_wait > 8000 and not item.is_reply:
            log.info("TTS 丢弃过期 hint (%.0fms): %s", queue_wait, item.text[:30])
            continue
        if queue_wait > 50:
            log.info("TTS 排队等待: %.0fms, %s", queue_wait, item.text[:30])
        try:
            _current_speak_task = asyncio.current_task()
            await _do_speak(item.text, item.fid, backend=item.backend)
        except asyncio.CancelledError:
            log.info("TTS _do_speak 被 cancel (barge-in): %s", item.text[:30])
        except Exception:
            log.exception("_speak_consumer: _do_speak 异常")
        finally:
            _current_speak_task = None


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
        if fid and _feishu_ref is not None and _feishu_loop is not None and _feishu_chat_id:
            _notify_feishu_voice_card(fid)
        return True
    except Exception:
        log.exception("stream_speak_text 跨线程调度失败")
        return False


_ipc_server = None


def _ipc_sock_path(bot_name: str) -> str:
    return f"/tmp/closecrab-voice-{bot_name}.sock"


async def _start_ipc_listener(bot_name: str):
    """Unix socket 入口：本机外部进程把一行 JSON 推进直播流。

    watch-task 探针是独立进程，拿不到 sidecar 的 in-process 队列，只能走这里。
    协议: 请求 {"text": "...", "fid": "", "backend": ""} 一行；响应 {"ok": bool} 一行。
    fid 非空 → 按 reply 入队（不会因排队超时被丢、也不会被后来的 reply 冲掉），
    结论类播报该带上；状态类播报留空当 hint，过期被丢是对的。
    """
    global _ipc_server
    import json as _json

    path = _ipc_sock_path(bot_name)
    try:
        os.unlink(path)
    except OSError:
        pass

    async def _handle(reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            payload = _json.loads(raw.decode("utf-8"))
            text = (payload.get("text") or "").strip()
            ok = False
            if text and _speak_queue is not None and is_voice_connected():
                await _enqueue_speak(text, str(payload.get("fid") or ""),
                                     backend=payload.get("backend", ""))
                ok = True
            writer.write((_json.dumps({"ok": ok}) + "\n").encode())
            await writer.drain()
        except Exception:
            log.exception("voice IPC 处理失败")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    try:
        _ipc_server = await asyncio.start_unix_server(_handle, path=path)
        os.chmod(path, 0o600)
        log.info("voice IPC listener 已启动: %s", path)
    except Exception:
        log.exception("voice IPC listener 启动失败 (不影响 TTS 主路径)")


def _notify_feishu_voice_card(fid: str):
    """通知飞书发语音控制卡片 (复用已有的 _build_voice_control_card)。"""
    feishu = _feishu_ref
    feishu_loop = _feishu_loop
    open_id = _feishu_open_id
    chat_id = _feishu_chat_id
    if not feishu or not feishu_loop or not chat_id:
        return
    feishu._voice_cards[fid] = "pending"  # 占位防 _send_voice_summary 重复发
    import asyncio
    async def _send_card():
        try:
            card = feishu._build_voice_control_card(open_id, chat_id, fid=fid)
            card_id = await feishu._async_send_card_with_id(chat_id, card)
            if card_id:
                feishu._voice_cards[fid] = card_id
                asyncio.create_task(
                    feishu._voice_progress_updater(fid, card_id, open_id, chat_id)
                )
        except Exception:
            log.exception("Discord→飞书 语音控制卡片发送失败")
    feishu_loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_send_card()))


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
        return bool(fut.result(timeout=0.5))
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
        return bool(fut.result(timeout=0.5))
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
        seek_to() 支持无缝跳转: 播放线程不停, 下一帧自动从新位置读。
        """

        FRAME = 3840  # 20ms @ 48kHz * 2ch * 2bytes

        def __init__(self, fid: str, path: str, total: int, start_byte: int = 0):
            self._fid = fid
            self._f = open(path, "rb")
            self._total = total
            self._seek_lock = threading.Lock()
            if start_byte > 0:
                try:
                    self._f.seek(min(start_byte, total))
                except Exception:
                    start_byte = 0
                    self._f.seek(0)
            self._played = start_byte
            _set_progress(fid, played=start_byte, total=total, active=True)

        def seek_to(self, byte_pos: int):
            """无缝跳转: 原子地改文件读取位置, 播放线程无需停止。"""
            byte_pos = max(0, min(byte_pos, self._total))
            byte_pos -= byte_pos % self.FRAME
            with self._seek_lock:
                self._f.seek(byte_pos)
                self._played = byte_pos
            _set_progress(self._fid, played=byte_pos, active=True)

        def read(self) -> bytes:
            with self._seek_lock:
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

    优先无缝 seek: 如果当前 source 已是同 fid 的 _FilePCMSource, 直接改文件读取位置,
    播放线程不停, 零断流。否则 fallback 到 stop→play (如从直播切换到重播)。
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

    # 无缝 seek: 当前 source 是同 fid 的 _FilePCMSource → 直接改读取位置, 不断流
    FileCls = _get_file_source_class()
    cur = getattr(vc, "source", None)
    if cur is not None and isinstance(cur, FileCls) and getattr(cur, "_fid", None) == fid:
        cur.seek_to(start)
        log.info("seamless seek fid=%s delta=%+.0f%% → %.1fs/%.1fs", fid, delta_frac * 100,
                 start / _PCM_BYTES_PER_SEC, total / _PCM_BYTES_PER_SEC)
        return True

    # Fallback: 当前 source 不是 _FilePCMSource (如直播), 需要 stop→play
    if vc.is_playing() or vc.is_paused():
        vc.stop()
        for _ in range(40):
            if not vc.is_playing() and not vc.is_paused():
                break
            await asyncio.sleep(0.05)
    try:
        source = FileCls(fid, path, total, start_byte=start)
        vc.play(source)
        log.info("seek (stop→play) fid=%s delta=%+.0f%% → %.1fs/%.1fs", fid, delta_frac * 100,
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


def _decryption_ledger(dave, uids) -> str:
    """把 davey 的 per-user 解密账本压成一行诊断文本。

    davey 的 ``get_decryption_stats(user_id, media_type=audio)`` 返回
    ``successes / failures / attempts / passthroughs``。**failures 是这里唯一
    真正要看的数** —— py-cord 解密失败时会塞一帧 OPUS_SILENCE 然后若无其事
    继续跑（reader.py:315 那个 except），日志里只有一条 DEBUG。所以「音质变差」
    在上层是完全静默的，只有这个计数器会动。

    返回字符串而不是结构体：它只进日志，不参与判断，别让调用方去解包。
    """
    if dave is None:
        return "-"
    out = []
    for uid in sorted(set(uids)):
        try:
            st = dave.get_decryption_stats(int(uid))
        except Exception as exc:
            # **带上异常正文**。上一版只打类型名，日志里就是一句光秃秃的
            # `<ValueError>` —— 看不出是「这人不在组里」还是「参数传错了」，
            # 而这两件事的下一步动作完全相反。截断是因为它只进日志。
            out.append(f"{uid}:<{type(exc).__name__}: {str(exc)[:60]}>")
            continue
        if st is None:
            out.append(f"{uid}:无记录")
            continue
        out.append(f"{uid}:成功{st.successes}/失败{st.failures}/透传{st.passthroughs}")
    return " ".join(out) or "-"


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

        __sink_listeners__: list = []

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
            # 全零帧计数。**这是区分「网络丢包」和「解密失败」的关键刻度。**
            # py-cord 对真丢包走的是 FEC/PLC 补偿 (opus.py PacketDecoder._decode_packet
            # 里那条 FakePacket 分支), 补出来的是有能量的近似音, 不会是精确的零。
            # 精确零只有两个来源: ① 对端真的发了 opus 静音帧 (说完话时会连发几个);
            # ② reader.py:315 —— DAVE 解密抛异常, 被 except 吞掉后塞 OPUS_SILENCE,
            #    只打一条 DEBUG 日志。②就是「有声但咯楞」的机制, 而且完全静默。
            self._zeros = 0

        def write(self, data, user):
            pcm = getattr(data, "pcm", None)
            if not pcm:
                return
            name = getattr(user, "display_name", None) or getattr(
                user, "name", None) or str(getattr(user, "id", "?"))
            try:
                mono = audioop.tomono(pcm, 2, 0.5, 0.5)  # 48kHz stereo → mono (标准混合)
            except Exception:
                return
            try:
                is_zero = audioop.max(mono, 2) == 0
            except Exception:
                is_zero = False
            with self._lock:
                self._hits += 1
                if is_zero:
                    self._zeros += 1
                self._pcm.extend(mono)
                self._last_name = name
                cap = _MONO_FRAME_BYTES * 100
                if len(self._pcm) > cap:
                    del self._pcm[: len(self._pcm) - cap]
            _stt_ab_record_pcm(mono)
            _utterance_feed(mono)   # 语音回归语料：逐句落盘，与下面喂 bridge 那条互不影响
            _funasr_ab_feed(mono)
            try:
                from .gemini_live_bridge import feed_discord_pcm
                feed_discord_pcm(mono)
            except Exception:
                pass

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

        def zeros(self) -> int:
            """收到的全零帧数。跟 hits 一起看才有意义 —— 看的是**占比**。"""
            with self._lock:
                return self._zeros

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
            _tts_interrupted = True
            # 直接 cancel _do_speak 协程，不等标志位轮询
            if _current_speak_task is not None and not _current_speak_task.done():
                _current_speak_task.cancel()
                log.info("barge-in: cancelled _do_speak task")
            # 清空整个 speak 队列 (hint + 旧 reply 全丢)
            if _speak_queue is not None:
                flushed = 0
                while not _speak_queue.empty():
                    try:
                        _speak_queue.get_nowait()
                        flushed += 1
                    except asyncio.QueueEmpty:
                        break
                if flushed:
                    log.info("barge-in: 清空队列 %d 条 (hint+reply)", flushed)
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


# ─── 语音回归语料：把每一句原始音频单独存下来 ──────────────────────────
#
# 这是从 sink 劈出来的第三条支路，跟喂 Gemini Live 的那条完全独立 —— 目的是拿到
# 「真实链路里那一份字节」：Discord Opus 解码 → 48kHz stereo → tomono 之后的样子。
# 手机另录一份是测不出问题的，编解码这一段就丢了。
#
# **句子边界不用能量 VAD，用包间隔。** Discord 只在有人按住麦时才发 Opus 包，
# 不说话就整个静默不发 —— 所以「超过 _UTT_GAP 秒没有新包」本身就是最干净的
# 断句信号，比在解码后的波形上再算一遍能量既准又便宜。
#
# 旧的 _stt_ab_* 那套是绑在 Chirp3 final transcript 回调上的，那条链路已经下线，
# 所以它只攒不落盘 —— 缓冲区一直在转，一个文件都没写出来过。这里不去动它。
_UTT_GAP = float(os.environ.get("VOICE_CORPUS_GAP", "1.0"))   # 静默多久算一句说完
_UTT_MIN_SEC = float(os.environ.get("VOICE_CORPUS_MIN", "0.35"))  # 短于此判为杂音
_UTT_ENABLED = os.environ.get("VOICE_CORPUS", os.environ.get("STT_AB_DEBUG", "")) == "1"
_utt_buf = bytearray()
_utt_lock = threading.Lock()
_utt_last_ts = 0.0
_utt_seq = 0
_utt_dir = ""
_utt_thread = None


def _utterance_feed(mono_48k: bytes):
    """收音支路：攒当前这句，并确保后台冲刷线程活着。"""
    global _utt_last_ts, _utt_thread
    if not _UTT_ENABLED:
        return
    import time as _t
    with _utt_lock:
        _utt_buf.extend(mono_48k)
        _utt_last_ts = _t.monotonic()
        if _utt_thread is None or not _utt_thread.is_alive():
            _utt_thread = threading.Thread(
                target=_utterance_flush_loop, daemon=True, name="voice-corpus-flush")
            _utt_thread.start()


def _utterance_flush_loop():
    import time as _t
    while True:
        _t.sleep(0.2)
        with _utt_lock:
            idle = _t.monotonic() - _utt_last_ts
            if not _utt_buf or idle < _UTT_GAP:
                continue
            pcm = bytes(_utt_buf)
            _utt_buf.clear()
        _utterance_write(pcm)


def _utterance_write(pcm: bytes):
    global _utt_seq, _utt_dir
    import time as _t, wave, json as _json
    dur = len(pcm) / 96000.0          # 48kHz * 2 bytes
    if dur < _UTT_MIN_SEC:
        return
    if not _utt_dir:
        _utt_dir = os.path.expanduser(
            _t.strftime("~/voice-regression/audio/%Y%m%d-%H%M%S"))
        os.makedirs(_utt_dir, exist_ok=True)
        log.info("[语料] 本轮录音目录: %s", _utt_dir)
    _utt_seq += 1
    path = os.path.join(_utt_dir, f"{_utt_seq:03d}.wav")
    try:
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(pcm)
        opus = _utt_opus_take()
        # 时间戳单独存一份，之后好跟 gemini-live-delivery 日志按时间对齐
        with open(path[:-4] + ".json", "w") as f:
            _json.dump({"seq": _utt_seq, "dur_sec": round(dur, 2),
                        "wall": _t.strftime("%Y-%m-%d %H:%M:%S"),
                        "epoch": _t.time(),
                        "opus": opus}, f, ensure_ascii=False)
        log.info("[语料] 第 %d 句已存: %s (%.1fs) %s", _utt_seq, path, dur, opus)
    except Exception:
        log.exception("[语料] WAV 写入失败 seq=%d", _utt_seq)


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


# ─── FunASR WebSocket 流式 STT (标准全套: VAD + 2pass + Punc + ITN) ──
_funasr_ws = None
_funasr_is_primary = False  # 停用 FunASR，完全切换到 Gemini Live 双向流
_funasr_last_feed = 0.0
_funasr_feeding = False
_funasr_flush_started = False

# Debug: 录制 Discord 收到的原始音频，offline 结果出来后转 OGG 发飞书
_funasr_debug_pcm = bytearray()  # 48kHz mono s16
_funasr_debug = True  # 录音推飞书（不跑 Gemini 对比）

def _funasr_debug_dump(text: str):
    """Debug: PCM→OGG, 同时跑 Gemini STT 对比, 发飞书。"""
    global _funasr_debug_pcm
    import time as _t, subprocess
    pcm = bytes(_funasr_debug_pcm)
    _funasr_debug_pcm.clear()
    dur = len(pcm) / 2 / 48000
    if dur < 0.3:
        return
    ts = _t.strftime("%H%M%S")
    pcm_path = f"/tmp/stt-debug-{ts}.pcm"
    ogg_path = f"/tmp/stt-debug-{ts}.ogg"
    try:
        with open(pcm_path, "wb") as f:
            f.write(pcm)
        subprocess.run([
            "ffmpeg", "-y", "-f", "s16le", "-ar", "48000", "-ac", "1",
            "-i", pcm_path, "-c:a", "libopus", "-b:a", "48k", ogg_path
        ], capture_output=True, timeout=10)
        os.remove(pcm_path)
    except Exception:
        log.exception("[STT-debug] PCM→OGG 转换失败")
        return
    log.info("[STT-debug] %.1fs | FunASR: %s", dur, text[:80])
    # 发飞书
    loop = _sidecar_loop
    feishu_ref = _feishu_ref
    if loop is not None and feishu_ref is not None:
        import asyncio
        feishu = feishu_ref
        async def _send():
            try:
                open_id = _feishu_open_id
                if not open_id:
                    return
                await feishu._send_voice_file(open_id, ogg_path)
            except Exception:
                log.exception("[STT-debug] 发飞书失败")
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_send()))


def _funasr_init():
    """初始化 FunASR WebSocket 连接 (C++ 服务端)。断线自动重连。"""
    global _funasr_ws
    if _funasr_ws is not None:
        return _funasr_ws
    try:
        import websockets.sync.client as ws_sync
        _funasr_ws = ws_sync.connect("ws://localhost:10095", open_timeout=3)
        import json
        from .chirp_phrases import default_phrases
        hotwords_lines = []
        for phrase, boost in default_phrases():
            w = int(boost) if boost else 10
            if len(phrase) <= 20:
                hotwords_lines.append(f"{phrase} {w}")
        hotwords_str = "\n".join(hotwords_lines)
        log.info("[FunASR] 热词: %d 个", len(hotwords_lines))
        _funasr_ws.send(json.dumps({
            "mode": "2pass", "chunk_size": [5, 10, 5],
            "wav_name": "discord", "is_speaking": True,
            "hotwords": hotwords_str, "itn": True
        }))
        log.info("[FunASR] WebSocket 已连接 (2pass 全套: VAD+online+offline+Punc+ITN)")
        import threading
        def _reader():
            global _funasr_ws
            import time as _time
            try:
                while True:
                    msg = _funasr_ws.recv(timeout=300)
                    data = json.loads(msg)
                    text = data.get("text", "").strip()
                    mode = data.get("mode", "")
                    if not text:
                        continue
                    t_now = _time.monotonic()
                    log.info("[FunASR] %s: %s", mode, text[:80])
                    if "offline" in mode and _funasr_is_primary:
                        log.info("[FunASR→LLM] offline → %s", text[:80])
                        # Debug: 把收到的原始音频转 OGG 发飞书
                        if _funasr_debug and len(_funasr_debug_pcm) > 0:
                            _funasr_debug_dump(text)
                        session = _agent_session
                        loop = _sidecar_loop
                        if session is not None and loop is not None:
                            from .livekit_io import _closecrab_llm_instance
                            llm_inst = _closecrab_llm_instance()
                            if llm_inst is not None:
                                llm_inst._skip_next_debounce = True
                            def _do(s=session, t=text):
                                s.generate_reply(user_input=t)
                            loop.call_soon_threadsafe(_do)
                        ch = _sidecar_bot.get_channel(_target_voice_channel_id) if _sidecar_bot else None
                        if loop is not None and ch is not None:
                            import asyncio
                            loop.call_soon_threadsafe(
                                lambda c=ch, t=text: asyncio.ensure_future(c.send(f"🎤 {t[:1900]}"))
                            )
            except Exception as e:
                log.warning("[FunASR] reader 退出: %s (下次 feed 自动重连)", e)
                _funasr_ws = None
        threading.Thread(target=_reader, daemon=True, name="funasr-reader").start()
        return _funasr_ws
    except Exception as e:
        log.warning("[FunASR] 连接失败: %s", e)
        return None


def _funasr_ab_feed(mono_48k: bytes):
    """把 48kHz mono PCM 降采样到 16kHz 流式喂 FunASR。"""
    global _funasr_ws, _funasr_last_feed, _funasr_feeding, _funasr_flush_started
    if not _funasr_is_primary:
        return
    import time as _time
    _funasr_last_feed = _time.monotonic()
    if not _funasr_feeding:
        _funasr_feeding = True
        if not _funasr_flush_started:
            _funasr_flush_started = True
            import threading
            threading.Thread(target=_funasr_flush_thread, daemon=True, name="funasr-flush").start()
    if _funasr_debug:
        _funasr_debug_pcm.extend(mono_48k)
        if len(_funasr_debug_pcm) > 48000 * 2 * 30:  # cap 30s
            del _funasr_debug_pcm[:len(_funasr_debug_pcm) - 48000 * 2 * 30]
    ws = _funasr_init()
    if ws is None:
        return
    try:
        mono_16k, _ = audioop.ratecv(mono_48k, 2, 1, 48000, 16000, None)
        ws.send(mono_16k)
    except Exception:
        _funasr_ws = None


def _funasr_flush_thread():
    """PTT 松手 (RTP 停 800ms) → 发 is_speaking=False 让 FunASR flush 最后一段。
    之后发 is_speaking=True 重新开始接收下一轮。"""
    global _funasr_ws, _funasr_feeding
    import time as _time
    while True:
        _time.sleep(0.1)
        if not _funasr_feeding:
            continue
        last = _funasr_last_feed
        if last > 0 and _time.monotonic() - last > 0.8:
            _funasr_feeding = False
            ws = _funasr_ws
            if ws is not None:
                try:
                    import json as _json
                    ws.send(_json.dumps({"is_speaking": False}))
                    log.info("[FunASR] RTP 停 800ms → is_speaking=false (flush)")
                    _time.sleep(0.5)
                    ws.send(_json.dumps({"is_speaking": True}))
                    log.info("[FunASR] is_speaking=true (重新接收)")
                except Exception:
                    _funasr_ws = None

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
            voice = tts_voice()
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
        return self._sess.get_marshalled_key_package() or b""

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
        if n <= 10 or n % 100 == 0:
            try:
                st = dec.get_stats(self._MT_AUDIO)
                d = bytes(data)
                ok_n = self._dec_ok.get(key, 0)
                fail_rate = n / (ok_n + n) * 100 if (ok_n + n) > 0 else 0
                # DAVE trailer: 最后几字节可能含 epoch/truncated_nonce 信息
                trailer = d[-16:].hex() if len(d) >= 16 else d[-8:].hex()
                log.warning(
                    "[DAVE深诊] decrypt(%s) 失败#%d (失败率%.1f%%) 群建立=%s 有ratchet=%s | "
                    "帧 in=%dB 头=%s trailer=%s | "
                    "stats ok=%d fail=%d miss_key=%d bad_nonce=%d attempts=%d",
                    key, n, fail_rate,
                    self._sess.has_established_group(), key in self._dec_has_ratchet,
                    len(d), d[:8].hex(), trailer,
                    st.decrypt_success_count, st.decrypt_failure_count,
                    st.decrypt_missing_key_count, st.decrypt_invalid_nonce_count,
                    st.decrypt_attempts)
            except Exception:
                log.exception("[DAVE深诊] 取 stats 失败")
        # **抛，不要返回静音帧。**
        #
        # 这个 adapter 冒充的是 davey，而 davey 失败时是**抛异常**的
        # (`DecryptionFailed`)。原来这里返回一帧 Opus 静音，是想「不留空洞」，
        # 结果把 davey 的契约破坏了：调用方 (py-cord 的 `decrypt_rtp` 和我们
        # 自己的 `_probed`) 都是靠捕获异常来判失败的，收到 bytes 就当成功。
        #
        # 后果不是少一行日志 —— 是**两个账本互相矛盾**：探针侧打「失败 0」，
        # dave-py 自己的 stats 打「fail=29」。当时我按探针那栏得出「dave-py
        # 零失败」，差点据此判定官方库的病因，实际两边差着 29 帧。
        #
        # 静音替代照做，只是挪到调用方 —— 那里本来就有这段逻辑，而且是
        # davey 路径和 dave-py 路径共用的一份。
        raise DavePyDecryptFailed(
            f"dave-py decrypt 返回 None (第{n}次, in={len(bytes(data))}B)")

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

    def get_decryption_stats(self, user_id, media_type=None):
        """把 dave-py 的 DecryptorStats 翻成 davey 的 DecryptionStats 形状。

        **这不是为了补全接口，是为了回答一个具体问题。** 换回 dave-py 之后
        `_probed` 侧一次失败都没有了，但那只说明 dave-py 的 decrypt() 返回了非 None
        —— 分不清它是**真解开了**还是**当明文透传了**。这两件事对「怎么修官方
        davey」的结论完全相反：

          - 若是透传 → 那些帧本来就没加密，官方 davey 报
            `UnencryptedWhenPassthroughDisabled` 是因为透传被关了。
            修法 = 把透传打开，一行的事。
          - 若是真解开 → 那些帧是加密的，官方 davey 认不出来，
            是它的帧识别或 ratchet 有问题，得往库里查。

        dave-py 的 `passthrough_count` 正好把这两条分开。
        """
        dec = self._decryptors.get(str(user_id))
        if dec is None:
            raise ValueError("NoDecryptorForUser")
        st = dec.get_stats(media_type or self._MT_AUDIO)

        def _v(name):
            a = getattr(st, name)
            return a() if callable(a) else a

        class _S:
            pass
        s = _S()
        s.successes = _v("decrypt_success_count")
        s.failures = _v("decrypt_failure_count")
        s.passthroughs = _v("passthrough_count")
        s.attempts = _v("decrypt_attempts")
        s.duration = _v("decrypt_duration")
        return s

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


def _retire_ssrc(vc, user_id: int, old: int, new: int) -> None:
    """清掉某用户旧 SSRC 的残留：反向表条目 + 解码器 + speaking 计时器。

    单独拆出来是为了能脱离 py-cord 直接单测 —— 挂钩子那半段没法在单测里跑。

    每一步各自 try：清理是尽力而为，任何一步失败都不该挡住新号写入。宁可留下
    一点垃圾，也不能因为清理抛异常把 speaking 事件整条打断 —— 那会让新号根本
    进不了映射表，症状从「有点卡」升级成「完全听不见」。
    """
    global _ssrc_rotations
    _ssrc_rotations += 1
    try:
        vc._ssrc_to_id.pop(old, None)
    except Exception:
        log.debug("SSRC 轮换: 清反向表失败 old=%s", old, exc_info=True)
    reader = getattr(vc, "_reader", None)
    if reader is not None:
        try:
            reader.packet_router.destroy_decoder(old)
        except Exception:
            log.debug("SSRC 轮换: 销毁解码器失败 old=%s", old, exc_info=True)
        try:
            reader.speaking_timer.drop_ssrc(old)
        except Exception:
            log.debug("SSRC 轮换: 清 speaking 计时器失败 old=%s", old, exc_info=True)
    log.warning("SSRC 轮换: uid=%s %s → %s，已清旧号残留 (累计 %d 次)",
                user_id, old, new, _ssrc_rotations)


def _install_ssrc_rotation_patch():
    """同一个人换 SSRC 时，把旧号连同它的解码器一起清掉 (只挂一次, 进程级)。

    **SSRC 是流的身份证，不是人的。** 它是 32 位随机数, 写在每个 RTP 包头上,
    由语音网关另一条 WebSocket 单独通知 (op 5 speaking 带 user_id + ssrc)。同一
    个人重连一次就换一个号 —— 换号完全不产生任何「旧号作废」的通知。

    py-cord 2.8.1 的记账有两处漏：

    1. ``_add_ssrc`` 覆盖 ``_id_to_ssrc[uid]``, 但 ``_ssrc_to_id[旧号]`` 没人删,
       那条映射会一直挂着。
    2. 更要命的是**旧号的解码器不销毁**。销毁只发生在网关明确推 client_disconnect
       的时候 —— 而换号根本不推那个。于是旧解码器继续活着, 抱着过期的抖动缓冲
       往同一个混音池里吐数据, 和新号的活流交错。听感就是断断续续。

    RFC 3550 本身是有超时概念的 (一个源连续若干个 RTCP 周期没数据就判定离开),
    py-cord 一行都没实现, 纯事件驱动。所以这个洞不会自愈, 只能补。

    2026-09-11 19:21 的现场: ssrc_map 里 Chris 从 10664 变成 10769, 同时实收
    ssrc=[10664, 10769, 129012848, 1332456855, 2936393238], 失败 52 / 成功 308。
    """
    global _ssrc_rotate_patched
    if _ssrc_rotate_patched:
        return
    try:
        # 走 discord.voice —— 顶层 `discord.VoiceClient` 是同一个类的别名, 但 2.7
        # 起访问它会吐 DeprecationWarning, 3.0 直接没了。
        from discord.voice import VoiceClient
    except Exception:
        log.exception("SSRC 轮换补丁: 导入 VoiceClient 失败 (换号残留不会被清)")
        return
    if getattr(VoiceClient, "_cc_ssrc_rotate", False):
        _ssrc_rotate_patched = True
        return
    try:
        _orig_add_ssrc = VoiceClient._add_ssrc

        def _add_ssrc_rotating(self, user_id: int, ssrc: int) -> None:
            old = self._id_to_ssrc.get(user_id)
            # 只在**确实换号**时清理。同一个号被反复通报是常态 (每次开口都推一条
            # speaking), 那种情况下清解码器等于每说一句就把自己的音频掐一次。
            if old is not None and old != ssrc:
                _retire_ssrc(self, user_id, old, ssrc)
            return _orig_add_ssrc(self, user_id, ssrc)

        VoiceClient._add_ssrc = _add_ssrc_rotating
        VoiceClient._cc_ssrc_rotate = True
        _ssrc_rotate_patched = True
        log.info("SSRC 轮换补丁已挂载 (换号即清旧解码器)")
    except Exception:
        log.exception("SSRC 轮换补丁挂载失败 (换号残留不会被清, 症状=断断续续)")


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

            # 回归自管 DAVE 解密 (不切扩展头) + 原生 davey + 正确 MediaType。
            # py-cord 原生 decrypt_rtp 有扩展头 bug (corrupted stream)，
            # 而 packet.extended=False 又破坏 SRTP AEAD。只能自管。
            handled = False
            try:
                state = self.client._connection
                dave = getattr(state, "dave_session", None)
                if dave is not None and getattr(dave, "ready", False):
                    raw_payload = self._decryptor_rtp(packet)
                    # RTP 尾部填充必须在 DAVE 之前切掉 —— 它盖在帧尾的
                    # 「我是加密帧」标记上面。py-cord 解析了 P 位却从不切，
                    # 因为它原来那条路直接喂 Opus，而 Opus 忍得了。
                    padded = bool(getattr(packet, "padding", False))
                    raw_payload = _strip_rtp_padding(padded, raw_payload)
                    uid = state.ssrc_user_map.get(packet.ssrc)
                    if uid:
                        try:
                            import davey as _davey_mod
                            plain = dave.decrypt(uid, _davey_mod.MediaType.audio, raw_payload)
                            _record_dave_result(True, packet.extended, raw_payload,
                                                padded=padded)
                            # 解开之后才是真 Opus，TOC 只有在这里读才作数
                            _record_opus_toc(plain)
                            _utt_opus_note(packet.ssrc, len(plain) if plain else 0)
                        except Exception as exc:
                            # **这一行就是「有声但咯楞」的制造现场。** 解密失败被吞掉,
                            # 换成一帧静音接着跑 —— 上层完全看不出异常, 只听得出卡顿。
                            # 至少要把原因记下来, 否则只剩一个失败计数, 判不了病因。
                            #
                            # 成功那条也要记 —— **失败计数单独看是没有信息的**。
                            # 「165 帧失败」既可能是全体失败也可能是一半失败,
                            # 只有跟成功帧的形状并排放着才判得出差异在哪。
                            _record_dave_result(False, packet.extended, raw_payload, exc,
                                                padded=padded)
                            plain = None
                        packet.decrypted_data = plain if plain else OPUS_SILENCE
                    else:
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

        # 阻止 py-cord _decode_packet 里的 can_passthrough 二次解密：
        # 我们已在 _probed 里完成 DAVE 解密，decrypted_data 是明文 Opus。
        # 如果 can_passthrough 返回 True，py-cord 会对解码后的 PCM 再 dave.decrypt
        # 一次，把好音频搞成乱码。强制 davey.DaveSession.can_passthrough = lambda: False。
        try:
            import davey
            davey.DaveSession.can_passthrough = lambda self, uid: False
            log.info("davey.DaveSession.can_passthrough 已强制 False (防 PCM 二次解密)")
        except Exception:
            log.exception("can_passthrough monkeypatch 失败")
    except Exception:
        log.exception("decrypt_rtp ssrc 探针挂载失败 (ssrc 自动推断将退化)")

    # ── opus decode: FEC 丢包恢复 + 崩溃兜底 (接收路径专用, 不碰 TTS 发送) ──
    # 1) FEC 恢复: 检测 RTP 序列号 gap → 用当前包的 FEC 数据恢复丢失帧
    #    py-cord 2.8.0 的 JitterBuffer 不产生 FakePacket, 内置 FEC 路径是死代码。
    #    我们在 _decode_packet 层面检测 gap 并做 decode(fec=True) + decode(fec=False)。
    #    只在 gap>0 时触发, 不是每包都双解码 (那会破坏 Opus 状态)。
    # 1.5) RTCP 账本: 把服务器自己报的收发数记下来。
    #
    # 挂在**包类的构造函数**上，不挂在 reader 的分发逻辑上 —— 理由是
    # reader 对这两类包的处理就是「打一行 unexpected 然后丢掉」，
    # 挂在那儿等于赌它以后不改分发；挂在构造函数上则只要包被解析过就一定记到。
    try:
        from discord.voice.packets.rtp import (
            SenderReportPacket as _SR, ReceiverReportPacket as _RR)

        def _wrap_report(cls, store_info):
            if getattr(cls, "_cc_rtcp_probed", False):
                return
            _orig_init = cls.__init__

            def _init(self, data):
                _orig_init(self, data)
                try:
                    with _dave_fail_lock:
                        if store_info and getattr(self, "info", None) is not None:
                            _rtcp_sr[self.ssrc] = (self.info.packet_count,
                                                   self.info.octet_count)
                        for r in getattr(self, "reports", ()):
                            _rtcp_rr[r.ssrc] = (r.perc_loss, r.total_lost,
                                                r.last_seq)
                except Exception:
                    pass          # 诊断代码绝不能把收包路径带崩
            cls.__init__ = _init
            cls._cc_rtcp_probed = True

        _wrap_report(_SR, True)
        _wrap_report(_RR, False)
        log.info("RTCP 账本探针已挂载 (SR/RR, 用于拆分上行段与下行段丢包)")
    except Exception:
        log.exception("RTCP 账本探针挂载失败 (丢包仍可测, 只是分不了段)")

    # 2) 崩溃兜底: 单帧 decode 失败 → 回落静音 PCM, 不让 PacketRouter 退出。
    try:
        from discord.opus import PacketDecoder, Decoder
        if not getattr(PacketDecoder, "_cc_decode_guarded", False):
            _orig_decode = PacketDecoder._decode_packet
            try:
                _silence_pcm = b"\x00" * (Decoder.SAMPLES_PER_FRAME * Decoder.SAMPLE_SIZE)
            except Exception:
                _silence_pcm = b"\x00" * 3840
            _decode_fail_n = [0]
            _fec_recover_n = [0]
            _fec_fail_n = [0]

            from discord.voice.utils.wrapped import gap_wrapped as _gap_wrapped

            def _decode_guarded(self, packet):
                try:
                    # 先无条件记丢包，再谈恢复。
                    # 这两件事必须分开：下面那段是 FEC 恢复，它带着 `_FEC_ENABLED`
                    # 和 `gap < 50` 两道门；用它的计数当丢包率，等于「只统计救回来的」，
                    # 丢得越狠反而数字越好看 —— 正好把要找的信号抹掉。
                    if (packet is not None and hasattr(self, '_last_seq')
                            and self._last_seq >= 0):
                        try:
                            _utt_loss_note(_gap_wrapped(self._last_seq, packet.sequence))
                        except Exception:
                            pass
                    # FEC: 检测丢包 (序列号 gap) 并恢复
                    if (_FEC_ENABLED and packet and hasattr(self, '_last_seq')
                            and self._last_seq >= 0
                            and self._decoder is not None
                            and packet.decrypted_data):
                        gap = _gap_wrapped(self._last_seq, packet.sequence)
                        if 0 < gap < 50:
                            try:
                                fec_pcm = self._decoder.decode(
                                    packet.decrypted_data, fec=True)
                                cur_pcm = self._decoder.decode(
                                    packet.decrypted_data, fec=False)
                                _fec_recover_n[0] += 1
                                if _fec_recover_n[0] <= 10 or _fec_recover_n[0] % 200 == 0:
                                    log.info(
                                        "[FEC] 丢包恢复 #%d: gap=%d seq=%d→%d",
                                        _fec_recover_n[0], gap,
                                        self._last_seq, packet.sequence)
                                return packet, fec_pcm + cur_pcm
                            except Exception as e:
                                _fec_fail_n[0] += 1
                                if _fec_fail_n[0] <= 5 or _fec_fail_n[0] % 100 == 0:
                                    log.warning(
                                        "[FEC] 恢复失败 #%d (gap=%d): %s, 回退正常解码",
                                        _fec_fail_n[0], gap, e)
                    return _orig_decode(self, packet)
                except Exception:
                    _decode_fail_n[0] += 1
                    if _decode_fail_n[0] % 500 == 1:
                        log.warning("[opus兜底] decode 失败累计 %d 帧 → 回落静音 (单帧偶发, 不影响整体)",
                                    _decode_fail_n[0])
                    return packet, _silence_pcm

            PacketDecoder._decode_packet = _decode_guarded
            PacketDecoder._cc_decode_guarded = True
            log.info("opus _decode_packet FEC恢复 + 崩溃兜底已挂载")
    except Exception:
        log.exception("opus _decode_packet FEC+兜底挂载失败")


def _live_voice_client():
    """py-cord 眼里当前真正活着的那条语音连接。**这是单一来源。**

    模块全局 ``_listen_vc`` 只是守护循环手里的一份拷贝。每次重连
    (``ch.connect()``) 都会造一个新的 VoiceClient 挂到 ``guild.voice_client``,
    **没有任何代码回写 _listen_vc** —— 于是它成了指向尸体的悬空引用, 而所有
    自愈逻辑都在看它。表现就是「重连之后只能说不能听, 直到进程重启」。
    """
    bot = _sidecar_bot
    if bot is None or not bot.guilds:
        return None
    vc = bot.guilds[0].voice_client
    return vc if vc is not None and vc.is_connected() else None


def _channel_human_ids(vc) -> set:
    """当前语音频道里的真人 uid 集合 (排除自己和所有 bot)。

    voice_states 里会混入其它队友 bot (如 tianmaojingling), py-cord 还可能
    "Skipping member" 根本解析不出它们 —— 解析不出的一律不算真人。
    """
    ids = set()
    ch = getattr(vc, "channel", None)
    if ch is None:
        return ids
    bot_id = None
    try:
        bot_id = vc.guild.me.id
    except Exception:
        pass
    for m in (getattr(ch, "members", None) or []):
        if not getattr(m, "bot", False) and m.id != bot_id:
            ids.add(m.id)
    for uid in (getattr(ch, "voice_states", None) or {}).keys():
        if uid == bot_id:
            continue
        try:
            m = ch.guild.get_member(uid)
        except Exception:
            m = None
        if m is None or getattr(m, "bot", False):
            continue
        ids.add(uid)
    return ids


def _note_speaking(member, state, hits: int, now: float) -> bool:
    """服务端通报「某人开始说话」时，记下时刻和当时的 hits。

    返回是否真的记了 —— 只为让测试能直接断言过滤逻辑，调用方不看返回值。

    过滤掉三类不算数的通报：
      - `member is None`：py-cord 查不到成员时会传 None，无从判断是不是 bot。
      - `member.bot`：**bot 自己发音也会被通报**。不滤掉的话 bunny 每次 TTS
        都会给自己记一笔「有人在说话」，然后因为自己的声音进不来 hits 而误判聋，
        在完全健康的连接上每 5 秒重连一次。
      - `speaking=0`（停止说话）：那是结束通报，不是开始。

    `state` 可能是 `SpeakingState` 也可能是**裸整数** —— Discord 下发的是位域
    (voice|priority = 5)，`try_enum` 认不出组合值时就原样返回 int。另外
    **`bool(SpeakingState.none)` 是 True**（标准 Enum 成员恒真），拿 `if state`
    判会把「停止说话」当成「开始说话」，所以必须取整数值。
    """
    global _speak_start_ts, _speak_start_hits
    if member is None or getattr(member, "bot", False):
        return False
    try:
        flags = int(state)
    except (TypeError, ValueError):
        return False
    if flags == 0:
        return False
    _speak_start_ts = now
    _speak_start_hits = hits
    return True


def _deaf_verdict(now: float, hits: int) -> bool:
    """服务端说有人在说话，宽限期过完 hits 一帧没涨 → 这条接收路是聋的。

    hits 一涨立刻销案（`_speak_start_hits = None`），所以正常说话不会积累出误判；
    没有待判的说话事件时也恒为 False —— 安静的房间不算故障。

    **销案值用 `None` 不用 `-1`** 是故意的：`-1` 会让下面那句比较变成
    `hits > -1`，对任何真实 hits 都成立，于是「没有待判事件」这条护栏即使被
    删掉行为也不变 —— 一条删了没人发现的护栏等于没有。`None` 让它变成
    TypeError，删掉就有测试变红。
    """
    global _speak_start_hits
    if _speak_start_hits is None:
        return False
    if hits > _speak_start_hits:
        _speak_start_hits = None        # 声音进来了，销案
        return False
    if now - _speak_start_ts < _DEAF_GRACE_S:
        return False                     # 还在宽限期内，再等等
    _speak_start_hits = None             # 判过一次就销案，交给冷却限流
    return True


async def _force_dave_rejoin(reason: str) -> bool:
    """强制重建整条 Discord 语音连接: 停录 → 断开 → 重连 → 重新开录。

    **只重建 Discord 这一侧, 不碰 AgentSession。** 两个理由:
    ① Gemini 那条链路 (AgentSession/LLM/TTS) 跟 Discord 的 MLS 树毫无关系,
       没有理由跟着重来; ② ``_start_agent_session`` 根本不是幂等的 —— 每次都
       新造一个 AgentSession 覆盖全局, 旧的直接泄漏。所以这里绕开 _activate_listen,
       照守护循环里「录音被冲垮后重启」那条路自己换 sink 重新 start_recording。
    """
    global _listen_vc, _stt_sink, _listen_restart_n, _listen_ok_since, _dave_rejoin_n
    global _voice_reconnecting
    _dave_rejoin_n += 1
    log.warning("强制重连语音 (第 %d 次): %s", _dave_rejoin_n, reason)
    old = _listen_vc
    # 整个动作期间挂牌, 否则 30 秒一轮的心跳会在这中间看见「掉线」也去重连,
    # 两条握手互相 Terminating。**必须 try/finally**, 中途抛异常留下这个牌子
    # 就等于把心跳这条自愈永久关掉了。
    _voice_reconnecting = True
    try:
        try:
            if old is not None and old.is_recording():
                old.stop_recording()
        except Exception:
            log.exception("重连前停录音失败 (继续断开)")
        try:
            if old is not None:
                await old.disconnect(force=True)
        except Exception:
            log.exception("重连前断开失败 (继续重连)")
        # 给 Discord 一点时间把「bot 离开频道」这件事广播出去 —— 逼出成员变更才是
        # 整个动作的意义所在, 断完立刻回去有概率被当成同一个会话。
        await asyncio.sleep(1.5)
        vc = await _ensure_connected()
        if vc is None:
            # 手里那条已经断了、也没连回来。**必须把 _listen_vc 清掉**: 留着它
            # 守护循环会一路 `not is_connected() → continue` 空转到进程结束,
            # 而心跳只看 guild.voice_client, 不会发现这边聋了。清成 None 之后
            # 守护下一轮会去认领 py-cord 那条活着的连接。
            _listen_vc = None
            log.error("强制重连语音失败: 连不回频道, 已清空句柄等守护下一轮认领")
            return False
        _listen_vc = vc
        _listen_restart_n = 0
        _listen_ok_since = None
        try:
            sink = _get_stt_sink_class()()
            sink.vc = vc
            _stt_sink = sink
            vc.start_recording(sink, _on_recording_done)
        except Exception:
            log.exception("强制重连后重新开录失败 (守护循环下一轮会再试)")
            return False
        log.info("强制重连语音完成, 已重新开录")
        return True
    finally:
        _voice_reconnecting = False


async def _ssrc_infer_loop(period: float = 0.3):
    """后台守护: ① 录音被 corrupted stream 冲垮时自动重启; ② ssrc 自动推断兜底
    (频道唯一真人时, 把传输层实收的未映射 ssrc 直接 _add_ssrc, 不靠 speaking 事件)。
    含诊断日志 (dave ready/epoch + ssrc_map + hits + 实收 ssrc), 便于现场定位。"""
    global _stt_sink, _listen_restart_n, _listen_ok_since, _listen_vc
    global _dave_unready_since, _dave_last_rejoin
    log.info("ssrc 推断 + 录音守护循环已启动")
    diag_n = 0
    _diag_last: tuple = ()
    try:
        while True:
            await asyncio.sleep(period)
            try:
                # ── 手里的句柄是死的, 而 py-cord 有一条活的 → 认领它 ──────────
                # 每次重连都新造一个 VoiceClient 挂到 guild.voice_client, 而
                # _voice_heartbeat 的 rejoin **从不回写 _listen_vc**。于是重连之后
                # 守护还攥着上一条断掉的连接: 它不在录音 → 下面那个分支
                # `not is_connected() → continue` → 每 0.3 秒空转一次, 直到进程结束。
                # 心跳那边只看 guild.voice_client, 看到的是新连接, 一切正常。
                # **两边都没错, 但没人负责把新连接交给守护** —— 这就是「掉线重连过一次
                # 以后只能说不能听」的成因, 2026-09-11 02:50 实测钉在这个状态五分钟。
                # 条件写得很窄 (自己这条确实断了 + 那条确实连着) 是故意的: 握手途中
                # guild.voice_client 会短暂指向半成品, 急着认领会在没建好的连接上开录。
                if (_listen_active and not _voice_reconnecting
                        and (_listen_vc is None or not _listen_vc.is_connected())):
                    live = _live_voice_client()
                    if live is not None and live is not _listen_vc:
                        log.warning("守护手里的语音连接已失效, 认领 py-cord 当前那条并重新开录")
                        _listen_vc = live
                        _listen_restart_n = 0
                        _listen_ok_since = None
                        try:
                            new_sink = _get_stt_sink_class()()
                            new_sink.vc = live
                            _stt_sink = new_sink
                            live.start_recording(new_sink, _on_recording_done)
                            log.info("已在新的语音连接上恢复收音")
                        except Exception:
                            log.exception("认领新连接后开录失败 (下一轮再试)")
                        continue
                # 录音被冲垮后自动重启 (新 sink), 等 ssrc 映射好就能正常收
                if _listen_active and _listen_vc is not None and not _listen_vc.is_recording():
                    _listen_ok_since = None
                    # 掉线重连期间**一次都不要试**。这个守护 0.3 秒一轮，而 voice 握手
                    # 要一秒多 —— 不挡的话它会在那个窗口里连开五六枪，每枪都被
                    # start_recording 以 "not connected to a voice channel" 顶回来，
                    # 白白把崩溃重启的配额烧光。2026-09-08 就是这么哑的：13:00:19
                    # 掉线，1.2 秒内失败 5 次撞满 8 次上限；13:00:51 重连成功后守护
                    # 已被自己的计数器锁死，此后只能说不能听，日志里一句错都没有。
                    if not _listen_vc.is_connected():
                        continue
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
                    elif _listen_restart_n == _LISTEN_RESTART_MAX:
                        # 只喊一次，别刷屏。没这行的话「预算耗尽」是完全静默的。
                        _listen_restart_n += 1
                        log.error("录音重启已达上限 %d 次，放弃自动恢复 —— "
                                  "机器人从现在起只能说不能听，需要 /listen 或重启进程",
                                  _LISTEN_RESTART_MAX)
                    continue
                # 录着且连着 —— 稳定够久就把重启预算还回去 (见 _LISTEN_HEALTHY_RESET_S)。
                if _listen_active and _listen_vc is not None and _listen_restart_n:
                    _now = time.monotonic()
                    if _listen_ok_since is None:
                        _listen_ok_since = _now
                    elif _now - _listen_ok_since >= _LISTEN_HEALTHY_RESET_S:
                        log.info("录音连续正常 %.0f 秒，重启预算复位 (原 %d/%d)",
                                 _now - _listen_ok_since, _listen_restart_n, _LISTEN_RESTART_MAX)
                        _listen_restart_n = 0
                        _listen_ok_since = None
                sink = _stt_sink
                if sink is None:
                    continue
                vc = getattr(sink, "vc", None)
                st = getattr(vc, "_connection", None) if vc else None
                dave = getattr(st, "dave_session", None)
                # ssrc_user_map: ssrc→uid (state.py 属性, 是 _id_to_ssrc 的逆)。
                smap = getattr(st, "ssrc_user_map", None)
                # **不能直接 dict(smap)。** 这个 map 由 py-cord 的语音接收线程
                # 增删，掉线重连那一刻正好在重建它 —— 拷到一半被改就是
                # RuntimeError: dictionary changed size during iteration。
                # 这是 2026-09-11 那次「只能说不能听」最可能的起爆点。
                # 上面那层 except 已经保证它不会再带走整个守护，这里再退一步：
                # 拷失败就当这一轮没读到，下一轮 0.3 秒后自然重来。
                try:
                    cur_map = dict(smap) if smap else {}
                except RuntimeError:
                    cur_map = {}
                with _seen_ssrcs_lock:
                    seen = set(_seen_ssrcs)
                cur_hits = sink.hits()
                cur_zeros = sink.zeros() if hasattr(sink, "zeros") else -1
                # 诊断: 每 10 轮(~3s)打一次, hits / map / 实收 ssrc 一变化立即打
                cur_diag = (cur_hits, tuple(sorted(cur_map.items())), tuple(sorted(seen)))
                changed = cur_diag != _diag_last
                diag_n += 1
                if changed or diag_n % 10 == 0:
                    # 解密账本: davey 自己数成功/失败/passthrough。**这是唯一能把
                    # 「零帧是对端发的静音」和「零帧是解密失败被吞了」分开的证据** ——
                    # 失败的包会被换成一帧静音继续跑, 上层一个异常都看不到。
                    # **按 user_id 逐个取** —— davey 的账本是 per-user 的
                    # (get_decryption_stats(user_id, media_type=audio))，
                    # 不传 uid 会 TypeError。uid 从 ssrc_map 拿，正好只有在场的人。
                    dstats = _decryption_ledger(dave, cur_map.values())
                    log.info(
                        "诊断#%d: ready=%s epoch=%s ssrc_map=%s hits=%s 全零帧=%s 换号=%s "
                        "实收ssrc=%s 解密账=%s 失败原因=%s 包形状=%s Opus模式=%s%s",
                        diag_n, getattr(dave, "ready", None), getattr(dave, "epoch", None),
                        cur_map, cur_hits, cur_zeros, _ssrc_rotations, sorted(seen), dstats,
                        _dave_fail_summary(), _dave_shape_summary(), _opus_toc_summary(),
                        "  <<变化" if changed else "",
                    )
                    _diag_last = cur_diag
                # ── 「连着但聋」自愈: DAVE 掉出 MLS 树后强制重建语音连接 ──
                # dave.ready = has_established_group() and encryptor.has_key_ratchet()。
                # 它为 False 就是**这一路音频既解不开也加不了密**。要命的是出站 TTS
                # 照发不误 (py-cord 不拦), 所以从外面看它活得好好的 —— 跟四小时失聪
                # 那次是同一类静默故障, 只是病灶换了一层。
                #
                # 触发场景 (2026-09-11 实测两次, 相隔 8 分钟, 02:34:03 与 02:42:25):
                # Discord 下发 session_description → py-cord reinit_dave_session() 把
                # MLS 会话整个 reset 并重发 key package → **对端再没回 proposals**。
                # 树是空的, ready 从此钉死 False。Chris 那边表现为「必须退出语音频道
                # 再进来才听得到 bunny」—— 他手动重进能好, 是因为成员变更逼 Discord
                # 重发 proposals 把树建回来, 这也正是这里要模拟的事。
                #
                # 为什么不走「再 reinit 一次重发 key package」那条轻的路: 02:42:25 那次
                # 正是刚 reinit 完、key package 刚发出去就再没下文。重放同一个动作没有
                # 任何理由会有不同结果。**成员变更是目前唯一被证实能把树建回来的事件。**
                #
                # **必须有真人在场才算故障。** 房间里只有 bot 自己时根本没有 MLS
                # 群要建, ready=False 是空闲态的正常值 —— 实测 Chris 不在的那一小时
                # 里 (诊断#620 / #4610 / #8610) 它一直是 False。不加这条判据, bot
                # 会在没人的时候每 60 秒把语音连接掐断重连一次, 比原来的病还糟。
                if _listen_active and vc is not None and dave is not None and not _voice_reconnecting:
                    if (getattr(dave, "ready", False) or not vc.is_connected()
                            or not _channel_human_ids(vc)):
                        _dave_unready_since = None
                    else:
                        _now = time.monotonic()
                        if _dave_unready_since is None:
                            _dave_unready_since = _now
                        elif _now - _dave_unready_since >= _DAVE_UNREADY_GRACE_S:
                            if _now - _dave_last_rejoin < _DAVE_REJOIN_COOLDOWN_S:
                                pass  # 冷却中, 下一轮再看
                            else:
                                _bad_for = _now - _dave_unready_since
                                _dave_last_rejoin = _now
                                _dave_unready_since = None
                                await _force_dave_rejoin(
                                    "DAVE ready 连续 %.0f 秒为 False (MLS 树没建起来, "
                                    "这条连接收不到也发不出加密音频)" % _bad_for)
                                continue

                # ── 第二条判据: 树是好的, 可就是一帧都进不来 ──────────────
                # 上面那条只在 ready=False 时动手。2026-09-11 18:10 那次
                # ready=True epoch=1 却双向不通 (ssrc 对不上, 见 _DEAF_GRACE_S
                # 处的现场记录), 上面那条一次都没触发 —— 它监视的是树, 不是声音。
                #
                # 这条改看结果: **服务端说有人在说话, 宽限期过完 hits 一帧没涨**。
                # speaking 事件走 voice websocket, 跟 UDP 收没收到、解没解得开
                # 完全无关, 所以它能把「真聋了」和「没人说话」分开 —— 后者根本
                # 不会有这个事件, 判据恒为假。
                if (_listen_active and vc is not None and not _voice_reconnecting
                        and vc.is_connected()):
                    if _deaf_verdict(time.monotonic(), cur_hits):
                        _now = time.monotonic()
                        if _now - _dave_last_rejoin >= _DAVE_REJOIN_COOLDOWN_S:
                            _dave_last_rejoin = _now
                            await _force_dave_rejoin(
                                "服务端通报有人在说话, %.0f 秒内 hits 一帧没涨 "
                                "(树是好的但这条接收路是聋的; ssrc_map=%s 实收=%s)"
                                % (_DEAF_GRACE_S, cur_map, sorted(seen)))
                            continue

                # ssrc 自动推断兜底: 频道唯一真人时, 传输层实收但未映射的 ssrc 必是那个真人
                try:
                    if vc is not None and dave is not None and getattr(dave, "ready", False):
                        known = set(cur_map.keys())
                        unknown = seen - known
                        # 解析不出的成员或 .bot=True 一律不算真人 —— 否则会被算进
                        # "未映射真人", 让 len(unmapped_humans)>1, 唯一真人的 ssrc
                        # 永远绑不上 → decrypt_rtp 兜底每帧塞 OPUS_SILENCE → RMS=0
                        # → VAD 永不断句。判据细节见 _channel_human_ids。
                        human_ids = _channel_human_ids(vc)
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
                raise
            except Exception:
                # **这条 except 是整个守护活下去的唯一保障。**
                # 2026-09-11 05:20 HKT 实测：voice WS 报 1006 掉线的同一秒，
                # 这个循环抛了一个没人接的异常，任务当场死掉。它一死，上面
                # 「录音被冲垮 → 自动重启」那条路就再没人走 —— bot 从那一刻起
                # **只能说不能听，持续四小时，日志里一句 ERROR 都没有**。
                # 静默是最坏的部分：出站 TTS 一切正常，看着像活得好好的。
                # 循环体里任何一处抛异常都不该带走整个守护，宁可这一轮白跑。
                log.exception("ssrc/录音守护本轮出错，跳过继续下一轮")
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

    @bot.listen()
    async def on_member_speaking_state_update(member, ssrc, state):
        """「连着但聋」自愈的证据源 —— 服务端说这个人开始发音频了。

        用 `@bot.listen()` 不是 `@bot.event`：后者是**覆盖**，会把同名的其它
        处理器顶掉；listen 是追加，多个可以共存。

        只记时刻和当时的 hits，判定在守护循环里做 —— 这里是 py-cord 的语音
        接收线程回调，越轻越好，而且判定要跟宽限期一起看，本来就不该在事件里做。
        """
        try:
            sink = _stt_sink
            hits = sink.hits() if sink is not None else 0
            _note_speaking(member, state, hits, time.monotonic())
        except Exception:
            log.debug("speaking 事件记账失败 (不影响主链路)", exc_info=True)

    @bot.event
    async def on_application_command_error(ctx, error):
        log.error("slash command 出错: %s", error, exc_info=error)
        try:
            await ctx.respond(f"❌ 命令出错：{error}", ephemeral=True)
        except Exception:
            pass

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


from .tts_config import apply_tts_voice, tts_voice  # noqa: F401  (对外保持原有导入路径)


def _spawn_sidecar_thread(
    bot_name: str, token: str, guild_id: str, voice_channel_id: str,
):
    """拉起 sidecar daemon 线程 (独立 loop 跑 discord.Bot)。返回 thread 或 None。

    开机自启 (maybe_start_discord_voice_sidecar) 和命令强制启动 (start_sidecar)
    共用这段。调用方负责校验 token / enabled。
    """
    apply_tts_voice(bot_name)

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
        # 换号清理: 必须在任何 speaking 事件之前挂上, 否则第一次换号就漏掉。
        _install_ssrc_rotation_patch()
        # 把 DAVE 后端从 davey 换成 dave-py (解密能出真 PCM)。这条线同时碰发送加密,
        # encrypt_opus 已做明文回落兜底; 一键回滚 = _DAVE_PY_BACKEND_ENABLED=False。
        _install_dave_py_backend()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _sidecar_loop = loop
        bot = _build_bot(bot_name, guild_id, voice_channel_id)
        _sidecar_bot = bot
        asyncio.ensure_future(_start_ipc_listener(bot_name), loop=loop)
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
    vch = cfg.get("voice_channel_id", "")
    try:
        from .gemini_live_bridge import get_bridge
        get_bridge()
    except Exception as e:
        log.warning("提前预热 Gemini Live Bridge 失败: %s", e)
    thread = _spawn_sidecar_thread(bot_name, cfg["token"], cfg.get("guild_id", ""), vch)
    if thread is not None:
        # 后台验证：轮询到连上为止；连不上只大声报错，**不改配置**。
        #
        # 原来这里是 `sleep(15)` + 没连上就 `_persist_sidecar_enabled(False)`。
        # 两个问题叠在一起，把偶发故障变成了永久故障：
        #   1. 15s 比 py-cord 自己的语音握手超时 (20s) 还短 —— 第一次握手都没跑完
        #      就判了死刑，而心跳要 30s 后才发起第二次重试。
        #   2. 清掉持久化标记不影响本进程 (心跳照样每 30s 重连)，只影响**下次重启**
        #      —— 那次重启 sidecar 线程根本不会被拉起，日志里一行错都没有。
        # 2026-09-11 jarvis 和天猫精灵就是这么一起消失的：一次握手超时 → 标记被清 →
        # 重启后 Discord 语音静默不存在，只能人工 /discordon。
        #
        # 「这次没连上」和「这个 bot 不该连」是两回事。前者归心跳管，它会一直重试；
        # 后者是配置问题 (缺 token / 频道号错)，在上面就已经显式返回了。
        import threading as _th

        def _verify():
            import time
            deadline = time.monotonic() + _BOOT_VERIFY_TIMEOUT
            while time.monotonic() < deadline:
                if is_voice_connected():
                    return
                time.sleep(_BOOT_VERIFY_POLL)
            log.error(
                "开机自启 sidecar %ds 仍未连上语音频道 (bot=%s)；"
                "心跳会继续每 30s 重试，持久化标记保持不变",
                _BOOT_VERIFY_TIMEOUT, bot_name,
            )

        _th.Thread(target=_verify, daemon=True, name="sidecar-boot-verify").start()
    return thread

def start_sidecar(bot_name: str) -> tuple[bool, str]:
    """【飞书线程调用】运行时连进 Discord 语音频道 + 持久化 voice_sidecar=true。"""
    if is_sidecar_running():
        if is_voice_connected():
            _persist_sidecar_enabled(bot_name, True)
            return True, "Discord 已经连着 General 了。"
        # sidecar 线程活着但语音没连上 → 杀掉重来
        log.warning("sidecar 线程在跑但语音未连接，强制重启 sidecar")
        stop_sidecar(bot_name)
    cfg = _load_sidecar_config(bot_name)
    if not cfg or not cfg["token"]:
        return False, "这个 bot 没配 Discord token，连不了。"
    vch = cfg.get("voice_channel_id", "")
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
