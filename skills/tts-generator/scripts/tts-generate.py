#!/usr/bin/env python3
"""Generate speech audio using Gemini 3.1 Flash TTS (primary) or Edge TTS (fallback).

Usage:
    tts-generate.py "你好世界"
    tts-generate.py "[excitedly] 太好了！" --voice aoede
    tts-generate.py "你好" --engine edge --voice xiaoxiao

Output: prints the path to the generated .ogg file on stdout.
"""

import argparse
import asyncio
import base64
import os
import re
import subprocess
import sys
import tempfile
import wave

# ── Gemini voices ──

GEMINI_VOICES = {
    "aoede": "Aoede",          # Breezy (default)
    "achird": "Achird",        # Friendly
    "sulafat": "Sulafat",      # Warm
    "kore": "Kore",            # Firm
    "puck": "Puck",            # Upbeat
    "charon": "Charon",        # Informative
    "fenrir": "Fenrir",        # Excitable
    "leda": "Leda",            # Youthful
    "orus": "Orus",            # Firm
    "zephyr": "Zephyr",        # Bright
    "achernar": "Achernar",    # Soft
    "gacrux": "Gacrux",        # Mature
    "sadachbia": "Sadachbia",  # Lively
    "algieba": "Algieba",      # Smooth
    "vindemiatrix": "Vindemiatrix",  # Gentle
}

DEFAULT_GEMINI_VOICE = "aoede"

# ── Edge TTS voices (fallback) ──

EDGE_VOICES = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "xiaoyi": "zh-CN-XiaoyiNeural",
    "yunxi": "zh-CN-YunxiNeural",
    "yunjian": "zh-CN-YunjianNeural",
    "yunyang": "zh-CN-YunyangNeural",
    "yunxia": "zh-CN-YunxiaNeural",
}

DEFAULT_EDGE_VOICE = "xiaoxiao"


