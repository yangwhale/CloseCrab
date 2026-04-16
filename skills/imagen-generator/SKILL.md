---
name: imagen-generator
description: Generate images using Gemini 3 Pro Image (Nano Banana) on Vertex AI. Use when the user says "生成图片", "画一张", "generate image", "帮我画", "生成一张图", "create an image", "图片生成", or when you need to create visual content to explain concepts.
---

# Gemini Image Generator (Nano Banana)

Generate images from text prompts using Gemini 3 Pro Image (gemini-3-pro-image-preview) on Vertex AI, save to CC Pages, and share.

## Usage

Call the generation script directly:

```bash
~/.claude/skills/imagen-generator/scripts/imagen-generate.sh "your prompt here"
```

### Options

```bash
# Basic generation (1 image, 1:1 aspect ratio, 1K resolution)
~/.claude/skills/imagen-generator/scripts/imagen-generate.sh "a cute cat sitting on a GPU server"

# Custom aspect ratio: 1:1, 3:4, 4:3, 16:9, 9:16, 3:2, 2:3, 4:5, 5:4, 21:9, 4:1, 1:4, 8:1, 1:8
~/.claude/skills/imagen-generator/scripts/imagen-generate.sh "TPU pod in a datacenter" --aspect 16:9

# Multiple images (1-4, generated sequentially)
~/.claude/skills/imagen-generator/scripts/imagen-generate.sh "neural network visualization" --count 2

# Resolution: 512, 1K (default), 2K, 4K
~/.claude/skills/imagen-generator/scripts/imagen-generate.sh "landscape photo" --resolution 2K

# Custom output filename
~/.claude/skills/imagen-generator/scripts/imagen-generate.sh "logo design" --output my-logo

# Override model (default: gemini-3-pro-image-preview)
~/.claude/skills/imagen-generator/scripts/imagen-generate.sh "photo" --model gemini-3.1-flash-image-preview

# Combine options
~/.claude/skills/imagen-generator/scripts/imagen-generate.sh "B200 GPU rack in datacenter, photorealistic" --aspect 16:9 --resolution 2K --count 2
```

### Output

- Images saved to `$CC_PAGES_WEB_ROOT/assets/imagen/` as PNG
- Returns the public URL: `$CC_PAGES_URL_PREFIX/assets/imagen/{filename}.png`
- When called from Discord context, send the URL using `send-to-discord.sh --plain`

### Workflow for Discord

```bash
# 1. Generate image
URL=$(~/.claude/skills/imagen-generator/scripts/imagen-generate.sh "your prompt" --aspect 16:9)

# 2. Send to Discord
~/.claude/scripts/send-to-discord.sh --plain "$URL"
```

## Model

- **`gemini-3-pro-image-preview`** (default) — Gemini 3 Pro Image, best quality, supports text+image generation
- **`gemini-3.1-flash-image-preview`** — Nano Banana 2, faster, good price-performance

## Supported Aspect Ratios

`1:1`, `3:4`, `4:3`, `16:9`, `9:16`, `3:2`, `2:3`, `4:5`, `5:4`, `21:9`, `4:1`, `1:4`, `8:1`, `1:8`

## Supported Resolutions

`512`, `1K`, `2K`, `4K`

## Prompt Tips

- Be specific and descriptive: "a red sports car on a mountain road at sunset, photorealistic" > "car"
- Supports Chinese prompts natively (simplified & traditional)
- For technical diagrams, add style keywords: "technical illustration", "blueprint style", "infographic"
- Gemini models can also generate text within images (e.g., signs, labels, infographics)

## Prerequisites

- gcloud CLI with valid credentials
- Vertex AI API enabled on the GCP project
- CC Pages (GCS-backed via `$CC_PAGES_WEB_ROOT`)

## Files

```
~/.claude/skills/imagen-generator/scripts/
└── imagen-generate.sh                  # Image generation script

$CC_PAGES_WEB_ROOT/assets/imagen/      # Generated images (web-accessible)
```
