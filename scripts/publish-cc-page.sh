#!/bin/bash
# publish-cc-page.sh — Verify URLs in an HTML file, then upload to CC Pages
# (gs://chris-pgp-host-asia/cc-pages/) and confirm the publicly served version
# matches the local file. Replaces ad-hoc `gsutil cp` so URL verification
# becomes part of the publish workflow, not an afterthought.
#
# Usage:
#   publish-cc-page.sh <local-html-path> [--to pages|assets|both] [--force]
#
# Defaults: --to both
# --force: publish even if URL verification reports failures (use sparingly)
#
# Exit codes:
#   0 = published + remote matches local
#   1 = remote size mismatch or HTTP non-2xx after publish
#   2 = bad args / file not found
#   3 = URL verification failed (no --force)

set -u

HTML=""
TO="both"
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --to)    TO="${2:-both}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help)
      sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)      echo "Unknown flag: $1" >&2; exit 2 ;;
    *)       HTML="$1"; shift ;;
  esac
done

[ -n "$HTML" ]  || { echo "Usage: $0 <html-file> [--to pages|assets|both] [--force]" >&2; exit 2; }
[ -f "$HTML" ]  || { echo "ERROR: $HTML not found" >&2; exit 2; }
case "$TO" in pages|assets|both) ;; *) echo "ERROR: --to must be pages|assets|both" >&2; exit 2 ;; esac

BASENAME=$(basename "$HTML")
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERIFIER="$SCRIPT_DIR/verify-page-urls.sh"
BUCKET="gs://chris-pgp-host-asia/cc-pages"
SA="604327164091-compute@developer.gserviceaccount.com"
GSUTIL_ENV=(
  "CLOUDSDK_CORE_ACCOUNT=$SA"
  "CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false"
)

# ────────────────── Step 1: URL verification ──────────────────
echo "━━━━ Step 1: URL verification ━━━━"
if [ -x "$VERIFIER" ]; then
  set +e
  "$VERIFIER" "$HTML"
  V_RC=$?
  set -e
else
  echo "WARN: $VERIFIER not found or not executable — skipping URL verification" >&2
  V_RC=0
fi

if [ "$V_RC" -ne 0 ]; then
  echo ""
  if [ "$FORCE" -eq 1 ]; then
    echo "⚠️  URL verification failed but --force given. Proceeding."
  else
    echo "⛔ URL verification failed. Fix the URLs (suggested ALTERNATIVEs above),"
    echo "   or re-run with --force to publish anyway."
    exit 3
  fi
fi

# ────────────────── Step 2: Upload to GCS ──────────────────
echo ""
echo "━━━━ Step 2: Upload to GCS ($TO) ━━━━"
LOCAL_SIZE=$(stat -c '%s' "$HTML")
echo "Local size: $LOCAL_SIZE bytes"

upload_one() {
  local subdir="$1"
  local dest="$BUCKET/$subdir/$BASENAME"
  echo "  → $dest"
  env "${GSUTIL_ENV[@]}" gsutil \
    -h "Cache-Control:no-cache, max-age=0" \
    -h "Content-Type:text/html; charset=utf-8" \
    cp "$HTML" "$dest" 2>&1 | tail -2
}

case "$TO" in
  pages|both)  upload_one "pages"  ;;
esac
case "$TO" in
  assets|both) upload_one "assets" ;;
esac

# Confirm GCS object size matches local size
echo ""
echo "GCS object check:"
for sub in pages assets; do
  case "$TO" in
    "$sub"|both)
      REMOTE_SIZE=$(env "${GSUTIL_ENV[@]}" gsutil ls -l "$BUCKET/$sub/$BASENAME" 2>/dev/null \
                    | awk '/^ *[0-9]/{print $1}' | head -1)
      if [ "$REMOTE_SIZE" = "$LOCAL_SIZE" ]; then
        printf "  \033[32m✓\033[0m %s/%s  GCS size=%s\n" "$sub" "$BASENAME" "$REMOTE_SIZE"
      else
        printf "  \033[31m✗\033[0m %s/%s  GCS=%s vs local=%s\n" "$sub" "$BASENAME" "$REMOTE_SIZE" "$LOCAL_SIZE"
        FAIL=1
      fi
      ;;
  esac
done

# ────────────────── Step 3: Public access HEAD test ──────────────────
echo ""
echo "━━━━ Step 3: Public-edge access test ━━━━"
FAIL=${FAIL:-0}
case "$TO" in
  assets|both)
    URL="https://cc.higcp.com/assets/$BASENAME"
    STATUS=$(curl -sSo /dev/null -w "%{http_code}" -m 10 "$URL")
    REMOTE_CL=$(curl -sSI "$URL" 2>/dev/null | awk -F': *' 'tolower($1)=="content-length"{print $2}' | tr -d '\r')
    if [[ "$STATUS" == "200" && "$REMOTE_CL" == "$LOCAL_SIZE" ]]; then
      printf "  \033[32m✓\033[0m Public:  %s  (HTTP %s, %s bytes)\n" "$URL" "$STATUS" "$REMOTE_CL"
    else
      printf "  \033[31m✗\033[0m Public:  %s  (HTTP %s, local=%s remote=%s)\n" \
        "$URL" "$STATUS" "$LOCAL_SIZE" "$REMOTE_CL"
      FAIL=1
    fi
    ;;
esac
case "$TO" in
  pages|both)
    URL="https://cc.higcp.com/pages/$BASENAME"
    STATUS=$(curl -sSo /dev/null -w "%{http_code}" -m 10 "$URL")
    if [[ "$STATUS" == "302" ]]; then
      printf "  \033[32m✓\033[0m IAP:     %s  (HTTP 302 → Google login, expected)\n" "$URL"
    else
      printf "  \033[33m⚠\033[0m IAP:     %s  (HTTP %s, expected 302 redirect)\n" "$URL" "$STATUS"
    fi
    ;;
esac

# ────────────────── Final summary ──────────────────
echo ""
echo "━━━━ Done ━━━━"
case "$TO" in
  assets|both) echo "  Public (no IAP):   https://cc.higcp.com/assets/$BASENAME" ;;
esac
case "$TO" in
  pages|both)  echo "  Internal (IAP):    https://cc.higcp.com/pages/$BASENAME" ;;
esac
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
