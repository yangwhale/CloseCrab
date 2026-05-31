"""Discord 语音小尾巴 (voice-only sidecar)。

用途：当 bot 的 active_channel 是飞书时，仍想借用 Discord 的语音输出能力。
这个 sidecar 维持 Discord gateway 连接、**自动常驻**一个固定语音频道，并把
飞书对话的口语回复 (voice-summary) 镜像念到该频道——用户进频道就能听。

它**故意不注册** ``on_message`` / 任何消息处理 handler，所以「接收消息那条
路」天然堵死。它不依赖 BotCore，完全自包含，跑在独立后台 daemon 线程里。

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
import logging
import os
import threading

log = logging.getLogger("closecrab.discord_voice_sidecar")

# 模块级状态：给飞书线程跨线程调用 speak_text() 用。sidecar 未启动时全为 None/0，
# speak_text() 据此静默跳过。
_sidecar_loop: "asyncio.AbstractEventLoop | None" = None
_sidecar_bot = None
_target_voice_channel_id: int = 0


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
