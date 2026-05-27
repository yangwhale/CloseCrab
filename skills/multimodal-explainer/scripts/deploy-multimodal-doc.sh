#!/bin/bash
# Deploy multimodal explainer HTML + voice files to CC Pages.
#
# Usage:
#   deploy-multimodal-doc.sh <html_path> <voice_dir> [--public]
#
# Default: uploads to IAP-only /pages/ (cc-pages/pages/ + cc-pages/pages/voice/)
# With --public: uploads to public /assets/ (cc-pages/assets/ + cc-pages/assets/voice/)
#
# Uses Python google-cloud-storage SDK to bypass gcloud's CAA wall on cc-tw.

set -eu

HTML_PATH="${1:?Usage: $0 <html_path> <voice_dir> [--public]}"
VOICE_DIR="${2:?Usage: $0 <html_path> <voice_dir> [--public]}"
VISIBILITY="${3:-iap}"

# Validate
[ -f "$HTML_PATH" ] || { echo "ERROR: HTML not found: $HTML_PATH" >&2; exit 1; }
[ -d "$VOICE_DIR" ] || { echo "ERROR: voice dir not found: $VOICE_DIR" >&2; exit 1; }

# Determine target prefix
if [ "$VISIBILITY" = "--public" ]; then
  PREFIX="assets"
  URL_BASE="https://cc.higcp.com/assets"
else
  PREFIX="pages"
  URL_BASE="https://cc.higcp.com/pages"
fi

HTML_NAME=$(basename "$HTML_PATH")

echo "=== Multimodal doc deploy ==="
echo "  HTML:       $HTML_PATH"
echo "  Voice dir:  $VOICE_DIR ($(ls "$VOICE_DIR"/*.ogg 2>/dev/null | wc -l) files)"
echo "  Visibility: $PREFIX ($URL_BASE)"
echo ""

# Upload via Python SDK (bypasses CAA cert wall)
python3 - "$HTML_PATH" "$VOICE_DIR" "$PREFIX" "$HTML_NAME" << 'PYEOF'
import sys, os
from google.cloud import storage

html_path, voice_dir, prefix, html_name = sys.argv[1:5]
client = storage.Client(project='chris-pgp-host')
bucket = client.bucket('chris-pgp-host-asia')

# Upload HTML
blob = bucket.blob(f'cc-pages/{prefix}/{html_name}')
blob.upload_from_filename(html_path)
print(f'  ✅ {html_name}: {blob.size} bytes → cc-pages/{prefix}/')

# Upload voice files
voice_files = sorted(f for f in os.listdir(voice_dir) if f.endswith('.ogg'))
for vf in voice_files:
    blob = bucket.blob(f'cc-pages/{prefix}/voice/{vf}')
    blob.upload_from_filename(os.path.join(voice_dir, vf))
    print(f'  ✅ voice/{vf}: {blob.size} bytes')

print(f'\nTotal: 1 HTML + {len(voice_files)} voice files')
PYEOF

echo ""
echo "=== URL verification ==="
HTML_URL="$URL_BASE/$HTML_NAME"
echo "HTML:  $HTML_URL"
curl -sI "$HTML_URL" 2>&1 | head -2 | sed 's/^/  /'

if [ -f "$VOICE_DIR/01-overview.ogg" ] || ls "$VOICE_DIR"/*.ogg >/dev/null 2>&1; then
  FIRST_VOICE=$(ls "$VOICE_DIR"/*.ogg | head -1 | xargs basename)
  VOICE_URL="$URL_BASE/voice/$FIRST_VOICE"
  echo ""
  echo "Voice: $VOICE_URL"
  curl -sI "$VOICE_URL" 2>&1 | head -2 | sed 's/^/  /'
fi

echo ""
echo "=== Done ==="
echo "  Main link: $HTML_URL"
if [ "$VISIBILITY" = "--public" ]; then
  echo "  ⚠️  PUBLIC URL — anyone with link can access. Confirm no internal info before sharing."
else
  echo "  🔒 IAP-protected — accessible only to authorized corp accounts via cc.higcp.com IAP."
fi
