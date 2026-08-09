---
name: chinese-dictation
description: Generate Chinese (traditional/simplified) dictation practice — extract words+pinyin from a screenshot, produce an HTML page with audio that reads each word aloud with configurable pause between words. Use when user says "听写"、"聽寫"、"语文听写"、"听写练习"、"dictation"、"做个听写"、"听写卡"、"听写音频".
---

# Chinese Dictation Generator

Given a list of Chinese words (with pinyin), produce a self-contained HTML page that:
1. Lists every word + pinyin (hidden by default, click to reveal — encourages real practice).
2. Generates one tiny ogg per word, and embeds a **JavaScript scheduler** that plays them sequentially with a **client-side adjustable interval**. User can change the gap (e.g. 5s → 10s), pause/resume, skip to next word, or stop entirely — all live in the browser, no need to regenerate.

Designed for elementary-school 中文/語文 homework. Defaults to 繁體中文 reading voice; works for simplified too.

## When to invoke

User (Chris) typically shares a screenshot of a textbook 生字 page or 聽寫表. **The expected workflow is fully autonomous**: read the image → extract 字+拼音 → generate the dictation page → send the URL. Don't ask back about voice/pause/etc — defaults are already production-tested. If the page is part of a series (e.g. user already did page 24, now sends page 25), use a slug like `pageNN` for clean URL grouping.

## Workflow (Claude side)

### Step 1 — Extract words from the image

You (Claude) read the user's screenshot directly. For each entry on the page, extract:
- `hanzi` — the character(s) to dictate (preserve traditional form if the source is traditional)
- `pinyin` — full pinyin with tone marks (e.g. `qiū qiān`, not `qiu1 qian1`)

If pinyin is missing from the image but you're confident, fill it in. If a word is ambiguous, ask the user before generating.

### Step 2 — Run the generator

Write the words to `/tmp/dictation-input.json`:

```json
{
  "title": "第一週聽寫",
  "slug": "week1",
  "words": [
    {"hanzi": "鞦韆", "pinyin": "qiū qiān"},
    {"hanzi": "翱翔", "pinyin": "áo xiáng"},
    {"hanzi": "璀璨", "pinyin": "cuǐ càn"}
  ],
  "pause_seconds": 20,
  "repeat": 2,
  "voice": "vindemiatrix"
}
```

Then run:

```bash
python3 ~/.claude/skills/chinese-dictation/scripts/generate.py /tmp/dictation-input.json
```

Output (one line, the public URL):

```
https://www.closecrab.com/pages/dictation-week1-20260523-145912.html
```

Send that URL to the user.

**Important**: `title` is the Chinese display name shown in the page; `slug` is the **ASCII-only** filename component for clean URLs. If you omit `slug`, the URL falls back to `dictation-{timestamp}.html` (still clean, just less descriptive). Never put Chinese in the URL — picks like `week1`, `unit3-lesson5`, `2025-spring` work well.

### Defaults / tuning

| Field | Default | Notes |
|-------|---------|-------|
| `pause_seconds` | `5` | **Default value pre-filled in the page's input box** — user can change it live (1-60s) without regenerating. Pick a sensible default for the typical student. |
| `repeat` | `2` | How many times each word is read. Standard dictation reads twice. (Baked into each word's ogg; not adjustable client-side.) |
| `voice` | `erinome` | **Verified by Chris (2026-05-23) as the best Gemini voice for 繁體中文 dictation** — sounds like a CCTV news anchor. The only "Clear"-tagged female voice in Gemini's 30. Don't change unless explicitly asked. |
| `engine` | `gemini` | `edge` is also supported (`voice: zh-TW-HsiaoChenNeural`) but lacks emotion — Chris prefers gemini+erinome. |
| `title` | `聽寫-MMDD` | Chinese display name shown on the page (h1 + browser tab). |
| `slug` | _(none)_ | Optional ASCII slug for the URL. If omitted, URL is `dictation-{timestamp}.html`. |

### What the generated HTML contains

- **Header** — title, date, word count, estimated duration.
- **Audio player** — single `<audio>` element, the user clicks play and writes along.
- **Word grid** — every word shown as a card with hanzi + pinyin **blurred by default**. Click a card to reveal (for self-check after listening). A "全部顯示 / 全部隱藏" toggle button up top.
- **Print-friendly** — `@media print` reveals all answers for the teacher's answer key.

## Pause tuning

The first time you generate dictation for a particular student, suggest the user try `pause_seconds: 20`, then offer to regenerate with adjusted timing based on feedback ("太快了" → 25, "太慢了" → 15).

## Tips

- **Traditional vs simplified**: pass `hanzi` exactly as the user wants to practice. The script doesn't auto-convert.
- **Multi-character words** (詞組): treat as a single dictation unit, e.g. `{"hanzi": "鞦韆"}` not two entries.
- **Phrases with same word reading multiple times**: `repeat: 3` if they need more practice.

## Files

- `scripts/generate.py` — main entry, JSON → HTML+audio → CC Pages upload.
- Dependencies: `ffmpeg`, `tts-generator` skill (Gemini 3.1 Flash TTS).
