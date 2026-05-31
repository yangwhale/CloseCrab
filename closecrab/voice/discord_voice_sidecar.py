"""Discord 语音小尾巴 (voice-only sidecar)。

用途：当 bot 的 active_channel 是飞书时，仍想借用 Discord 的语音输出能力做
测试。这个 sidecar 只做两件事——维持 Discord gateway 连接 (让 bot 在线、能进
语音频道)，并注册 ``/say`` / ``/leave`` 两个 slash command 用 TTS 念话。

它**故意不注册** ``on_message`` / 任何消息处理 handler，所以「接收消息那条路」
天然堵死——这正是 Chris 要的「只用语音输出做测试」。它也不依赖 BotCore，
完全自包含，跑在独立后台 daemon 线程里，不影响主线程的飞书 channel。

启用方式 (Firestore ``bots/{name}``)::

    channels:
      discord:
        token: "<bot token>"
        voice_sidecar: true        # 开关，缺省 false 不启动

设计要点：
- 用 ``bot.start(token)`` 而非 ``bot.run()``——后者会装 signal handler，
  只能在主线程跑；sidecar 在子线程，必须用 start()。
- intents 用 ``Intents.default()`` (含 voice_states)，不要 message_content，
  本来也不收消息。
- 进程退出时 daemon 线程随主进程一起走，无需额外清理。
"""

import asyncio
import logging
import os
import threading

log = logging.getLogger("closecrab.discord_voice_sidecar")


def _load_sidecar_config(bot_name: str) -> dict | None:
    """直接从 Firestore 读 Discord 子配置 (active channel 是飞书时不会被扁平化)。

    返回 ``{"token": ..., "enabled": bool}``，读失败返回 None。
    """
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
            "guild_id": data.get("guild_id", ""),
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
        proc = await asyncio.create_subprocess_exec(
            "python3", tts_script, text,
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


def _build_bot(bot_name: str, guild_id: str = ""):
    """构造只含 /say /leave 的最小 discord.Bot (不挂任何消息 handler)。

    传入 guild_id 时用 debug_guilds 注册 guild-scoped 命令——客户端里**秒出**，
    不传则全局注册 (最多 1 小时才传播)。
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
        log.info(
            "Discord 语音 sidecar 上线: %s (guilds=%d)",
            bot.user, len(bot.guilds),
        )

    @bot.event
    async def on_application_command_error(ctx, error):
        # 把 slash command 里抛出的异常完整打到 closecrab 日志, 否则只进 py-cord
        # 自己的 stderr, 排查时看不到。
        log.error("slash command 出错: %s", error, exc_info=error)
        try:
            await ctx.respond(f"❌ 命令出错：{error}", ephemeral=True)
        except Exception:
            pass

    @bot.slash_command(description="进语音频道用 TTS 念一段话")
    async def say(ctx, text: str):
        log.info(
            "say 被调用: user=%s text=%r voice=%s",
            getattr(ctx.author, "id", "?"),
            text[:40],
            bool(getattr(getattr(ctx.author, "voice", None), "channel", None)),
        )
        # 先 defer 抢下 3 秒窗口 (TTS 生成慢), 之后用 followup
        try:
            await ctx.defer()
        except Exception as e:
            log.exception("ctx.defer() 失败")
            return
        voice_state = getattr(ctx.author, "voice", None)
        if not voice_state or not voice_state.channel:
            await ctx.respond("⚠️ 你得先进一个语音频道，我才知道去哪念。", ephemeral=True)
            return
        channel = voice_state.channel

        ogg_path, err = await _generate_tts(text)
        if err:
            await ctx.respond(f"❌ TTS 失败：{err}")
            return

        # 连接语音频道。py-cord 有个僵尸态: 上次 connect 握手没完成时,
        # VoiceClient 仍挂在 guild 上 (is_connected()=False), 再 connect 会报
        # "Already connected"。所以先无条件清掉残留, 再干净重连。
        try:
            existing = ctx.guild.voice_client
            if existing is not None:
                if existing.is_connected() and existing.channel and existing.channel.id == channel.id:
                    vc = existing  # 已经在目标频道, 复用
                else:
                    try:
                        await existing.disconnect(force=True)
                    except Exception:
                        pass
                    vc = await channel.connect(timeout=20.0, reconnect=False)
            else:
                vc = await channel.connect(timeout=20.0, reconnect=False)
        except discord.ClientException:
            # "Already connected" 兜底: 强拆 guild 上的残留再连一次
            existing = ctx.guild.voice_client
            if existing is not None:
                try:
                    await existing.disconnect(force=True)
                except Exception:
                    pass
            await asyncio.sleep(0.5)
            try:
                vc = await channel.connect(timeout=20.0, reconnect=False)
            except Exception as e:
                log.exception("进语音频道失败(重试后)")
                await ctx.respond(f"❌ 进语音频道失败：{e}")
                return
        except Exception as e:
            log.exception("进语音频道失败")
            await ctx.respond(f"❌ 进语音频道失败：{e}")
            return

        # 等握手 (UDP + 加密) 真正完成, 否则 play() 报 "Not connected to voice"
        for _ in range(50):  # 最多 ~10s
            if vc.is_connected():
                break
            await asyncio.sleep(0.2)
        if not vc.is_connected():
            log.warning("语音握手超时, vc 未连上")
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            await ctx.respond("❌ 语音握手超时(连上了但 UDP 没通), 可能是网络/防火墙挡了语音 UDP。")
            return

        try:
            while vc.is_playing():
                await asyncio.sleep(0.2)
            source = discord.FFmpegOpusAudio(ogg_path)
            vc.play(source)
        except Exception as e:
            log.exception("播放失败")
            await ctx.respond(f"❌ 播放失败：{e}")
            return
        await ctx.respond(f"🔊 在 **{channel.name}** 念：{text[:80]}")

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

    返回线程对象 (已 start)，未启用 / 不可用时返回 None。幂等：不开启时静默跳过。
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
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot = _build_bot(bot_name, cfg.get("guild_id", ""))
        try:
            # 用 start() 而非 run()——run() 装 signal handler 只能在主线程
            loop.run_until_complete(bot.start(token))
        except Exception as e:
            log.error("Discord 语音 sidecar 崩溃: %s", e, exc_info=True)
        finally:
            try:
                loop.run_until_complete(bot.close())
            except Exception:
                pass
            loop.close()

    thread = threading.Thread(target=_run, daemon=True, name="discord-voice-sidecar")
    thread.start()
    log.info("Discord 语音 sidecar 线程已启动 (active channel 之外的旁路)")
    return thread
