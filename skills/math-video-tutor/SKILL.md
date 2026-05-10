---
name: math-video-tutor
description: Generate teaching video explanations for elementary math problems. Takes a math problem (with answer and solution steps) and produces a 2-3 minute MP4 video with dark-themed slides and TTS narration, suitable for 5th graders. Use when asked to create math teaching videos, explain math problems to kids, or generate video tutorials for math exercises. Supports batch processing for multiple questions.
---

# Math Video Tutor

Generate teaching videos from math problems: dark-themed animated slides + TTS voice narration → MP4.

## Pipeline

```
Math problem → [Claude generates content] → JSON + narration.txt
  → gen_slides.py → HTML slides
  → screenshot_slides.js → PNG sequence (1920×1080)
  → tts-generate.py (charon voice) → OGG audio
  → compose_video.sh (ffmpeg) → MP4 video
  → gcloud storage cp → CC Pages URL
```

## Step-by-Step Workflow

### Step 1: Generate Content (Claude)

For each math problem, generate two files:

**1a. Narration script** (`content/q{N}_narration.txt`)

Write a 500-1500 character teaching script as if speaking to a 5th grader:
- Start with "同学们，我们来看第N题"
- Read the problem aloud with natural phrasing
- Walk through each step, explaining WHY not just WHAT
- State the answer clearly
- End with a method summary

**1b. Slides JSON** (`content/q{N}_slides.json`)

See [references/slides-schema.md](references/slides-schema.md) for the full schema.

Key fields: `q_num`, `topic`, `problem_text`, `answer`, `steps[]` (3-5 steps with `badge`, `title`, `content_html`), `summary_when`, `summary_how`, `tip`.

Use highlight classes in `content_html`: `hl-blue` (given values), `hl-green` (intermediate), `hl-amber` (key quantities), `hl-rose` (answers), `hl-purple` (totals). Wrap math in `<div class="math-block">`.

### Step 2: Run the Pipeline

Single question:
```bash
SKILL_DIR=~/.claude/skills/math-video-tutor/scripts
WORKDIR=/tmp/math-video

# Generate HTML slides
python3 $SKILL_DIR/gen_slides.py content/q1_slides.json slides/q1_slides.html

# Screenshot
node $SKILL_DIR/screenshot_slides.js slides/q1_slides.html slides/q1/

# TTS
NARRATION=$(cat content/q1_narration.txt | tr '\n' ' ')
OGG=$(~/.claude/skills/tts-generator/scripts/tts-generate.py \
  --voice charon "[温和亲切，像老师给小学生讲题] $NARRATION")
cp "$OGG" audio/q1_narration.ogg

# Compose video
bash $SKILL_DIR/compose_video.sh slides/q1/ audio/q1_narration.ogg videos/q01_explanation.mp4
```

Batch (all questions):
```bash
bash $SKILL_DIR/batch_process.sh /tmp/math-video 1 25
```

### Step 3: Upload to CC Pages

```bash
gcloud storage cp videos/q01_explanation.mp4 \
  gs://chris-pgp-host-asia/cc-pages/assets/video-name.mp4
```

URL: `https://cc.higcp.com/assets/video-name.mp4`

### Step 4: Embed in HTML (optional)

```html
<div class="video-player">
  <div class="video-label">🎬 視頻講解</div>
  <video controls preload="none">
    <source src="https://cc.higcp.com/assets/video-name.mp4" type="video/mp4">
  </video>
</div>
```

## Parallelization

For batch content generation, use the Agent tool to spawn 4 agents, each handling a range of questions (e.g., Q1-6, Q7-12, Q13-18, Q19-25). Each agent writes `content/q{N}_slides.json` + `content/q{N}_narration.txt`.

Then run `batch_process.sh` which handles TTS and video composition with 4-way parallelism internally.

## Dependencies

- **playwright** (npm) + chromium — slide screenshots
- **ffmpeg** — video composition
- **tts-generator skill** — TTS audio (Gemini Flash, `charon` voice)
- **gcloud CLI** — upload to GCS/CC Pages
- **Noto Sans SC** font — loaded via Google Fonts CDN in slide HTML

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/gen_slides.py` | JSON → HTML slide deck |
| `scripts/screenshot_slides.js` | HTML → PNG screenshots (Playwright) |
| `scripts/compose_video.sh` | PNGs + audio → MP4 (ffmpeg) |
| `scripts/batch_process.sh` | Full pipeline for a range of questions |

## Slide Design

- Dark theme: `#0f172a` base, gradient backgrounds per slide type
- 1920×1080 resolution, Noto Sans SC font
- 5 slide types: title, problem, step (N), answer, summary
- Color-coded math highlights, bottom gradient bar
- Step icons cycle through blue/purple/amber/green/rose

## Visual Diagrams (CRITICAL)

When a problem involves visual/spatial concepts, the slides **MUST** include actual SVG diagrams — text descriptions alone are NOT sufficient. Students cannot understand these problems without seeing the figures.

### When to include diagrams

- **Venn diagrams** (韦恩图/容斥原理): Draw overlapping circles with labeled regions and values
- **Geometric figures**: Draw shapes, angles, auxiliary lines
- **Adjacency/coloring problems**: Draw the regions/blocks showing which areas share borders
- **Number lines, tables, grids**: Render as SVG, not text
- **Tree diagrams, flowcharts**: For counting/probability problems

### How to implement

For problems requiring diagrams, **bypass gen_slides.py** and write custom HTML directly:

1. Copy the CSS from an existing generated slide deck (dark theme, 1920×1080)
2. Create slides with inline `<svg>` elements in the slide content
3. Use the same color scheme: `#38bdf8` (blue), `#34d399` (green), `#fb7185` (rose), `#a78bfa` (purple), `#fbbf24` (amber)
4. Use `.two-col` layout for diagram + text side-by-side:
   ```html
   <div class="two-col">
     <div class="col-diagram"><svg>...</svg></div>
     <div class="col-text"><div class="step-body">...</div></div>
   </div>
   ```
5. Include the diagram on **multiple slides**: first as a blank/partial version (with unknowns), then as a completed version (with answer filled in)

### Example: Venn diagram slide

```html
<svg viewBox="0 0 800 550" width="900" height="620">
  <circle cx="320" cy="240" r="190" fill="rgba(56,189,248,0.12)" stroke="#38bdf8" stroke-width="3"/>
  <circle cx="480" cy="240" r="190" fill="rgba(52,211,153,0.12)" stroke="#34d399" stroke-width="3"/>
  <circle cx="400" cy="380" r="190" fill="rgba(251,113,133,0.12)" stroke="#fb7185" stroke-width="3"/>
  <text x="400" y="290" font-size="42" fill="#fbbf24" font-weight="900" text-anchor="middle">x = ?</text>
</svg>
```

## Quality Checklist

- [ ] Narration uses natural spoken Chinese, not written style
- [ ] Numbers read aloud (两百零一, not 201)
- [ ] Each step explains reasoning, not just calculation
- [ ] `problem_text` fits on one slide (abbreviate if needed)
- [ ] 3-5 steps per problem (not too many, not too few)
- [ ] Tip uses memorable phrasing (rhyme or mnemonic)
- [ ] **Visual problems MUST have SVG diagrams** — never describe a figure with text only
