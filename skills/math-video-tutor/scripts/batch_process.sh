#!/bin/bash
# Full pipeline: slides JSON → HTML → screenshots → TTS → video → upload
#
# Usage: batch_process.sh <workdir> [start] [end]
#
# Directory structure expected:
#   workdir/
#     content/q{N}_slides.json   — slide content (from Claude)
#     content/q{N}_narration.txt — narration script (from Claude)
#
# Output:
#   workdir/slides/q{N}/slide_XX.png  — slide screenshots
#   workdir/audio/q{N}_narration.ogg  — TTS audio
#   workdir/videos/q{NN}_explanation.mp4 — final videos
set -e

WORKDIR="${1:-.}"
START="${2:-1}"
END="${3:-25}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTENT_DIR="$WORKDIR/content"
SLIDES_DIR="$WORKDIR/slides"
AUDIO_DIR="$WORKDIR/audio"
VIDEO_DIR="$WORKDIR/videos"
mkdir -p "$SLIDES_DIR" "$AUDIO_DIR" "$VIDEO_DIR"

echo "=== Math Video Pipeline ==="
echo "Working dir: $WORKDIR"
echo "Range: Q${START} - Q${END}"
echo ""

# --- Phase 1: Generate HTML slides from JSON ---
echo "--- Phase 1: Generating slide HTML ---"
for q in $(seq "$START" "$END"); do
  JSON="$CONTENT_DIR/q${q}_slides.json"
  HTML="$SLIDES_DIR/q${q}_slides.html"
  if [ ! -f "$JSON" ]; then
    echo "[Q${q}] SKIP: no JSON"
    continue
  fi
  python3 "$SCRIPT_DIR/gen_slides.py" "$JSON" "$HTML"
done

# --- Phase 2: Screenshot slides (requires Playwright) ---
echo ""
echo "--- Phase 2: Screenshotting slides ---"
for q in $(seq "$START" "$END"); do
  HTML="$SLIDES_DIR/q${q}_slides.html"
  OUT="$SLIDES_DIR/q${q}"
  if [ ! -f "$HTML" ]; then
    continue
  fi
  if [ -d "$OUT" ] && [ "$(ls "$OUT"/slide_*.png 2>/dev/null | wc -l)" -ge 3 ]; then
    echo "[Q${q}] Screenshots exist, skipping"
    continue
  fi
  node "$SCRIPT_DIR/screenshot_slides.js" "$HTML" "$OUT"
done

# --- Phase 3: TTS generation (parallel, 4 at a time) ---
echo ""
echo "--- Phase 3: Generating TTS audio ---"
TTS_PIDS=()
tts_one() {
  local q=$1
  local NARRATION_FILE="$CONTENT_DIR/q${q}_narration.txt"
  local AUDIO_FILE="$AUDIO_DIR/q${q}_narration.ogg"
  if [ -f "$AUDIO_FILE" ]; then
    echo "[Q${q}] Audio exists, skipping"
    return 0
  fi
  if [ ! -f "$NARRATION_FILE" ]; then
    echo "[Q${q}] SKIP: no narration"
    return 1
  fi
  local NARRATION
  NARRATION=$(cat "$NARRATION_FILE" | tr '\n' ' ')
  echo "[Q${q}] Generating TTS..."
  local OGG
  OGG=$(~/.claude/skills/tts-generator/scripts/tts-generate.py \
    --voice charon "[温和亲切，像老师给小学生讲题] $NARRATION" 2>/dev/null)
  if [ -f "$OGG" ]; then
    cp "$OGG" "$AUDIO_FILE"
    rm -f "$OGG"
    local DUR
    DUR=$(ffprobe -v error -show_entries format=duration \
      -of default=noprint_wrappers=1:nokey=1 "$AUDIO_FILE" 2>/dev/null)
    echo "[Q${q}] TTS done: ${DUR}s"
  else
    echo "[Q${q}] TTS FAILED"
    return 1
  fi
}
for q in $(seq "$START" "$END"); do
  tts_one "$q" &
  TTS_PIDS+=($!)
  if [ ${#TTS_PIDS[@]} -ge 4 ]; then
    wait "${TTS_PIDS[0]}"
    TTS_PIDS=("${TTS_PIDS[@]:1}")
  fi
done
for pid in "${TTS_PIDS[@]}"; do wait "$pid"; done

# --- Phase 4: Compose videos (parallel, 4 at a time) ---
echo ""
echo "--- Phase 4: Composing videos ---"
VID_PIDS=()
for q in $(seq "$START" "$END"); do
  SLIDE_D="$SLIDES_DIR/q${q}"
  AUDIO_F="$AUDIO_DIR/q${q}_narration.ogg"
  PADDED=$(printf '%02d' "$q")
  VIDEO_F="$VIDEO_DIR/q${PADDED}_explanation.mp4"
  if [ ! -d "$SLIDE_D" ] || [ ! -f "$AUDIO_F" ]; then
    continue
  fi
  if [ -f "$VIDEO_F" ]; then
    echo "[Q${q}] Video exists, skipping"
    continue
  fi
  bash "$SCRIPT_DIR/compose_video.sh" "$SLIDE_D" "$AUDIO_F" "$VIDEO_F" &
  VID_PIDS+=($!)
  if [ ${#VID_PIDS[@]} -ge 4 ]; then
    wait "${VID_PIDS[0]}"
    VID_PIDS=("${VID_PIDS[@]:1}")
  fi
done
for pid in "${VID_PIDS[@]}"; do wait "$pid"; done

echo ""
echo "=== Pipeline complete ==="
ls -lhS "$VIDEO_DIR"/q*_explanation.mp4 2>/dev/null
