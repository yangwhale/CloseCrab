#!/bin/bash
# CC Pages 增量备份到 GitHub private repo
# 用法: backup-cc-pages.sh [--dry-run]

set -euo pipefail

MOUNT="/home/chrisya/cc-pages-new"
BACKUP_DIR="$HOME/my-private/cc-pages-backup"
REPO_DIR="$HOME/my-private"
DRY_RUN=""

[[ "${1:-}" == "--dry-run" ]] && DRY_RUN="--dry-run" && echo "[DRY RUN]"

# 1. Check mount
if ! mountpoint -q "$MOUNT" 2>/dev/null; then
  echo "ERROR: gcsfuse mount not active at $MOUNT"
  echo "Run: gcsfuse --implicit-dirs --only-dir=cc-pages --uid=$(id -u) --gid=$(id -g) --file-mode=0666 --dir-mode=0777 chris-pgp-host-asia $MOUNT"
  exit 1
fi

# 2. Sync pages/
echo "=== Syncing pages/ ==="
rsync -av $DRY_RUN --delete \
  "$MOUNT/pages/" "$BACKUP_DIR/pages/" 2>&1 | tail -3

# 3. Sync assets/ (exclude large binary files)
echo -e "\n=== Syncing assets/ ==="
rsync -av $DRY_RUN --delete \
  --exclude='*.pb' \
  --exclude='*.xplane.pb' \
  --exclude='*.trace.json' \
  --exclude='*.trace.json.gz' \
  --exclude='*.mp4' \
  --exclude='*.m4a' \
  --exclude='*.webm' \
  --exclude='*.zip' \
  --exclude='*.wav' \
  --exclude='tb-traces/' \
  "$MOUNT/assets/" "$BACKUP_DIR/assets/" 2>&1 | tail -3

# 4. Size report
echo -e "\n=== Backup size ==="
pages_count=$(find "$BACKUP_DIR/pages/" -type f | wc -l)
assets_count=$(find "$BACKUP_DIR/assets/" -type f | wc -l)
total_size=$(du -sh "$BACKUP_DIR" | cut -f1)
echo "pages/: $pages_count files"
echo "assets/: $assets_count files"
echo "Total: $total_size"

# 5. Git commit & push
if [[ -z "$DRY_RUN" ]]; then
  cd "$REPO_DIR"
  git add cc-pages-backup/
  
  if git diff --cached --quiet; then
    echo -e "\n=== No changes to commit ==="
  else
    changed=$(git diff --cached --stat | tail -1)
    timestamp=$(TZ='Asia/Hong_Kong' date '+%Y-%m-%d %H:%M HKT')
    git commit -m "backup: CC Pages sync $timestamp

$changed"
    echo -e "\n=== Pushing to GitHub ==="
    git push 2>&1 | tail -3
    echo "=== Done ==="
  fi
else
  echo -e "\n[DRY RUN] Would commit and push changes"
fi
