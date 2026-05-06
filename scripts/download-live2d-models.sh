#!/usr/bin/env bash
# Download Live2D model assets from CubismWebSamples (4-r.7 tag, Cubism 4 compatible)
# Usage: ./scripts/download-live2d-models.sh [model_name]
#   model_name: Natori (default), Haru, Mark, Rice, Mao, Wanko
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE_URL="https://raw.githubusercontent.com/Live2D/CubismWebSamples/4-r.7/Samples/Resources"
MODEL="${1:-Natori}"
DEST="$REPO_ROOT/assets/live2d/$(echo "$MODEL" | tr '[:upper:]' '[:lower:]')"

echo "=== Live2D Model Downloader ==="
echo "Model: $MODEL"
echo "Destination: $DEST"
echo ""

# Step 1: Download model3.json first to discover all referenced files
mkdir -p "$DEST"
MODEL_JSON="$MODEL.model3.json"
echo "Downloading $MODEL_JSON..."
curl -sfL "$BASE_URL/$MODEL/$MODEL_JSON" -o "$DEST/$MODEL_JSON"

# Step 2: Parse model3.json to find all referenced files
FILES=$(python3 -c "
import json, sys
m = json.load(open('$DEST/$MODEL_JSON'))
refs = m.get('FileReferences', {})

# Moc
print(refs.get('Moc', ''))

# Physics, Pose, DisplayInfo
for key in ['Physics', 'Pose', 'DisplayInfo']:
    v = refs.get(key, '')
    if v: print(v)

# Expressions
for exp in refs.get('Expressions', []):
    print(exp['File'])

# Motions (all groups)
for group_name, motions in refs.get('Motions', {}).items():
    for m in motions:
        print(m['File'])

# Textures
for t in refs.get('Textures', []):
    print(t)
")

# Step 3: Download each file
TOTAL=0
FAILED=0
for f in $FILES; do
    if [ -z "$f" ]; then continue; fi
    dir=$(dirname "$f")
    mkdir -p "$DEST/$dir"
    if curl -sfL "$BASE_URL/$MODEL/$f" -o "$DEST/$f"; then
        size=$(wc -c < "$DEST/$f")
        printf "  ✓ %-45s %s\n" "$f" "$(numfmt --to=iec-i --suffix=B $size 2>/dev/null || echo "${size}B")"
        TOTAL=$((TOTAL + 1))
    else
        printf "  ✗ %-45s FAILED\n" "$f"
        FAILED=$((FAILED + 1))
    fi
done

echo ""
echo "Done: $TOTAL files downloaded, $FAILED failed"
echo "Total size: $(du -sh "$DEST" | cut -f1)"

# Step 4: Verify moc3 version
MOC_FILE="$DEST/$(python3 -c "import json; print(json.load(open('$DEST/$MODEL_JSON')).get('FileReferences',{}).get('Moc',''))")"
if [ -f "$MOC_FILE" ]; then
    VERSION=$(od -A n -t x1 -j 4 -N 1 "$MOC_FILE" | tr -d ' ')
    case "$VERSION" in
        01) echo "moc3 version: 1 (Cubism 3/4) ✓" ;;
        03) echo "moc3 version: 3 (Cubism 4.2) ✓" ;;
        06) echo "moc3 version: 6 (Cubism 5) ✗ — incompatible with pixi-live2d-display 0.4.0!" ;;
        *)  echo "moc3 version: unknown ($VERSION)" ;;
    esac
fi
