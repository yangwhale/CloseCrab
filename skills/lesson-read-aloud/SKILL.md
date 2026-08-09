---
name: lesson-read-aloud
description: Turn lesson PDFs into a read-aloud study page — extract the text from each PDF, embed the original page screenshots, and attach an English audio player that reads the whole lesson aloud (a real click-to-play progress bar). Built for Hong Kong primary-school 常識科/General Studies lessons but works for any subject. Use when the user says "朗讀"、"課文朗讀"、"朗讀教學"、"常識科"、"小學常識"、"read aloud"、"配個朗讀音頻"、"念課文", or drops one-or-more lesson PDFs and asks for an audio read-along page.
---

# Lesson Read-Aloud Generator

Given one PDF **per lesson**, produce a self-contained multi-tab HTML page where each tab is one lesson containing:
1. an `<audio controls>` player that reads the whole lesson aloud (English voice, click-to-play progress bar),
2. the **original PDF page screenshots** (kids follow the real textbook), and
3. the extracted lesson text (large, clean, for reading along).

The user (Chris) typically drops **several PDFs at once — each PDF is one lesson** → each becomes its own tab.

## Workflow

### Step 1 — Extract each lesson's text (you, Claude)

Read every PDF the user sent with the Read tool. For each lesson, extract the **English text faithfully, word-for-word**, split into natural paragraphs. Preserve the reading order; skip page numbers, running headers, and pure decoration. If a word is illegible, transcribe your best guess — don't drop it.

You do **not** screenshot the pages yourself — the script does that via `pdftoppm`. Just keep each lesson's PDF path.

### Step 2 — Build the input JSON

Write to `/tmp/lesson-input.json` (one entry per lesson):

```json
{
  "title": "P1 常識科朗讀",
  "slug": "gs-p1-unit1",
  "voice": "kore",
  "lessons": [
    {
      "id": "L01",
      "title": "Lesson 1: My Body",
      "pdf": "/tmp/lesson1.pdf",
      "paragraphs": [
        "Our body has many parts.",
        "We use our eyes to see and our ears to hear."
      ]
    },
    {
      "id": "L02",
      "title": "Lesson 2: My Family",
      "pdf": "/tmp/lesson2.pdf",
      "paragraphs": ["..."]
    }
  ]
}
```

### Step 3 — Run the generator

```bash
python3 ~/.claude/skills/lesson-read-aloud/scripts/generate.py /tmp/lesson-input.json
```

It prints the public URL on stdout (stderr shows per-lesson progress). Send that URL to the user **bare, no quotes** (Feishu turns quotes into part of the link → 404).

```
https://www.closecrab.com/pages/lesson-gs-p1-unit1.html
```

## Defaults / fields

| Field | Default | Notes |
|-------|---------|-------|
| `slug` | _(required)_ | ASCII-only; the URL is `lesson-{slug}.html`. Same slug = same URL = updates in place. Use `gs-p1-unit1`, `2026-spring-week3`, etc. |
| `title` | `Read-Aloud Lesson` | Page heading (Chinese is fine — it's display text, not the URL). |
| `voice` | `kore` | English read-aloud voice. `kore` (clear, steady) is the default; `leda` (younger), `achernar` (softer), `charon` (knowledgeable) also work. Let the user hear it, then swap if they want a different one. |
| `engine` | `gemini` | `edge` is the fallback (no style control). |
| `lessons[].id` | _(required)_ | Short stable id like `L01` — used for filenames and tab anchors. |
| `lessons[].pdf` | _(optional)_ | PDF path → page screenshots. Omit for a text-only read-along page. |
| `lessons[].paragraphs` | _(one of two)_ | List of paragraph strings. Or use `lessons[].text` (a single string split on blank lines). |

## Caching & incremental updates

- Audio is **hash-named on the full lesson text + voice + engine**. Re-running with an unchanged lesson **skips TTS** for it — only changed/new lessons re-synthesize. Adding a lesson later = append it to the JSON and re-run; existing lessons stay cached.
- Bumping `PROMPT_VERSION` in `generate.py` invalidates all cached audio (only needed if the director prompt changes).
- Page screenshots re-render every run (cheap); stale pages for a lesson are cleared first.

## Notes

- **English voice mode**: the script wraps the text in a plain-English director prompt with **no `[...]` tags**, so `tts-generate.py` passes it through (its single-tag mode hardcodes "Say ... in Chinese", which would mangle English). Don't add bracket tags to lesson text.
- **One audio per lesson**: the whole lesson is synthesized in one call (paragraphs joined with blank lines for natural pauses). Fine for typical primary-school lessons (<~300 words). If a lesson is very long and TTS fails, split it into two lesson entries.
- Tab switching is **pure CSS** (radio inputs), no JavaScript — works offline and prints cleanly.
- Output goes to GCS-mounted `/gcs/cc-pages/`; writing the file *is* the upload. The only external resources are the PDF page PNGs under `/assets/lra-{slug}/`.

## Files

- `scripts/generate.py` — JSON → page screenshots + per-lesson TTS → multi-tab HTML on CC Pages.
- Dependencies: `pdftoppm` (poppler-utils), `tts-generator` skill (Gemini 3.1 Flash TTS).
