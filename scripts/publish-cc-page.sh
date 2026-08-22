#!/bin/bash
# publish-cc-page.sh — Verify URLs in an HTML file, then upload to CC Pages
# (gs://chris-pgp-host-asia/cc-pages/) and confirm the publicly served version
# matches the local file. Replaces ad-hoc `gsutil cp` so URL verification
# becomes part of the publish workflow, not an afterthought.
#
# Usage:
#   publish-cc-page.sh <local-html-path> [--to pages|assets|both] [--force] [--no-favicon]
#
# Defaults: --to both
# --force:      publish even if URL verification reports failures (use sparingly)
# --no-favicon: don't inject the CloseCrab crab favicon when it's missing
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
NO_FAVICON=0

while [ $# -gt 0 ]; do
  case "$1" in
    --to)    TO="${2:-both}"; shift 2 ;;
    --force)      FORCE=1; shift ;;
    --no-favicon) NO_FAVICON=1; shift ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)      echo "Unknown flag: $1" >&2; exit 2 ;;
    *)       HTML="$1"; shift ;;
  esac
done

[ -n "$HTML" ]  || { echo "Usage: $0 <html-file> [--to pages|assets|both] [--force] [--no-favicon]" >&2; exit 2; }
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

# ────────────────── Step 0: favicon ──────────────────
# 每个页面都该带 CloseCrab 的 🦀。缺了就当场补进源文件（不是补进临时副本）——
# 后面 Step 2/3 要拿本地字节数跟远端比，改副本会让那两步永远对不上。
# 内联 SVG 而不是图片文件：图标由浏览器用本机 emoji 字体现画，
# Mac 上是 Apple 那只红螃蟹。烤成 PNG 等于把某一家的字形钉死在所有平台。
# 规范见 skills/page-style/SKILL.md。
FAVICON_LINK="<link rel=\"icon\" href=\"data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🦀</text></svg>\">"
if [ "$NO_FAVICON" -eq 1 ]; then
  echo "━━━━ Step 0: favicon (skipped, --no-favicon) ━━━━"
elif grep -qi 'rel="icon"\|rel='"'"'icon'"'"'' "$HTML"; then
  echo "━━━━ Step 0: favicon ━━━━"
  echo "  ✓ 已有 favicon，不动"
else
  echo "━━━━ Step 0: favicon ━━━━"
  if grep -qi '</title>' "$HTML"; then
    python3 - "$HTML" "$FAVICON_LINK" <<'PY'
import re, sys
path, link = sys.argv[1], sys.argv[2]
s = open(path, encoding="utf-8").read()
m = re.search(r'</title\s*>', s, re.I)          # 插在 </title> 之后
i = m.end()                                     # 永远自成一行，别跟 </title> 挤在一起
open(path, "w", encoding="utf-8").write(s[:i] + "\n" + link + "\n" + s[i:].lstrip("\n"))
PY
    echo "  ✚ 缺 favicon，已补进 $HTML（<title> 之后）"
  else
    echo "  ⚠ 缺 favicon，且找不到 </title> —— 没动文件，请手动加："
    echo "    $FAVICON_LINK"
  fi
fi
echo ""

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
