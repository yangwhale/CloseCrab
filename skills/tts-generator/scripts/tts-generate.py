#!/usr/bin/env python3
"""Generate speech audio from text using Microsoft Edge TTS (Neural).

Usage:
    tts-generate.py "你好世界"
    tts-generate.py "你好" --voice yunxi
    tts-generate.py "你好" --rate "+20%"

Output: prints the path to the generated .ogg file on stdout.
"""

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile

import edge_tts

VOICE_MAP = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "xiaoyi": "zh-CN-XiaoyiNeural",
    "yunxi": "zh-CN-YunxiNeural",
    "yunjian": "zh-CN-YunjianNeural",
    "yunyang": "zh-CN-YunyangNeural",
    "yunxia": "zh-CN-YunxiaNeural",
}

DEFAULT_VOICE = "xiaoxiao"


async def generate(text: str, voice: str, rate: str) -> str:
    """Generate OGG Opus audio from text. Returns the output file path."""
    voice_full = VOICE_MAP.get(voice, voice)

    # Generate mp3 with edge-tts
    mp3_path = tempfile.mktemp(suffix=".mp3", prefix="tts-")
    communicate = edge_tts.Communicate(text, voice_full, rate=rate)
    await communicate.save(mp3_path)

    # Convert mp3 → ogg opus (Discord voice message format)
    ogg_path = mp3_path.replace(".mp3", ".ogg")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", mp3_path,
            "-c:a", "libopus",
            "-b:a", "32k",
            "-ar", "48000",
            "-ac", "1",
            "-application", "voip",
            ogg_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    # Clean up mp3
    os.unlink(mp3_path)
    return ogg_path


def main():
    parser = argparse.ArgumentParser(description="Generate speech with Edge TTS")
    parser.add_argument("text", help="Text to convert to speech")
    parser.add_argument(
        "--voice", default=DEFAULT_VOICE,
        help=f"Voice name: {', '.join(VOICE_MAP.keys())} (default: {DEFAULT_VOICE})",
    )
    parser.add_argument(
        "--rate", default="+0%",
        help="Speech rate adjustment, e.g. '+20%%' or '-10%%' (default: +0%%)",
    )
    parser.add_argument(
        "--list-voices", action="store_true",
        help="List available voice short names",
    )
    args = parser.parse_args()

    if args.list_voices:
        for short, full in VOICE_MAP.items():
            print(f"  {short:12s} → {full}")
        return

    ogg_path = asyncio.run(generate(args.text, args.voice, args.rate))
    print(ogg_path)


if __name__ == "__main__":
    main()
