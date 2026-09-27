"""离线走和现场语音完全同一条路（_gemini_tts_stream：同模型、同声音、同分批）生成讲课原声。
用法：offline_tts.py in.txt out.pcm"""
import asyncio, sys, os, time
sys.path.insert(0, os.path.expanduser('~/CloseCrab'))
import logging; logging.basicConfig(level=logging.WARNING)
from closecrab.voice import discord_voice_sidecar as S
from closecrab.voice.tts_config import apply_tts_voice
async def main(src, dst):
    print("voice:", apply_tts_voice(os.environ.get("BOT_NAME", "jarvis")))
    text = open(src, encoding='utf-8').read()
    t0 = time.time(); n = 0
    with open(dst, 'wb') as f:
        async for chunk in S._gemini_tts_stream(text):
            f.write(chunk); n += len(chunk)
    print(f"{dst}: {n/4/48000:.1f}s audio in {time.time()-t0:.0f}s", flush=True)
asyncio.run(main(sys.argv[1], sys.argv[2]))
