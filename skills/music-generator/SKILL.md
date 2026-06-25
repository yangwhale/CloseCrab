---
name: music-generator
description: Generate music using Google Lyria 3 (DeepMind). Use for "生成音乐", "作曲", "写一首曲子", "generate music", "create a song", "compose", "piano piece", "钢琴曲", "make a beat", "生成钢琴曲", "来一首歌".
---

# Music Generator (Google Lyria 3)

Generate music using Google Lyria 3 Pro / Clip (DeepMind) via Gemini API, save to CC Pages, and share.

Two models:
- **Lyria 3 Clip** (`lyria-3-clip-preview`) — 30-second clips, fast iteration
- **Lyria 3 Pro** (`lyria-3-pro-preview`) — full-length songs (1-3 minutes)

## Usage

```bash
~/.claude/skills/music-generator/scripts/music-generate.sh "your music prompt"
```

### Options

```bash
# Quick 30s clip (default)
~/.claude/skills/music-generator/scripts/music-generate.sh "a dramatic solo piano piece in C minor"

# Full-length song (1-3 min)
~/.claude/skills/music-generator/scripts/music-generate.sh "epic orchestral film score" --duration full

# Specify style, key, tempo
~/.claude/skills/music-generator/scripts/music-generate.sh "jazz trio" --style jazz --key "Bb major" --tempo 140

# Allow vocals (default: instrumental only)
~/.claude/skills/music-generator/scripts/music-generate.sh "pop ballad with singing" --vocal

# Custom output filename
~/.claude/skills/music-generator/scripts/music-generate.sh "ambient soundscape" --output my-ambient

# Combine options
~/.claude/skills/music-generator/scripts/music-generate.sh \
  "virtuoso solo piano, Late Romantic style, dramatic arpeggios and singing melody" \
  --duration full --key "C# minor" --tempo 72 --output concert-piece
```

### Output

- Audio saved to `$CC_PAGES_WEB_ROOT/assets/music/` as MP3
- Returns the public URL: `$CC_PAGES_URL_PREFIX/assets/music/{filename}.mp3`

### Workflow

```bash
# 1. Generate music
URL=$(~/.claude/skills/music-generator/scripts/music-generate.sh "solo piano nocturne" --duration full)

# 2. Share URL or embed in HTML
echo "Listen: $URL"
```

## Prompt Engineering Tips

1. **Use timestamps for structure**: `[0:00-0:25] slow intro, [0:25-1:00] build intensity, [1:00-1:30] climax`
2. **Use Italian musical terms**: Allegro, Pianissimo, Rubato, Con fuoco, Crescendo, Sforzando
3. **Specify instrumentation explicitly**: "solo piano", "string quartet", "jazz trio with piano, upright bass, and brushed drums"
4. **NO copyrighted references**: Never name specific compositions, artists, or songs — the API will block with PROHIBITED_CONTENT. Use generic style descriptions instead ("Late Romantic virtuoso piano", "bebop jazz")
5. **Repeat "NO vocals" for instrumental**: Say "Instrumental only. NO vocals. NO singing." at least twice if you want pure instrumental
6. **Describe dynamics and emotion**: "haunting and melancholic opening, building to a passionate fortissimo climax, then resolving with gentle pianissimo"

## Models

| Model | Duration | Best For |
|-------|----------|----------|
| `lyria-3-clip-preview` (default) | ~30s | Quick previews, iteration, short loops |
| `lyria-3-pro-preview` | 1-3 min | Full compositions, concert pieces |

## Prerequisites

- `google-genai` Python SDK (`pip install google-genai`)
- `GEMINI_API_KEY` environment variable set
- CC Pages (`$CC_PAGES_WEB_ROOT` and `$CC_PAGES_URL_PREFIX`)

## Files

```
~/.claude/skills/music-generator/scripts/
├── music-generate.sh                   # Entry point (exec wrapper)
└── music-generate.py                   # Core logic (google-genai SDK)

$CC_PAGES_WEB_ROOT/assets/music/       # Generated audio (web-accessible)
```
