#!/usr/bin/env bash
# Gemini Image — Text-to-image generation via Google GenAI SDK (Vertex AI)
#
# Usage:
#   imagen-generate.sh "prompt" [options]
#
# Options:
#   --aspect RATIO    Aspect ratio: 1:1 (default), 3:4, 4:3, 16:9, 9:16, 3:2, 2:3, 4:5, 5:4, 21:9, 4:1, 1:4, 8:1, 1:8
#   --count N         Number of images to generate: 1-4 (default: 1, sequential calls)
#   --resolution RES  Output resolution: 512, 1K (default), 2K, 4K
#   --output NAME     Custom output filename (without extension)
#   --model MODEL     Model ID (default: gemini-3-pro-image-preview)
#
# Output:
#   Prints public URL(s) of generated image(s) to stdout.
#   Images saved to $CC_PAGES_WEB_ROOT/assets/imagen/
#
# Prerequisites:
#   - google-genai Python SDK (pip install google-genai)
#   - Application Default Credentials configured
#   - Vertex AI API enabled on the GCP project
#   - CC Pages (GCS-backed via $CC_PAGES_WEB_ROOT)

set -euo pipefail

exec python3 "$(dirname "$0")/imagen-generate.py" "$@"
