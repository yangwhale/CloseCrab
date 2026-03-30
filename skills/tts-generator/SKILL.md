---
name: tts-generator
description: Generate speech audio from text using Edge TTS. Use when the user says "/tts", "语音合成", "文字转语音", "念出来", "读出来", "生成语音", "帮我念", "说出来", or when asked to convert text to speech audio.
---

# TTS Generator (Edge TTS)

Generate speech audio from text using Microsoft Edge TTS Neural voices, and send as Discord voice message.

## Usage

```bash
# Generate OGG audio file
OGG=$(~/.claude/skills/tts-generator/scripts/tts-generate.py "要转换的文本" --voice xiaoxiao)

# Send as Discord voice message
~/.claude/scripts/send-to-discord.sh --voice "$OGG"

# Clean up
rm -f "$OGG"
```

### Options

```bash
# Default voice (xiaoxiao)
~/.claude/skills/tts-generator/scripts/tts-generate.py "你好世界"

# Choose voice
~/.claude/skills/tts-generator/scripts/tts-generate.py "你好" --voice yunxi

# Adjust speed
~/.claude/skills/tts-generator/scripts/tts-generate.py "你好" --rate "+20%"
~/.claude/skills/tts-generator/scripts/tts-generate.py "你好" --rate "-10%"

# List available voices
~/.claude/skills/tts-generator/scripts/tts-generate.py "" --list-voices
```

## Available Voices

- `xiaoxiao` (default) — 女声，最受欢迎
- `xiaoyi` — 女声，温柔
- `yunxi` — 男声，年轻活力
- `yunjian` — 男声，沉稳成熟
- `yunyang` — 男声，新闻播报
- `yunxia` — 男声，少年

## Workflow

1. Generate OGG Opus audio from text
2. Send as Discord voice message (with waveform visualization)
3. Clean up temp file

## Notes

- Output format: OGG Opus (1ch, 48kHz, 32kbps) — Discord voice message standard
- Edge TTS is free (uses Microsoft Edge's Read Aloud backend)
- Supports Chinese text with English technical terms mixed in
