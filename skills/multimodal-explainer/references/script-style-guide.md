# Voice Narration Script Style Guide

This file defines the spoken-script style for TTS narration segments.

## Default Voice Mode Style (Chinese)

Match the conversational "voice mode" style users expect from a friendly technical buddy explaining concepts. Not a formal lecture, not a marketing pitch.

### Hard Rules

- **No markdown** — no `**bold**`, no `# headers`, no bullet `-`. TTS reads them literally.
- **No emojis** — TTS reads them as 句号 / character names.
- **No URLs / no file paths** — TTS chokes on `/`, `:`, `.`. Reference them as "前面 HTML 文档里的链接".
- **Short sentences** — 25-50 Chinese characters per sentence. Long sentences make TTS pace feel monotone.
- **Avoid English-Chinese code-switching density** — alternating every word slows TTS pacing. OK: "用 GRPO 算法跑 RL". Bad: "在 PT phase 我们 use next-token prediction 来 learn world knowledge".
- **Spell out symbols**: `/` → "斜杠", `=` → "等于", `>` → "大于", `<` → "小于", `%` → "百分之", `→` → "到"
- **Hyphens in model names** break TTS pacing — replace with spaces:
  - `Qwen3.5-397B-A17B` → `Qwen3.5 397B A17B`
  - `DeepSeek-R1` → `DeepSeek R1`
  - `on-policy distillation` → `on policy distillation`
- **Numbers**: Arabic numerals OK ("3970 亿"), but very long numbers should be grouped ("一千零二十四" instead of "1024" in some contexts feels more natural — judge by context)

## Gemini Emotion Tags

Lead each script with a tag. Switch every 1-3 paragraphs to match content emotion. **One tag per paragraph at most**, never multiple in the same paragraph.

| Tag | When to use | Example trigger |
|---|---|---|
| `[casually]` | Default conversational opening, section intros | "来咱们看一下..." |
| `[thoughtfully]` | Explaining nuance, drawing distinctions | "这里有个关键区别..." |
| `[contemplative]` | Slower, pondering complex tradeoffs | "想想看为什么..." |
| `[realization]` | "Aha" moment, key insight surfacing | "关键来了..." / "注意到没..." |
| `[seriously]` | Warnings, risks, contradictions, must-know | "风险点来了..." / "客户文档自相矛盾..." |
| `[excitedly]` | Good news, breakthrough, performance win | "搞定了..." / "实测领先..." |
| `[cheerfully]` | Positive resolution, encouragement | "皆大欢喜..." |
| `[playful]` | Light recap, joke, low-stakes summary | "这段就这样..." / "都答出来就 OK 了..." |
| `[calmly]` | Steady technical explanation, no drama | (rarely needed; default to casually) |
| `[suggestion]` | Recommending an approach | "建议你..." |
| `[neutral]` | Pure facts, no emotion | "数据是..." |

### Tag Density Heuristic

A 2000-character script ≈ 7-12 paragraphs ≈ 4-7 tag changes. Don't over-tag — too many switches make narration feel manic. Don't under-tag — single tag for whole 7-minute segment sounds robotic.

## Structure of a Good Script

### Opening (1 sentence)
`[casually]` Lead with what this segment will cover. Set expectation for length / difficulty.

> "来啃硬骨头，强化学习这一段。这是这次 PoC 最难、争议最多的部分，我会讲得细一点。"

### Body (5-10 paragraphs)
Each paragraph = one concept. Pattern: introduce term → define → example → why it matters.

Use these connectors to maintain flow:
- "先说..." / "首先..."
- "接下来..."
- "然后..."
- "再讲..."
- "最后..."

### Recap (1 sentence)
End with what listener should now know. Helps anchor learning.

> "听完这段你应该能搞清楚 RL 是干啥的、三大算法是啥、OPD 跟 OPSD 啥区别、reward 怎么设计、监控盯啥、Functional 跟 Convergence 怎么 trade off。"

## Length Limits (CRITICAL)

| Chinese chars | Audio duration | TTS status |
|---|---|---|
| ≤ 1500 | ~1m30s-2m30s | ✅ Safe |
| 1500-2000 | ~2m30s-3m30s | ✅ OK |
| 2000-2500 | ~3m30s-4m30s | ⚠️ Risky, monitor for fail |
| > 2500 | > 4m30s | ❌ **Silently fails to ~1s silence** |

### Detecting failure

```bash
# After generation, OGG < 50KB = failed
[ $(stat -c%s file.ogg) -lt 51200 ] && echo "FAILED"

# Or check duration
dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 file.ogg)
[ ${dur%.*} -lt 30 ] && echo "FAILED (only ${dur}s)"
```

### When a segment fails

1. Split the original script at a natural breakpoint (usually a section transition like "接下来讲 reward")
2. Rename files `04-rl.txt` → `04a-rl-algorithms.txt` + `04b-rl-reward-metrics.txt`
3. Add a transition phrase at the start of part B: "这是 RL 第二小节，讲..."
4. Regenerate both
5. In HTML, embed two `<audio>` players in the same section

## English Variation (when user requests English narration)

- Same length limits apply (~2000 English words = ~3.5min, similar threshold)
- Gemini emotion tags work in English narration too
- Default voice changes — `puck` (Upbeat), `achird` (Friendly), or `sulafat` (Warm) tend to fit English casual style better than `orus`
- Drop the "听完这段..." recap pattern; use "By the end of this segment you should be able to..." instead
