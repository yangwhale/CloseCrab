#!/usr/bin/env bash
# Veo 3.1 — Text-to-video generation via Google GenAI SDK (Vertex AI)
#
# Usage:
#   veo-generate.sh "prompt" [options]
#
# Options:
#   --aspect RATIO      Aspect ratio: 16:9 (default), 9:16
#   --count N           Number of videos to generate: 1-4 (default: 1)
#   --model TIER        Model tier: standard (default, best quality), fast, lite
#   --duration N        Video duration in seconds: 4, 6, or 8 (default: 8)
#   --resolution RES    Video resolution: 720p (default), 1080p, 4k
#   --negative TEXT     Negative prompt (content to avoid)
#   --output NAME       Custom output filename (without extension)
#   --image PATH        Input image for image-to-video (used as first frame)
#   --last-frame PATH   Last frame image for frame interpolation
#   --ref PATH          Reference image for visual guidance (asset type, up to 3)
#   --no-rewrite        Disable built-in prompt rewriter
#   --poll-interval N   Polling interval in seconds (default: 10)
#   --timeout N         Max wait time in seconds (default: 300)
#
# Output:
#   Prints public URL(s) of generated video(s) to stdout.
#   Videos saved to $CC_PAGES_WEB_ROOT/assets/veo/
#
# Prerequisites:
#   - google-genai Python SDK (pip install google-genai)
#   - Application Default Credentials configured
#   - Vertex AI API enabled on the GCP project
#   - CC Pages (GCS-backed via $CC_PAGES_WEB_ROOT)

set -euo pipefail

exec python3 "$(dirname "$0")/veo-generate.py" "$@"
