#!/usr/bin/env bash
# rebuild-search.sh — Build Pagefind search index for CC Wiki
#
# Runs Pagefind CLI to scan wiki/ HTML files and generate the _pagefind/
# search index directory. Also generates search.html page.
#
# Usage: bash rebuild-search.sh [--wiki-dir ~/my-wiki]

set -euo pipefail

WIKI_REPO="${WIKI_REPO:-${HOME}/my-wiki}"
WIKI_DIR="${WIKI_REPO}/wiki"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --wiki-dir) WIKI_REPO="$2"; WIKI_DIR="${WIKI_REPO}/wiki"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ ! -d "$WIKI_DIR" ]]; then
  echo "Error: Wiki directory not found at $WIKI_DIR"
  exit 1
fi

# Generate search.html first (so Pagefind can index it too if needed)
echo "Generating search.html..."
python3 "${SCRIPT_DIR}/rebuild-search-page.py"

# Run Pagefind
echo "Building Pagefind search index..."
npx -y pagefind@latest \
  --site "$WIKI_DIR" \
  --glob "**/*.html" \
  --exclude-selectors "[data-pagefind-ignore]" \
  --output-path "${WIKI_DIR}/_pagefind"

# Count index files
INDEX_COUNT=$(find "${WIKI_DIR}/_pagefind" -type f | wc -l)
echo "Pagefind index built: ${INDEX_COUNT} files in ${WIKI_DIR}/_pagefind/"
