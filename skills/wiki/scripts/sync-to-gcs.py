#!/usr/bin/env python3
"""sync-to-gcs.py — Sync wiki/ and wiki-data/ to GCS serving directory.

Strategy:
  - gcsfuse mounted → rsync to mount point (fast, no extra copy)
  - gcsfuse NOT mounted → fallback to `gcloud storage rsync` (direct GCS upload)

raw/ is NOT synced (may contain large files, not needed for URL access).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
GCS_ROOT = Path(os.environ.get("CC_PAGES_WEB_ROOT", os.path.expanduser("~/gcs-mount/cc-pages")))
GCS_BUCKET = os.environ.get("CC_PAGES_GCS_BUCKET", "")

SYNC_DIRS = ["wiki", "wiki-data"]

# Extra files to copy into wiki/ (from skill references)
SKILL_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXTRA_FILES = {
    "local-graph.js": SKILL_DIR / "references" / "local-graph.js",
}


def is_gcsfuse_mounted() -> bool:
    """Check if GCS_ROOT is on a gcsfuse mount."""
    try:
        result = subprocess.run(
            ["mountpoint", "-q", str(GCS_ROOT.parent)],
            capture_output=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def sync_via_rsync(src: Path, dst: Path) -> bool:
    """Sync via local rsync to gcsfuse mount."""
    dst.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-a", "--delete", f"{src}/", f"{dst}/"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  rsync error: {result.stderr}", file=sys.stderr)
        return False
    count = sum(1 for f in dst.rglob("*") if f.is_file())
    print(f"  {count} files synced")
    return True


def sync_via_gcloud(src: Path, gcs_dst: str) -> bool:
    """Sync via `gcloud storage rsync` directly to GCS.

    Not gsutil: it authenticates as the default service account from
    `gcloud config get account` rather than ADC, which fails on buckets that
    only granted ADC (access_denied: Account restricted).
    """
    # PROCESS_COUNT=1: 多进程并发时每个 worker 都去取 CAA 客户端证书，
    # 并发下大量失败成 `[SSL] PEM lib`。单进程慢些但几乎无噪声。
    # 失败的对象只是被跳过、命令照样非零退出，所以要重试到真正干净为止。
    env = {**os.environ, "CLOUDSDK_STORAGE_PROCESS_COUNT": "1"}
    cmd = ["gcloud", "storage", "rsync", "-r",
           "--delete-unmatched-destination-objects", f"{src}/", f"{gcs_dst}/"]
    for attempt in range(1, 6):
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode == 0:
            break
        if attempt == 5:
            print(f"  gcloud storage error: {result.stderr}", file=sys.stderr)
            return False
        print(f"  attempt {attempt} had failed objects, retrying...")
    lines = result.stderr.strip().split("\n") if result.stderr else []
    op_lines = [l for l in lines if "Operation completed" in l]
    if op_lines:
        print(f"  {op_lines[-1].strip()}")
    else:
        print(f"  Synced to {gcs_dst}")
    return True


def sync():
    if not WIKI_REPO.exists():
        print(f"Error: Wiki repo not found at {WIKI_REPO}", file=sys.stderr)
        print("Run: bash ~/.claude/skills/wiki/scripts/init-wiki.sh", file=sys.stderr)
        sys.exit(1)

    # Copy extra files into wiki/ before syncing
    wiki_dir = WIKI_REPO / "wiki"
    for filename, src_path in EXTRA_FILES.items():
        if src_path.exists():
            dst = wiki_dir / filename
            shutil.copy2(src_path, dst)
            print(f"Copied {filename} → {dst}")

    use_gcsfuse = is_gcsfuse_mounted()
    if use_gcsfuse:
        print(f"Using gcsfuse mount: {GCS_ROOT}")
    else:
        print(f"gcsfuse not mounted, using gcloud storage → {GCS_BUCKET}")

    for dir_name in SYNC_DIRS:
        src = WIKI_REPO / dir_name
        if not src.exists():
            print(f"Skip: {src} does not exist")
            continue

        print(f"Syncing {dir_name}/")
        if use_gcsfuse:
            sync_via_rsync(src, GCS_ROOT / dir_name)
        else:
            sync_via_gcloud(src, f"{GCS_BUCKET}/{dir_name}")

    print("Sync complete.")
    url_prefix = os.environ.get("CC_PAGES_URL_PREFIX", "")
    print(f"Wiki URL: {url_prefix}/wiki/index.html")


if __name__ == "__main__":
    sync()
