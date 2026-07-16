#!/usr/bin/env bash
# render.sh — convert a deck/doc to PNG pages so you can LOOK at the result.
# The render-verify loop is the whole point: never trust a slide you haven't seen.
#
# Usage:
#   render.sh deck.pptx              # render ALL pages
#   render.sh deck.pptx 3 5          # render pages 3..5 only
#   render.sh file.pdf 1 1 160       # a PDF, page 1, at 160 dpi
#
# Output: /tmp/render_out/p-<n>.png  (low dpi ~80 is enough to judge layout;
# bump dpi to read small text). Then Read those PNGs.
set -e
SRC="$1"; FROM="${2:-}"; TO="${3:-}"; DPI="${4:-90}"
OUT=/tmp/render_out; mkdir -p "$OUT"; rm -f "$OUT"/p-*.png

ext="${SRC##*.}"
if [ "$ext" = "pdf" ]; then
  PDF="$SRC"
else
  # convert pptx/docx -> pdf via libreoffice (headless)
  timeout 240 libreoffice --headless --convert-to pdf --outdir "$OUT" "$SRC" >/dev/null 2>&1
  PDF="$OUT/$(basename "${SRC%.*}").pdf"
fi

RANGE=()
[ -n "$FROM" ] && RANGE+=(-f "$FROM")
[ -n "$TO" ]   && RANGE+=(-l "$TO")
pdftoppm -png -r "$DPI" "${RANGE[@]}" "$PDF" "$OUT/p" >/dev/null 2>&1
echo "rendered -> $OUT/"
ls "$OUT"/p-*.png
