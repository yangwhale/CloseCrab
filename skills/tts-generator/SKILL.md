---
name: tts-generator
description: Generate speech audio from text using Gemini 3.1 Flash TTS (with emotion control) or Edge TTS fallback. Use when the user says "/tts", "语音合成", "文字转语音", "念出来", "读出来", "生成语音", "帮我念", "说出来", or when asked to convert text to speech audio.
---

# TTS Generator (Gemini 3.1 Flash TTS)

Generate expressive speech audio using Google Gemini 3.1 Flash TTS with emotion/audio tag support. Falls back to Edge TTS if Gemini is unavailable.

## Usage

```bash
# Generate OGG audio (Gemini, default)
OGG=$(~/.claude/skills/tts-generator/scripts/tts-generate.py "[casually] 你好世界")

# Choose voice
OGG=$(~/.claude/skills/tts-generator/scripts/tts-generate.py "[excitedly] 太棒了！" --voice sulafat)

# Fallback to Edge TTS
OGG=$(~/.claude/skills/tts-generator/scripts/tts-generate.py "你好" --engine edge --voice xiaoxiao)

# Send as Discord voice message
~/.claude/scripts/send-to-discord.sh --voice "$OGG"

# Clean up
rm -f "$OGG"
```

## Gemini Voices (15 voices)

| 代号 | 风格 | 代号 | 风格 |
|------|------|------|------|
| `aoede` (default) | 轻松 | `achird` | 友好 |
| `sulafat` | 温暖 | `kore` | 坚定 |
| `puck` | 欢快 | `charon` | 知性 |
| `fenrir` | 兴奋 | `leda` | 青春 |
| `zephyr` | 明亮 | `achernar` | 柔和 |
| `gacrux` | 成熟 | `sadachbia` | 活泼 |
| `algieba` | 顺滑 | `vindemiatrix` | 温柔 |
| `orus` | 坚定 | | |

## Emotion Tags (Gemini only)

Gemini TTS supports inline audio tags for emotion and style control:

```bash
# 常用情绪
"[casually] 今天的进度还不错"
"[excitedly] 实验通过了！"
"[thoughtfully] 这个方案有两个权衡点"
"[seriously] 注意这个安全隐患"
"[cheerfully] 早上好！"
"[calmly] 总结一下今天的工作"

# 特殊效果
"[whispers] 这是个秘密"
"[laughs] 没问题"
"[sighs] 又要加班了"
"[surprised] 真的吗？"
```

## Edge TTS Voices (fallback)

- `xiaoxiao` — 女声（默认）
- `xiaoyi` — 女声，温柔
- `yunxi` — 男声，活力
- `yunjian` — 男声，成熟
- `yunyang` — 男声，新闻播报
- `yunxia` — 男声，少年

## Notes

- Output: OGG Opus (1ch, 48kHz, 48kbps)
- Gemini TTS needs `GOOGLE_API_KEY` env var or GCP Application Default Credentials
- Gemini supports 80+ languages with auto-detection
- If Gemini fails, automatically falls back to Edge TTS
