---
name: imagen-generator
description: Generate or edit images using Gemini 3 Pro Image (Nano Banana) on Vertex AI. Use for text-to-image ("生成图片", "画一张", "generate image", "帮我画", "create an image", "图片生成") and image-to-image editing ("修图", "P图", "改图", "edit image", "在这张图上加 ...", "基于这张图 ...", "保持风格生成 ...").
---

# Gemini Image Generator (Nano Banana)

Generate or edit images using Gemini 3 Pro Image (gemini-3-pro-image-preview) on Vertex AI, save to CC Pages, and share.

Two modes:
- **Text-to-image** — pure prompt, model invents from scratch
- **Image-to-image (修图)** — pass `--image <path>` one or more times; model treats them as visual baselines and the text prompt as the editing instruction (preserve / add / remove / restyle)

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

### Image-to-Image (修图)

Pass `--image <path>` to provide a visual baseline. The text prompt becomes the editing instruction (what to keep, what to add, what to change). Repeat `--image` for multiple references — first one is the primary baseline.

```bash
# 在一张图上加元素，保持原风格
~/.claude/skills/imagen-generator/scripts/imagen-generate.sh \
  "Keep the cyberpunk style unchanged. Add a glowing holographic dashboard in the upper-left corner showing 4 floating cards." \
  --image ~/CloseCrab/crab-with-claude-code-inside.png \
  --aspect 4:5 --resolution 2K --output poster-with-cards

# 多参考图：第一张是主 baseline，后面是风格/元素参考
~/.claude/skills/imagen-generator/scripts/imagen-generate.sh \
  "Combine subject of first image with the lighting and color palette of second image" \
  --image subject.png --image style_ref.png \
  --aspect 16:9 --resolution 2K

# 改 aspect 重构图（recompose）— 模型会保留主体，重新构图填满目标比例
~/.claude/skills/imagen-generator/scripts/imagen-generate.sh \
  "Same subject, recompose for vertical poster, keep cinematic mood" \
  --image hero.png --aspect 9:16 --resolution 2K
```

**Prompt 写法建议（修图）**：
- 明确写"KEEP UNCHANGED:"列出要保留的元素（风格、材质、灯光、构图）
- 明确写"ADD:" / "CHANGE:" / "REMOVE:" 列出要修改的元素
- 最后强调"this must still look like the same artwork"，否则模型可能跑偏成纯文生图
- 改 aspect 时加"recompose for ... aspect, keep [subject] as dominant element"

支持的图片格式：`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`

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

- `google-genai` Python SDK (`pip install google-genai`)
- Application Default Credentials configured (`gcloud auth application-default login`)
- Vertex AI API enabled on the GCP project
- CC Pages (GCS-backed via `$CC_PAGES_WEB_ROOT`)

## Files

```
~/.claude/skills/imagen-generator/scripts/
├── imagen-generate.sh                  # Entry point (exec wrapper)
└── imagen-generate.py                  # Core logic (google-genai SDK)

$CC_PAGES_WEB_ROOT/assets/imagen/      # Generated images (web-accessible)
```
