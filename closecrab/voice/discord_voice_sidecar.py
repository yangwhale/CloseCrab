"""Discord 语音小尾巴 (voice-only sidecar)。

用途：当 bot 的 active_channel 是飞书时，仍想借用 Discord 的语音输出能力。
这个 sidecar 维持 Discord gateway 连接、**自动常驻**一个固定语音频道，并把
飞书对话的口语回复 (voice-summary) 镜像念到该频道——用户进频道就能听。

它**故意不注册** ``on_message`` / 任何消息处理 handler，所以「接收消息那条
路」天然堵死。它不依赖 BotCore，完全自包含，跑在独立后台 daemon 线程里。

⚠️ 只做 TTS「播报 (发送)」，不做语音「接收 (STT)」。
   Discord 已对所有非 Stage 通话强制 DAVE 端到端加密 (E2EE)，bot 端接收语音
   在整个生态 (py-cord / discord.js / davey) 实测全部拿不到音频包 —— 上游死锁，
   2026 年无 workaround (发送不受影响)。完整 STT 接收实现已封存到同目录
   ``discord_stt_receive.py.disabled``，等上游修好 DAVE 接收后再复活。
   详见 memory: discord-dave-voice-receive-blocked。

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
import logging
import os
import re
import threading
import time

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
# _stream_speak 串行锁: opener/hint/最终答 多条并发时, 保证 wait+play 临界区 FIFO,
# 不会都通过 vc.is_playing() 等待后同时 vc.play 互相冲掉。懒创建 (绑 sidecar loop)。
_speak_lock: "asyncio.Lock | None" = None

# 没配 voice_channel_id 的 bot 默认连这个频道 (Discord General)。多 bot 共用。
_DEFAULT_VOICE_CHANNEL_ID = "1471064068851761165"


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
    return vc if vc.is_connected() else None


async def _voice_heartbeat(interval: float = 30.0):
    """后台心跳：周期性检查 voice 连接，掉线则自动爬回常驻频道。

    根因：Discord gateway 与 voice 是两条独立连接。半夜 websocket 1006 断线后
    gateway 会 RESUME，但 voice 连接不会自动重建 → 飞书 voice-summary 检测到
    is_voice_connected=False 就回退飞书 ogg，Discord 静音。这个心跳就是兜底。
    """
    while True:
        try:
            await asyncio.sleep(interval)
            if not _target_voice_channel_id:
                continue
            bot = _sidecar_bot
            if bot is None or not bot.guilds:
                continue
            vc = bot.guilds[0].voice_client
            if vc is not None and vc.is_connected():
                continue  # 健康，跳过
            log.warning("检测到 voice 掉线，尝试自动 rejoin 常驻频道…")
            vc = await _ensure_connected()
            if vc is not None:
                log.info("voice 自动 rejoin 成功")
            else:
                log.warning("voice 自动 rejoin 失败，下个周期再试")
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
_MAX_BATCH_CHARS = 200    # 之后批上限: 此时已有大段 buffer, 放大减接缝; 远低于 ~500c 吞尾阈值


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


async def _gemini_tts_stream(text: str):
    """分批流式调 Gemini TTS, 逐 chunk yield 24kHz mono s16 PCM bytes。

    复用 gemini_tts 的 client 构造 + 文本清洗 + config, 保证与飞书 ogg 同
    model/voice (orus→"Orus")。整段先切句打包成多批, 逐批 generate_content_stream,
    PCM 连续 yield (消费方 _gen_worker 无感知分批, ratecv state 跨批连续)。
    """
    from google.genai import types as gt
    from .gemini_tts import _build_genai_client, _clean_text_for_tts

    cleaned = _clean_text_for_tts(text)
    if not cleaned.strip():
        return
    client = _build_genai_client(None)
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
        last_finish = None
        got = 0
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
        log.info("TTS 批 #%d/%d: %dc → %.1fs 音频 finish=%s",
                 idx, len(batches), len(batch), got / 2 / 24000, last_finish)


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

        def __init__(self, fid: str = ""):
            self._buf = bytearray()
            self._lock = threading.Lock()
            self._finished = False
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

        def finish(self):
            with self._lock:
                self._finished = True
                # 生成完, 总长已确定; 让进度条拿到分母 (直播首播此前 total=0 未知)
                if self._fid:
                    _set_progress(self._fid, total=self._written)

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
                if self._finished:
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
                return b"\x00" * self.FRAME  # 欠载未结束: 静音帧保持流活着

        def is_opus(self) -> bool:
            return False

    _SOURCE_CLASS = _StreamPCMSource
    return _SOURCE_CLASS


async def _stream_speak(text: str, fid: str = ""):
    """sidecar loop 内: 用现有常驻连接流式直生 → resample → 推 Discord。

    不主动建连 (没连由调用方 is_voice_connected 拦掉)。排队等上一句念完避免叠音。
    fid 非空时, 把生成的 48k/stereo/s16 PCM 同步落盘到 _buf_path(fid), 供重播。
    """
    bot = _sidecar_bot
    if bot is None or not bot.guilds:
        return
    vc = bot.guilds[0].voice_client
    if vc is None or not vc.is_connected():
        return

    global _speak_lock
    if _speak_lock is None:
        _speak_lock = asyncio.Lock()
    # 临界区: 等上一句念完 + 起播。整段持锁 (vc.play 立即返回, 实际播放在后台,
    # 不占锁), 让 opener→hint→hint→最终答 严格 FIFO, 不叠音不互相冲掉。
    async with _speak_lock:
        for _ in range(1200):  # 最多 ~60s 等上一句念完
            if not vc.is_playing():
                break
            await asyncio.sleep(0.05)

        source = _get_source_class()(fid)
        # 预充阈值: 攒够 ~0.8s 的 48k/stereo/s16 再开播, 抗 jitter。
        PREBUFFER = int(48000 * 2 * 2 * 0.8)

        # Gemini 生成挪到独立线程, 自带干净 event loop, 全速迭代。
        # 原因: 之前在 sidecar loop 里跑, 那个 loop 还扛着 Discord 心跳 + voice
        # keepalive + LiveKit Voice IO, 太忙 → chunk 消费被挤占变慢 → Vertex gRPC
        # 流被服务端按 idle 提前关 → async for 正常结束但只拿到一半音频 (截断元凶)。
        def _gen_worker():
            buf_f = None
            bpath = _buf_path(fid)
            if bpath:
                try:
                    os.makedirs(_BUF_DIR, exist_ok=True)
                    buf_f = open(bpath, "wb")
                except Exception:
                    log.exception("打开 buffer 落盘文件失败: %s", bpath)
                    buf_f = None

            async def _run():
                state = None  # ratecv 跨块状态, 跨 chunk 传递保证边界无爆音
                async for pcm24 in _gemini_tts_stream(text):
                    pcm48, state = audioop.ratecv(pcm24, 2, 1, 24000, 48000, state)
                    stereo = audioop.tostereo(pcm48, 2, 1, 1)  # mono → stereo
                    source.write(stereo)
                    if buf_f is not None:
                        buf_f.write(stereo)  # tee: 直播的同时落盘整段, 供重播
                log.info("Discord 流式念(生成完): %s", text[:40])
            try:
                asyncio.run(_run())
            except Exception:
                log.exception("流式 TTS 生成失败")
            finally:
                source.finish()  # 让 read() 放完残余后返回 b'' 结束播放
                if buf_f is not None:
                    try:
                        buf_f.close()
                    except Exception:
                        pass

        gen_thread = threading.Thread(
            target=_gen_worker, daemon=True, name="discord-tts-gen")
        gen_thread.start()

        # sidecar loop 只做轻量轮询: 等预充够 (或生成已结束) 再开播。
        for _ in range(2400):  # 最多 ~120s
            if source.buffered() >= PREBUFFER or not gen_thread.is_alive():
                break
            await asyncio.sleep(0.05)
        if source.buffered() > 0 or gen_thread.is_alive():
            try:
                vc.play(source)
            except Exception:
                log.exception("Discord vc.play(stream source) 失败")
                source.finish()
        else:
            source.finish()  # 啥都没生成出来


def stream_speak_text(text: str, fid: str = "") -> bool:
    """【飞书线程调用】流式直生 TTS 推 Discord 常驻语音频道念。线程安全。

    未连语音频道 / sidecar 未启动 → 静默返回 False (不主动建连, 不费劲)。
    fire-and-forget: 立即返回, 不阻塞飞书后续 (飞书 ogg) 的生成。
    fid 非空时把整段音频落盘到 _buf_path(fid), 供后续 replay_file(fid) 重播。
    """
    if not text or not text.strip():
        return False
    loop = _sidecar_loop
    if loop is None or _sidecar_bot is None:
        return False
    if not is_voice_connected():
        return False
    try:
        asyncio.run_coroutine_threadsafe(_stream_speak(text, fid), loop)
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


# ─── 语音「接收」(STT) 已封存 ────────────────────────────────────────────────
# Discord 强制 DAVE E2EE 后, bot 端接收语音在整个生态实测拿不到音频包 (上游死锁,
# 2026 无 workaround)。完整 V2 接收实现 (连续 PCM 流 + silero VAD + AgentSession
# STT-only + ssrc 自动推断 + 诊断探针 + /listen /stoplisten 命令) 已封存到同目录
# discord_stt_receive.py.disabled, 等上游修好 DAVE 接收后复活。本文件只保留发送
# (TTS 播报)。详见 memory: discord-dave-voice-receive-blocked。


def _build_bot(bot_name: str, guild_id: str = "", voice_channel_id: str = ""):
    """构造只含 /leave 的最小 discord.Bot (不挂任何消息 handler)。

    on_ready 后自动常驻 voice_channel_id 指定的语音频道。
    """
    import discord

    intents = discord.Intents.default()  # 含 voice_states；不开 message_content
    debug_guilds = None
    if guild_id:
        try:
            debug_guilds = [int(guild_id)]
        except (ValueError, TypeError):
            debug_guilds = None
    bot = discord.Bot(intents=intents, debug_guilds=debug_guilds)

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
        # 启动后台 voice 健康检查（防重复：on_ready 在 RESUME 后可能再次触发）
        global _heartbeat_task
        if _heartbeat_task is None or _heartbeat_task.done():
            _heartbeat_task = asyncio.create_task(_voice_heartbeat())
            log.info("voice 心跳已启动（30s 周期，断线自动 rejoin）")

    @bot.event
    async def on_application_command_error(ctx, error):
        log.error("slash command 出错: %s", error, exc_info=error)
        try:
            await ctx.respond(f"❌ 命令出错：{error}", ephemeral=True)
        except Exception:
            pass

    @bot.slash_command(description="让机器人离开语音频道")
    async def leave(ctx):
        vc = ctx.guild.voice_client
        if vc and vc.is_connected():
            await vc.disconnect(force=True)
            await ctx.respond("👋 已离开语音频道。")
        else:
            await ctx.respond("我不在任何语音频道里。")

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
