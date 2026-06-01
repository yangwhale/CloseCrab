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
import threading

log = logging.getLogger("closecrab.discord_voice_sidecar")

# 模块级状态：给飞书线程跨线程调用 speak_text() 用。sidecar 未启动时全为 None/0，
# speak_text() 据此静默跳过。
_sidecar_loop: "asyncio.AbstractEventLoop | None" = None
_sidecar_bot = None
_target_voice_channel_id: int = 0
_heartbeat_task = None  # 后台 voice 健康检查 task，防重复启动


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


async def _gemini_tts_stream(text: str):
    """流式调 Gemini TTS, 逐 chunk yield 24kHz mono s16 PCM bytes。

    复用 gemini_tts 的 client 构造 + 文本清洗 + config, 保证与飞书 ogg 同
    model/voice (orus→"Orus")。
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
    stream = await client.aio.models.generate_content_stream(
        model=model, contents=cleaned, config=config
    )
    async for chunk in stream:
        for cand in getattr(chunk, "candidates", None) or []:
            content = getattr(cand, "content", None)
            for part in getattr(content, "parts", None) or []:
                inline = getattr(part, "inline_data", None)
                if inline and inline.data:
                    yield bytes(inline.data)


_SOURCE_CLASS = None


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

        def __init__(self):
            self._buf = bytearray()
            self._lock = threading.Lock()
            self._finished = False
            self._written = 0   # 累计写入字节 (诊断)
            self._real = 0      # 派发真音频帧数 (诊断)
            self._silence = 0   # 派发欠载静音帧数 (诊断)

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

        def read(self) -> bytes:
            with self._lock:
                if len(self._buf) >= self.FRAME:
                    out = bytes(self._buf[: self.FRAME])
                    del self._buf[: self.FRAME]
                    self._real += 1
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
                    return b""
                self._silence += 1
                return b"\x00" * self.FRAME  # 欠载未结束: 静音帧保持流活着

        def is_opus(self) -> bool:
            return False

    _SOURCE_CLASS = _StreamPCMSource
    return _SOURCE_CLASS


async def _stream_speak(text: str):
    """sidecar loop 内: 用现有常驻连接流式直生 → resample → 推 Discord。

    不主动建连 (没连由调用方 is_voice_connected 拦掉)。排队等上一句念完避免叠音。
    """
    bot = _sidecar_bot
    if bot is None or not bot.guilds:
        return
    vc = bot.guilds[0].voice_client
    if vc is None or not vc.is_connected():
        return
    for _ in range(1200):  # 最多 ~60s 等上一句念完
        if not vc.is_playing():
            break
        await asyncio.sleep(0.05)

    source = _get_source_class()()
    # 预充阈值: 攒够 ~0.8s 的 48k/stereo/s16 再开播, 抗 jitter。
    PREBUFFER = int(48000 * 2 * 2 * 0.8)

    # Gemini 生成挪到独立线程, 自带干净 event loop, 全速迭代。
    # 原因: 之前在 sidecar loop 里跑, 那个 loop 还扛着 Discord 心跳 + voice
    # keepalive + LiveKit Voice IO, 太忙 → chunk 消费被挤占变慢 → Vertex gRPC
    # 流被服务端按 idle 提前关 → async for 正常结束但只拿到一半音频 (截断元凶)。
    def _gen_worker():
        async def _run():
            state = None  # ratecv 跨块状态, 跨 chunk 传递保证边界无爆音
            async for pcm24 in _gemini_tts_stream(text):
                pcm48, state = audioop.ratecv(pcm24, 2, 1, 24000, 48000, state)
                source.write(audioop.tostereo(pcm48, 2, 1, 1))  # mono → stereo
            log.info("Discord 流式念(生成完): %s", text[:40])
        try:
            asyncio.run(_run())
        except Exception:
            log.exception("流式 TTS 生成失败")
        finally:
            source.finish()  # 让 read() 放完残余后返回 b'' 结束播放

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


def stream_speak_text(text: str) -> bool:
    """【飞书线程调用】流式直生 TTS 推 Discord 常驻语音频道念。线程安全。

    未连语音频道 / sidecar 未启动 → 静默返回 False (不主动建连, 不费劲)。
    fire-and-forget: 立即返回, 不阻塞飞书后续 (飞书 ogg) 的生成。
    """
    if not text or not text.strip():
        return False
    loop = _sidecar_loop
    if loop is None or _sidecar_bot is None:
        return False
    if not is_voice_connected():
        return False
    try:
        asyncio.run_coroutine_threadsafe(_stream_speak(text), loop)
        return True
    except Exception:
        log.exception("stream_speak_text 跨线程调度失败")
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


def maybe_start_discord_voice_sidecar(bot_name: str) -> threading.Thread | None:
    """如配置启用，在后台 daemon 线程启动 Discord 语音 sidecar。

    返回线程对象 (已 start)，未启用 / 不可用时返回 None。
    """
    cfg = _load_sidecar_config(bot_name)
    if not cfg or not cfg["enabled"]:
        return None
    token = cfg["token"]
    if not token:
        log.warning("Discord 语音 sidecar 已开启但缺 token，跳过")
        return None

    try:
        import discord  # noqa: F401
    except ImportError:
        log.warning("未安装 py-cord，Discord 语音 sidecar 跳过")
        return None

    def _run():
        global _sidecar_loop, _sidecar_bot
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
        bot = _build_bot(bot_name, cfg.get("guild_id", ""), cfg.get("voice_channel_id", ""))
        _sidecar_bot = bot
        try:
            # 用 start() 而非 run()——run() 装 signal handler 只能在主线程
            loop.run_until_complete(bot.start(token))
        except Exception as e:
            log.error("Discord 语音 sidecar 崩溃: %s", e, exc_info=True)
        finally:
            _sidecar_bot = None
            _sidecar_loop = None
            try:
                loop.run_until_complete(bot.close())
            except Exception:
                pass
            loop.close()

    thread = threading.Thread(target=_run, daemon=True, name="discord-voice-sidecar")
    thread.start()
    log.info("Discord 语音 sidecar 线程已启动 (active channel 之外的旁路)")
    return thread
