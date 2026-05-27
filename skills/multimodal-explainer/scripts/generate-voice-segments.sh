#!/bin/bash
# Parallel-generate Gemini TTS voice segments from .txt files.
#
# Usage:
#   generate-voice-segments.sh <input_dir> <output_dir> [--voice orus]
#
# Reads all *.txt files in input_dir, runs TTS in parallel, writes *.ogg to output_dir.
# Auto-detects failed segments (file < 50KB) and lists them for manual splitting.

set -eu

INPUT_DIR="${1:?Usage: $0 <input_dir> <output_dir> [--voice <name>]}"
OUTPUT_DIR="${2:?Usage: $0 <input_dir> <output_dir> [--voice <name>]}"
VOICE="${4:-orus}"  # default voice

# Validate
[ -d "$INPUT_DIR" ] || { echo "ERROR: input dir not found: $INPUT_DIR" >&2; exit 1; }
mkdir -p "$OUTPUT_DIR"

TTS_BIN="$HOME/CloseCrab/skills/tts-generator/scripts/tts-generate.py"
[ -x "$TTS_BIN" ] || { echo "ERROR: tts-generate.py not found at $TTS_BIN" >&2; exit 1; }

echo "=== Generating TTS segments ==="
echo "  Input:  $INPUT_DIR"
echo "  Output: $OUTPUT_DIR"
echo "  Voice:  $VOICE"
echo ""

# Generate in parallel
TXT_FILES=("$INPUT_DIR"/*.txt)
[ ${#TXT_FILES[@]} -gt 0 ] && [ -e "${TXT_FILES[0]}" ] || { echo "ERROR: no .txt files in $INPUT_DIR" >&2; exit 1; }

for txt_path in "${TXT_FILES[@]}"; do
  base=$(basename "$txt_path" .txt)
  (
    text=$(cat "$txt_path")
    out=$("$TTS_BIN" "$text" --voice "$VOICE" 2>/dev/null | tail -1)
    if [ -f "$out" ]; then
      cp "$out" "$OUTPUT_DIR/$base.ogg"
      size=$(stat -c%s "$OUTPUT_DIR/$base.ogg")
      echo "  ✅ $base.ogg ($size bytes)"
    else
      echo "  ❌ $base.ogg — TTS returned no file"
    fi
  ) &
done
wait

echo ""
echo "=== Validating segments ==="

# Check for failed segments (< 50KB indicates Gemini TTS silent failure)
FAILED=()
for ogg in "$OUTPUT_DIR"/*.ogg; do
  [ -f "$ogg" ] || continue
  size=$(stat -c%s "$ogg")
  if [ "$size" -lt 51200 ]; then
    FAILED+=("$(basename "$ogg")")
    echo "  ⚠️  FAILED: $(basename "$ogg") ($size bytes — likely TTS silent failure due to length)"
  fi
done

if [ ${#FAILED[@]} -gt 0 ]; then
  echo ""
  echo "❌ ${#FAILED[@]} segment(s) failed."
  echo "   Fix: split the corresponding .txt file in half at a natural breakpoint,"
  echo "        rename as <orig>a.txt + <orig>b.txt, then re-run this script."
  echo "   See references/audio-segmentation-guide.md for split guidance."
  exit 1
fi

echo ""
echo "=== Durations ==="
total_sec=0
for ogg in "$OUTPUT_DIR"/*.ogg; do
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$ogg" 2>/dev/null | cut -d. -f1)
  mins=$((dur / 60))
  secs=$((dur % 60))
  printf "  %-40s %dm%02ds\n" "$(basename "$ogg")" "$mins" "$secs"
  total_sec=$((total_sec + dur))
done
total_min=$((total_sec / 60))
total_rem=$((total_sec % 60))
echo ""
echo "  TOTAL: ${total_min}m${total_rem}s ($total_sec seconds across $(ls "$OUTPUT_DIR"/*.ogg | wc -l) files)"
