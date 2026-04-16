---
name: veo-generator
description: Generate videos using Google Veo 3.1 on Vertex AI. Use when the user says "生成视频", "做个视频", "generate video", "帮我生成一段视频", "create a video", "视频生成", "text to video", "文生视频", or when you need to create video content.
---

# Veo 3.1 Video Generator

Generate videos from text prompts using Veo 3.1 on Vertex AI, save to CC Pages, and send to Discord.

## Usage

Call the generation script directly:

```bash
~/.claude/skills/veo-generator/scripts/veo-generate.sh "your prompt here"
```

### Options

```bash
# Basic generation (1 video, 16:9, 8s, standard model, 720p)
~/.claude/skills/veo-generator/scripts/veo-generate.sh "a cat playing with a laser pointer"

# Portrait video for mobile
~/.claude/skills/veo-generator/scripts/veo-generate.sh "smartphone app demo" --aspect 9:16

# Shorter clip
~/.claude/skills/veo-generator/scripts/veo-generate.sh "ocean waves at sunset" --duration 6

# Faster model (lower quality but quicker)
~/.claude/skills/veo-generator/scripts/veo-generate.sh "cinematic drone shot over mountains" --model fast

# 1080p resolution
~/.claude/skills/veo-generator/scripts/veo-generate.sh "product showcase" --resolution 1080p

# Multiple videos (1-4)
~/.claude/skills/veo-generator/scripts/veo-generate.sh "abstract art animation" --count 2

# Negative prompt (avoid certain content)
~/.claude/skills/veo-generator/scripts/veo-generate.sh "peaceful park scene" --negative "people, crowds, text"

# Disable prompt rewriter (use exact prompt)
~/.claude/skills/veo-generator/scripts/veo-generate.sh "minimal geometric shapes" --no-rewrite

# Custom output filename
~/.claude/skills/veo-generator/scripts/veo-generate.sh "logo animation" --output brand-intro

# Longer timeout for complex videos
~/.claude/skills/veo-generator/scripts/veo-generate.sh "detailed cityscape timelapse" --timeout 600

# Combine options
~/.claude/skills/veo-generator/scripts/veo-generate.sh "B200 GPU rack with blinking LEDs, cinematic" --aspect 16:9 --model standard --resolution 1080p --duration 8
```

### Image-to-Video

Use `--image` to pass an input image as the first frame:

```bash
# Image-to-video: animate an existing image
~/.claude/skills/veo-generator/scripts/veo-generate.sh "the robot starts walking forward" --image /path/to/robot.png

# With Imagen pipeline: generate image first, then animate
IMG_URL=$(~/.claude/skills/imagen-generator/scripts/imagen-generate.sh "a futuristic robot standing in a hangar, cinematic" --aspect 16:9)
IMG_PATH="${CC_PAGES_WEB_ROOT}/assets/imagen/$(basename "$IMG_URL")"
VID_URL=$(~/.claude/skills/veo-generator/scripts/veo-generate.sh "the robot powers up and walks forward" --image "$IMG_PATH")

# Reference images for visual guidance (up to 3 asset images)
~/.claude/skills/veo-generator/scripts/veo-generate.sh "a product showcase video" --ref product-front.png --ref product-side.png
```

### Output

- Videos saved to `$CC_PAGES_WEB_ROOT/assets/veo/` as MP4
- Returns the public URL: `$CC_PAGES_URL_PREFIX/assets/veo/{filename}.mp4`
- When called from Discord context, send the URL using `send-to-discord.sh --plain`

### Workflow for Discord

```bash
# 1. Generate video
URL=$(~/.claude/skills/veo-generator/scripts/veo-generate.sh "your prompt" --aspect 16:9)

# 2. Send to Discord
~/.claude/scripts/send-to-discord.sh --plain "$URL"
```

## Important: Async Operation

Unlike Imagen (synchronous), Veo uses **long-running operations**:
1. Submit request → get operation ID
2. Poll `fetchPredictOperation` every N seconds
3. When `done: true`, extract video bytes or GCS URI

The script handles all of this automatically. Default poll interval is 10s, timeout is 300s (5 min). For complex prompts, use `--timeout 600`.

**Progress output goes to stderr**, so `URL=$(...veo-generate.sh...)` captures only the final URL.

## Models

- **`veo-3.1-generate-001`** (standard, default) — Best quality, GA
- **`veo-3.1-fast-generate-001`** (fast) — Faster generation, good quality, GA
- **`veo-3.1-lite-generate-001`** (lite) — Lightest, supports audio generation, Preview

### Older Models (available but not default)

- `veo-3.0-generate-001` / `veo-3.0-fast-generate-001` — Veo 3 GA
- `veo-2.0-generate-001` — Veo 2 (no 1080p support)

## Parameters

- **Aspect Ratio**: `16:9` (landscape, default), `9:16` (portrait)
- **Duration**: 4, 6, or 8 seconds (default: 8)
- **Resolution**: `720p` (default), `1080p` (Veo 3+ only)
- **Sample Count**: 1-4 videos per request
- **Negative Prompt**: describe content to avoid
- **Enhance Prompt**: Gemini-powered prompt rewriting (on by default)
- **Person Generation**: `allow_adult` (default), `disallow`
- **Image Input** (`--image`): Local image path, used as first frame for image-to-video
- **Reference Images** (`--ref`): Up to 3 asset reference images for visual guidance

## Prompt Tips

- Be specific: "A golden retriever running through autumn leaves in slow motion, cinematic lighting" > "dog running"
- Specify camera movement: "dolly zoom", "tracking shot", "aerial drone shot", "timelapse"
- Specify style: "cinematic", "documentary", "anime", "watercolor", "photorealistic"
- Supports Chinese prompts natively
- Use `--negative` to exclude unwanted elements

## Prerequisites

- `google-genai` Python SDK (`pip install google-genai`)
- Application Default Credentials configured (`gcloud auth application-default login`)
- Vertex AI API enabled on the GCP project
- CC Pages (GCS-backed via `$CC_PAGES_WEB_ROOT`)

## Files

```
~/.claude/skills/veo-generator/scripts/
├── veo-generate.sh                  # Entry point (exec wrapper)
└── veo-generate.py                  # Core logic (google-genai SDK)

$CC_PAGES_WEB_ROOT/assets/veo/      # Generated videos (web-accessible)
```