def _wav_write(path: str, pcm: bytes, rate: int = 24000):
    """Write raw PCM data to a WAV file."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)


def _wav_to_ogg(wav_path: str) -> str:
    """Convert WAV to OGG Opus. Returns ogg path."""
    ogg_path = wav_path.replace(".wav", ".ogg")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", wav_path,
            "-c:a", "libopus",
            "-b:a", "48k",
            "-ar", "48000",
            "-ac", "1",
            "-application", "voip",
            ogg_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    os.unlink(wav_path)
    return ogg_path


def _mp3_to_ogg(mp3_path: str) -> str:
    """Convert MP3 to OGG Opus. Returns ogg path."""
    ogg_path = mp3_path.replace(".mp3", ".ogg")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", mp3_path,
            "-c:a", "libopus",
            "-b:a", "48k",
            "-ar", "48000",
            "-ac", "1",
            "-application", "voip",
            ogg_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    os.unlink(mp3_path)
    return ogg_path


def _build_prompt(text: str) -> str:
    """Build a prompt for Gemini TTS.

    Single leading tag:  "[excitedly] 太好了！" → director instruction + clean transcript
    Multiple inline tags or no tags: passed through as-is (Gemini natively handles inline audio tags)
    """
    tags = re.findall(r"\[[^\]]+\]", text)
    if len(tags) == 1:
        m = re.match(r"^\[([^\]]+)\]\s*", text)
        if m:
            emotion = m.group(1)
            transcript = text[m.end():]
            return f"Say the following in Chinese, {emotion}:\n{transcript}"
    return text


def _ensure_gemini_api_key():
    """Ensure GEMINI_API_KEY is in env; load from ~/.zshenv or ~/.claude/settings.json if missing."""
    INVALID_VALUES = {"proxy", "", "your-api-key-here"}
    for var in ("GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY", "GOOGLE_API_KEY"):
        val = os.environ.get(var, "")
        if val and val.lower() not in INVALID_VALUES and val.startswith("AIza"):
            return
    # Try ~/.zshenv
    zshenv = os.path.expanduser("~/.zshenv")
    if os.path.exists(zshenv):
        with open(zshenv) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export GEMINI_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        os.environ["GEMINI_API_KEY"] = val
                        return
    # Try ~/.claude/settings.json (bot process may not inherit Claude Code env)
    import json
    settings_path = os.path.expanduser("~/.claude/settings.json")
    if os.path.exists(settings_path):
        try:
            with open(settings_path) as f:
                settings = json.load(f)
            env = settings.get("env", {})
            for var in ("GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY", "GOOGLE_API_KEY"):
                if env.get(var):
                    os.environ[var] = env[var]
                    return
        except Exception:
            pass


def generate_gemini(text: str, voice: str) -> str:
    """Generate OGG Opus audio via Gemini 3.1 Flash TTS."""
    from google import genai
    from google.genai import types

    _ensure_gemini_api_key()
    api_key = os.environ.get("GEMINI_API_KEY", "")
    client = genai.Client(api_key=api_key)
    voice_name = GEMINI_VOICES.get(voice, voice.capitalize())
    prompt = _build_prompt(text)

    response = client.models.generate_content(
        model="gemini-3.1-flash-tts-preview",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name,
                    )
                )
            ),
        ),
    )

    data = response.candidates[0].content.parts[0].inline_data.data
    if isinstance(data, str):
        data = base64.b64decode(data)

    wav_path = tempfile.mktemp(suffix=".wav", prefix="tts-")
    _wav_write(wav_path, data)
    return _wav_to_ogg(wav_path)


async def generate_edge(text: str, voice: str, rate: str) -> str:
    """Generate OGG Opus audio via Edge TTS (fallback)."""
    import edge_tts

    voice_full = EDGE_VOICES.get(voice, voice)
    mp3_path = tempfile.mktemp(suffix=".mp3", prefix="tts-")
    communicate = edge_tts.Communicate(text, voice_full, rate=rate)
    await communicate.save(mp3_path)
    return _mp3_to_ogg(mp3_path)


def main():
    parser = argparse.ArgumentParser(description="Generate speech audio (Gemini TTS / Edge TTS)")
    parser.add_argument("text", help="Text to convert to speech (supports Gemini audio tags like [excitedly])")
    parser.add_argument(
        "--voice", default=None,
        help="Voice name (default: aoede for Gemini, xiaoxiao for Edge)",
    )
    parser.add_argument(
        "--engine", default="gemini", choices=["gemini", "edge"],
        help="TTS engine: gemini (default) or edge (fallback)",
    )
    parser.add_argument(
        "--rate", default="+0%",
        help="Speech rate (Edge TTS only), e.g. '+20%%' or '-10%%'",
    )
    parser.add_argument(
        "--list-voices", action="store_true",
        help="List available voices for selected engine",
    )
    args = parser.parse_args()

    if args.list_voices:
        if args.engine == "gemini":
            print("Gemini 3.1 Flash TTS voices:")
            for short, full in GEMINI_VOICES.items():
                print(f"  {short:16s} → {full}")
        else:
            print("Edge TTS voices:")
            for short, full in EDGE_VOICES.items():
                print(f"  {short:12s} → {full}")
        return

    engine = args.engine
    voice = args.voice

    if engine == "gemini":
        if voice is None:
            voice = DEFAULT_GEMINI_VOICE
        try:
            ogg_path = generate_gemini(args.text, voice)
        except Exception as e:
            import traceback
            print(f"Gemini TTS failed: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            try:
                with open("/tmp/tts-gemini-debug.log", "a") as dbg:
                    from datetime import datetime
                    dbg.write(f"\n{'='*60}\n{datetime.now()}\n")
                    dbg.write(f"Error: {e}\n")
                    traceback.print_exc(file=dbg)
                    dbg.write(f"GEMINI_API_KEY set: {'GEMINI_API_KEY' in os.environ}\n")
                    dbg.write(f"GOOGLE_API_KEY set: {'GOOGLE_API_KEY' in os.environ}\n")
            except Exception:
                pass
            voice = DEFAULT_EDGE_VOICE
            ogg_path = asyncio.run(generate_edge(args.text, voice, args.rate))
    else:
        if voice is None:
            voice = DEFAULT_EDGE_VOICE
        ogg_path = asyncio.run(generate_edge(args.text, voice, args.rate))

    print(ogg_path)


if __name__ == "__main__":
    main()
