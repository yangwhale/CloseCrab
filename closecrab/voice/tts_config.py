"""TTS 音色配置 — 故意做成零重依赖的小模块。

音色是 **bot 级** 设置，流式 TTS 和语音消息(ogg)两条路都用它。之前它住在
discord_voice_sidecar 里，而那个模块顶层 `import audioop` —— audioop 在
Python 3.13 被 PEP 594 移除，Ubuntu 26.04 上的 3.14 直接 ImportError。
结果就是：只要机器新一点，读个音色都会把整条语音消息路径带崩。

所以这里只依赖 google.cloud.firestore，谁都能安全 import。
"""

import logging

log = logging.getLogger("closecrab.tts_config")

_tts_voice: str = ""
_tts_voice_bot: str = ""


def apply_tts_voice(bot_name: str) -> str:
    """从 Firestore bots/{name}.channels.discord.tts_voice 读音色到进程内。

    在 main.py 启动时无条件调用一次 —— 不能挂在 Discord sidecar 的启动流程里，
    否则 /discordoff 之后重启就静默失效（2026-08-09 小爱因此用上了别人的声音）。
    """
    global _tts_voice, _tts_voice_bot
    _tts_voice_bot = bot_name
    try:
        from google.cloud import firestore
        from ..constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
        db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
        doc = db.collection("bots").document(bot_name).get()
        cfg = ((doc.to_dict() or {}).get("channels") or {}).get("discord") or {}
        _tts_voice = str(cfg.get("tts_voice") or "")
    except Exception as e:
        log.warning("读取 TTS 音色失败: %s", e)
        _tts_voice = ""
    if _tts_voice:
        log.info("TTS 音色: %s (bot=%s)", _tts_voice, bot_name)
    else:
        log.error("bots/%s.channels.discord.tts_voice 没配，任何 TTS 调用都会直接报错",
                  bot_name)
    return _tts_voice


def tts_voice() -> str:
    """当前音色。没配就抛 —— 宁可响亮失败，也不要悄悄用一个谁也说不清来源的值。"""
    if not _tts_voice:
        raise RuntimeError(
            f"TTS 音色未配置: 请设 bots/{_tts_voice_bot or '<bot>'}"
            ".channels.discord.tts_voice"
        )
    return _tts_voice
