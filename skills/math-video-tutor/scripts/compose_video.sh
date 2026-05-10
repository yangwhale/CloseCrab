#!/bin/bash
# Compose a video from slide PNGs + narration audio.
#
# Usage: compose_video.sh <slide_dir> <audio_file> <output.mp4>
#
# Slide duration allocation:
#   - Slide 1 (title):   3 seconds
#   - Slide N-1 (answer): 5 seconds
#   - Slide N (summary): 20 seconds
#   - Middle slides:     remaining time split evenly
set -e

SLIDE_DIR="$1"
AUDIO_FILE="$2"
OUTPUT="$3"

if [ -z "$SLIDE_DIR" ] || [ -z "$AUDIO_FILE" ] || [ -z "$OUTPUT" ]; then
  echo "Usage: compose_video.sh <slide_dir> <audio_file> <output.mp4>"
  exit 1
fi

# Get audio duration
DURATION=$(ffprobe -v error -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 "$AUDIO_FILE")

# Count slides
TOTAL_SLIDES=$(ls "$SLIDE_DIR"/slide_*.png 2>/dev/null | wc -l)
if [ "$TOTAL_SLIDES" -lt 3 ]; then
  echo "ERROR: need at least 3 slides, found $TOTAL_SLIDES"
  exit 1
fi

# Calculate durations
TITLE_DUR=3
ANSWER_DUR=5
SUMMARY_DUR=20
MIDDLE=$((TOTAL_SLIDES - 3))
REMAINING=$(echo "$DURATION - $TITLE_DUR - $ANSWER_DUR - $SUMMARY_DUR" | bc)
PER_SLIDE=$(echo "scale=1; $REMAINING / $MIDDLE" | bc)

# Fallback if audio too short
if (( $(echo "$PER_SLIDE <= 0" | bc -l) )); then
  PER_SLIDE=$(echo "scale=1; $DURATION / $TOTAL_SLIDES" | bc)
  TITLE_DUR=$PER_SLIDE
  ANSWER_DUR=$PER_SLIDE
  SUMMARY_DUR=$PER_SLIDE
fi

# Build concat file
CONCAT=$(mktemp /tmp/concat_XXXXXX.txt)
for i in $(seq 1 "$TOTAL_SLIDES"); do
  P=$(printf '%02d' "$i")
  echo "file '${SLIDE_DIR}/slide_${P}.png'" >> "$CONCAT"
  if [ "$i" -eq 1 ]; then
    echo "duration $TITLE_DUR" >> "$CONCAT"
  elif [ "$i" -eq $((TOTAL_SLIDES - 1)) ]; then
    echo "duration $ANSWER_DUR" >> "$CONCAT"
  elif [ "$i" -eq "$TOTAL_SLIDES" ]; then
    echo "duration $SUMMARY_DUR" >> "$CONCAT"
  else
    echo "duration $PER_SLIDE" >> "$CONCAT"
  fi
done
# Repeat last frame (ffmpeg concat requirement)
echo "file '${SLIDE_DIR}/slide_$(printf '%02d' "$TOTAL_SLIDES").png'" >> "$CONCAT"

ffmpeg -y -f concat -safe 0 -i "$CONCAT" \
  -i "$AUDIO_FILE" \
  -c:v libx264 -preset medium -crf 23 \
  -c:a aac -b:a 128k \
  -pix_fmt yuv420p \
  -vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2" \
  -shortest -movflags +faststart \
  "$OUTPUT" 2>/dev/null

rm -f "$CONCAT"

SIZE=$(ls -lh "$OUTPUT" | awk '{print $5}')
echo "Video: $OUTPUT ($SIZE, ${DURATION}s)"
