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

Gemini TTS 支持 inline audio tags 控制情感和风格。**有两种工作模式**，行为不同：

### 模式 A：单标签（director instruction）

只有一个开头标签时，脚本把它翻译成 director 指令喂给 Gemini：`"[casually] 你好"` → `"Say the following in Chinese, casually:\n你好"`。**这种模式下任意词都能用**（因为 Gemini 把它当自然语言指令理解），所以下面这些"自创"标签都能工作：

```bash
"[casually] 今天的进度还不错"        # 随意
"[excitedly] 实验通过了！"           # 兴奋
"[thoughtfully] 这个方案有两个权衡点" # 思考
"[seriously] 注意这个安全隐患"        # 严肃
"[cheerfully] 早上好！"              # 愉快
"[calmly] 总结一下今天的工作"         # 平静
```

### 模式 B：多个 inline 标签（raw passthrough）

文本里有 ≥2 个标签时，脚本不动它，原样 pass 给 Gemini。Gemini **只认它训练时见过的官方词**，自创词会被静默丢弃。一段话里想多次切换情绪（推荐用法，更生动）必须用下面的官方词：

**思考类**：`[thinking] [contemplative] [analysis] [focus] [reflection] [planning] [speculation] [pensive] [curiosity]`

**积极类**：`[excitement] [enthusiasm] [joy] [happy] [pleased] [optimism] [playful] [amusement] [friendly] [triumph] [satisfaction]`

**中性类**：`[neutral] [contentment] [serenity] [relaxation] [certainty]`

**严肃类**：`[seriousness] [urgency] [warning] [concern] [caution] [emphasis]`

**惊讶类**：`[surprise] [amazement] [realization] [confusion] [uncertainty] [doubt] [disbelief]`

**消极类**：`[disappointment] [frustration] [regret] [exhaustion] [weariness]`

**幽默类**：`[humor] [sarcasm] [amused] [self-deprecation]`

**自信类**：`[confidence] [determination] [assertive] [pride]`

**特效类**：`[whispers] [laughs] [sighs] [slow] [fast]`

**说明类**：`[informative] [explaining] [summary] [instruction] [suggestion]`

**多标签例子（好）**：
```
[curiosity] 我先看下日志。[realization] 哦原来是端口冲突。
[amused] 这种小坑最烦了。[suggestion] 你 kill 8080 那个就行。
```

**多标签例子（差）**：
```
[casually] 我看了日志发现端口冲突 你 kill 8080 就行
# 整段一个标签，且 [casually] 不是官方词，多标签场景下还会被丢
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
