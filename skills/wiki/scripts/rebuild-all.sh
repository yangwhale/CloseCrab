#!/usr/bin/env bash
# rebuild-all.sh — One-click rebuild index + graph + search + sync.
#
# Usage:
#   bash rebuild-all.sh                  # full rebuild + sync
#   bash rebuild-all.sh --no-sync        # rebuild only, skip GCS sync
#   bash rebuild-all.sh --fix            # fix broken links first, then rebuild
#   bash rebuild-all.sh --incremental    # only rebuild changed pages (default)
#   bash rebuild-all.sh --full           # force full rebuild (ignore manifest)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WIKI_REPO="${WIKI_REPO:-$HOME/my-wiki}"
NO_SYNC=false
DO_FIX=false
INCREMENTAL=true

for arg in "$@"; do
    case "$arg" in
        --no-sync) NO_SYNC=true ;;
        --fix) DO_FIX=true ;;
        --incremental) INCREMENTAL=true ;;
        --full) INCREMENTAL=false ;;
    esac
done

echo "=== CC Wiki Rebuild ==="

# Check for changes if incremental
if $INCREMENTAL; then
    CHANGED=$(python3 -c "
import json, os, sys
sys.path.insert(0, '$SCRIPT_DIR')
from wiki_utils import WIKI_REPO, SKIP_FILES, compute_file_hash, load_manifest
manifest = load_manifest()
pages = manifest.get('pages', {})
wiki_dir = WIKI_REPO / 'wiki'
changed = 0
if not pages:
    print('-1')  # No manifest, do full rebuild
    sys.exit(0)
for f in wiki_dir.rglob('*.html'):
    rel = str(f.relative_to(wiki_dir))
    if f.name in SKIP_FILES or rel.startswith('.') or rel.startswith('_'):
        continue
    sha = compute_file_hash(f)
    old = pages.get(rel, {}).get('sha256', '')
    if sha != old:
        changed += 1
# Check for deleted pages
for rel in pages:
    if not (wiki_dir / rel).exists():
        changed += 1
print(changed)
" 2>/dev/null || echo "-1")

    if [ "$CHANGED" = "0" ]; then
        echo "  No changes detected, skipping rebuild"
        echo "=== Done (no changes) ==="
        exit 0
    elif [ "$CHANGED" != "-1" ]; then
        echo "  Detected $CHANGED changed/new/deleted pages"
    fi
fi

# Optional: fix issues first
if $DO_FIX; then
    echo ""
    echo "[1/9] Fixing broken links..."
    python3 "$SCRIPT_DIR/fix-broken-links.py"
else
    echo ""
    echo "[skip] --fix not specified, skipping auto-fix"
fi

echo ""
echo "[2/9] Rebuilding index..."
python3 "$SCRIPT_DIR/rebuild-index.py"

echo ""
echo "[3/9] Rebuilding graph (+ auto backlinks)..."
python3 "$SCRIPT_DIR/rebuild-graph.py"

echo ""
echo "[4/9] Rebuilding Pagefind search..."
bash "$SCRIPT_DIR/rebuild-search.sh"

echo ""
echo "[5/9] Building query search index..."
python3 "$SCRIPT_DIR/build-search-index.py"

echo ""
echo "[6/9] Updating compile manifest..."
python3 "$SCRIPT_DIR/update-manifest.py"

echo ""
echo "[7/9] Building health dashboard..."
python3 "$SCRIPT_DIR/rebuild-health.py" 2>/dev/null || echo "  (rebuild-health.py not found, skipping)"

if $NO_SYNC; then
    echo ""
    echo "[skip] --no-sync specified, skipping GCS sync"
else
    echo ""
    echo "[8/9] Syncing to GCS..."
    python3 "$SCRIPT_DIR/sync-to-gcs.py"

    echo ""
    echo "[9/9] Uploading wiki-data via gsutil..."
    gsutil -q cp "$WIKI_REPO/wiki-data/graph.json" gs://chris-pgp-host-asia/cc-pages/wiki-data/graph.json 2>/dev/null || true
    gsutil -q cp "$WIKI_REPO/wiki-data/log.json" gs://chris-pgp-host-asia/cc-pages/wiki-data/log.json 2>/dev/null || true
fi

echo ""
echo "=== Done ==="
echo "Wiki: https://cc.higcp.com/wiki/index.html"
