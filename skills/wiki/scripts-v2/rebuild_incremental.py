#!/usr/bin/env python3
"""rebuild_incremental.py — Incremental rebuild for CC Wiki.

Main entry point for the v2 rebuild system. Replaces the O(K×N) full-scan
approach with manifest-driven incremental rebuilds.

Usage:
  # Auto-detect changes and rebuild incrementally
  python3 rebuild_incremental.py --auto

  # Full scan + rebuild (for bootstrap or verification)
  python3 rebuild_incremental.py --full

  # Dry run (show what would change)
  python3 rebuild_incremental.py --dry-run

  # Rebuild for specific pages only
  python3 rebuild_incremental.py --pages entities/tpu-v7.html sources/foo.html
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# Import v2 modules
sys.path.insert(0, str(Path(__file__).parent))
from manifest_v2 import ManifestV2, WIKI_DIR, DATA_DIR

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from wiki_utils import WIKI_REPO

# Import v1 functions we reuse directly
import importlib.util

_rg_spec = importlib.util.spec_from_file_location(
    "rebuild_graph",
    str(Path(__file__).parent.parent / "scripts" / "rebuild-graph.py"),
)
_rg_mod = importlib.util.module_from_spec(_rg_spec)
_rg_spec.loader.exec_module(_rg_mod)
build_graph_html = _rg_mod.build_graph_html

_ri_spec = importlib.util.spec_from_file_location(
    "rebuild_index",
    str(Path(__file__).parent.parent / "scripts" / "rebuild-index.py"),
)
_ri_mod = importlib.util.module_from_spec(_ri_spec)
_ri_spec.loader.exec_module(_ri_mod)
build_index_html = _ri_mod.build_index_html

SCRIPT_DIR_V1 = Path(__file__).parent.parent / "scripts"


def run_v1_script(name: str, args: list[str] | None = None) -> bool:
    """Run a v1 wiki script. Returns True on success."""
    script = SCRIPT_DIR_V1 / name
    cmd = [sys.executable, str(script)] + (args or [])
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        print(f"  WARNING: {name} failed: {r.stderr[:200]}", file=sys.stderr)
    return r.returncode == 0


def run_v1_shell(name: str, args: list[str] | None = None) -> bool:
    """Run a v1 shell script."""
    script = SCRIPT_DIR_V1 / name
    cmd = ["bash", str(script)] + (args or [])
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return r.returncode == 0


def rebuild_from_manifest(manifest: ManifestV2, affected_slugs: set[str] | None = None,
                          full: bool = False) -> dict:
    """Core rebuild logic: generate graph/index/backlinks/search from manifest.

    Args:
        manifest: Loaded and up-to-date ManifestV2.
        affected_slugs: If provided, only rewrite backlinks for these slugs.
                        If None or full=True, rewrite all backlinks.
        full: Force full backlinks rewrite.

    Returns:
        Stats dict with timing and counts.
    """
    stats = {}
    t0 = time.time()

    # 1. Build graph.json from manifest (no file I/O)
    t1 = time.time()
    graph_data = manifest.build_graph_json()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    graph_path = DATA_DIR / "graph.json"
    graph_path.write_text(json.dumps(graph_data, ensure_ascii=False, indent=2), encoding="utf-8")
    stats["graph_nodes"] = graph_data["meta"]["node_count"]
    stats["graph_links"] = graph_data["meta"]["link_count"]
    stats["graph_ms"] = int((time.time() - t1) * 1000)
    print(f"  graph.json: {stats['graph_nodes']} nodes, {stats['graph_links']} links ({stats['graph_ms']}ms)")

    # 2. Build graph.html (static template, no data dependency)
    graph_html_path = WIKI_DIR / "graph.html"
    graph_html_path.write_text(build_graph_html(), encoding="utf-8")

    # 3. Build index.html from manifest (no file I/O)
    t1 = time.time()
    pages = manifest.build_index_data()
    index_path = WIKI_DIR / "index.html"
    index_path.write_text(build_index_html(pages), encoding="utf-8")
    stats["index_pages"] = len(pages)
    stats["index_ms"] = int((time.time() - t1) * 1000)
    print(f"  index.html: {stats['index_pages']} pages ({stats['index_ms']}ms)")

    # 4. Inject backlinks (targeted or full)
    t1 = time.time()
    from backlink_injector import inject_backlinks_for, inject_all_backlinks

    backlinks = manifest.compute_backlinks()
    slug_to_path = {entry["slug"]: entry["path"] for entry in manifest.pages.values()}

    if full or affected_slugs is None:
        updated = inject_all_backlinks(backlinks, slug_to_path)
        stats["backlinks_mode"] = "full"
    else:
        updated = inject_backlinks_for(affected_slugs, backlinks, slug_to_path)
        stats["backlinks_mode"] = "incremental"

    # Re-hash files modified by backlinks injection so manifest stays in sync.
    # Only re-hash pages that were targeted for injection.
    if updated > 0:
        from wiki_utils import compute_file_hash
        rehash_slugs = set(slug_to_path.keys()) if (full or affected_slugs is None) else affected_slugs
        path_to_key = {entry["path"]: k for k, entry in manifest.pages.items()}
        for slug in rehash_slugs:
            rel_path = slug_to_path.get(slug)
            if not rel_path:
                continue
            page_path = WIKI_DIR / rel_path
            if page_path.exists():
                key = path_to_key.get(rel_path)
                if key and key in manifest.pages:
                    manifest.pages[key]["sha256"] = compute_file_hash(page_path)

    stats["backlinks_updated"] = updated
    stats["backlinks_ms"] = int((time.time() - t1) * 1000)
    print(f"  backlinks: {updated} pages updated ({stats['backlinks_mode']}, {stats['backlinks_ms']}ms)")

    # 5. Update search index
    t1 = time.time()
    from search_updater import update_chunks, full_rebuild as full_search_rebuild

    if full or affected_slugs is None:
        search_stats = full_search_rebuild()
        stats["search_mode"] = "full"
        stats["search_chunks"] = search_stats["chunk_count"]
    else:
        changed_slugs = {s for s in affected_slugs if slug_to_path.get(s)}
        search_stats = update_chunks(changed_slugs)
        stats["search_mode"] = "incremental"
        stats["search_chunks"] = search_stats["total"]

    stats["search_ms"] = int((time.time() - t1) * 1000)
    print(f"  search: {stats['search_chunks']} chunks ({stats['search_mode']}, {stats['search_ms']}ms)")

    # 6. Pagefind (always full — no incremental API)
    t1 = time.time()
    run_v1_shell("rebuild-search.sh")
    stats["pagefind_ms"] = int((time.time() - t1) * 1000)
    print(f"  pagefind: ({stats['pagefind_ms']}ms)")

    stats["total_ms"] = int((time.time() - t0) * 1000)
    return stats


def cmd_auto(args):
    """Auto-detect changes and rebuild incrementally."""
    manifest = ManifestV2().load()
    changes = manifest.detect_changes()
    total_changes = sum(len(v) for v in changes.values())

    if total_changes == 0:
        print("No changes detected.")
        return

    print(f"Changes: +{len(changes['added'])} ~{len(changes['changed'])} -{len(changes['deleted'])}")

    # Process changes
    all_affected = set()

    for rel_key in changes["added"] + changes["changed"]:
        html_path = WIKI_DIR / rel_key
        if not html_path.exists():
            continue

        old_entry = manifest.pages.get(rel_key)
        new_entry = manifest.scan_page(html_path)
        if new_entry:
            manifest.pages[rel_key] = new_entry
            affected = manifest.compute_affected_slugs(old_entry, new_entry)
            all_affected.update(affected)

    for rel_key in changes["deleted"]:
        old_entry = manifest.pages.pop(rel_key, None)
        if old_entry:
            affected = manifest.compute_affected_slugs(old_entry, None)
            all_affected.update(affected)

    print(f"Affected slugs for backlinks: {len(all_affected)}")

    # Rebuild
    stats = rebuild_from_manifest(manifest, affected_slugs=all_affected)

    # Save manifest
    manifest.save()

    # Run log + sync
    if not args.no_sync:
        run_v1_script("sync-to-gcs.py")
        print("  sync: done")

    print(f"\nTotal: {stats['total_ms']}ms")


def cmd_full(args):
    """Full scan + rebuild (bootstrap or verification)."""
    manifest = ManifestV2().load()

    print("Full scan...")
    scan_stats = manifest.full_scan()
    print(f"  Scanned: {len(manifest.pages)} pages "
          f"(+{scan_stats['added']} ~{scan_stats['changed']} -{scan_stats['deleted']})")

    print("Rebuilding...")
    stats = rebuild_from_manifest(manifest, full=True)

    # Save manifest
    manifest.save()

    # Sync
    if not args.no_sync:
        run_v1_script("sync-to-gcs.py")
        print("  sync: done")

    print(f"\nTotal: {stats['total_ms']}ms")


def cmd_dry_run(args):
    """Show what would change without doing anything."""
    manifest = ManifestV2().load()
    changes = manifest.detect_changes()

    total = sum(len(v) for v in changes.values())
    print(f"Changes detected: {total}")

    if changes["added"]:
        print(f"\n  Added ({len(changes['added'])}):")
        for p in sorted(changes["added"]):
            print(f"    + {p}")

    if changes["changed"]:
        print(f"\n  Changed ({len(changes['changed'])}):")
        for p in sorted(changes["changed"]):
            print(f"    ~ {p}")

    if changes["deleted"]:
        print(f"\n  Deleted ({len(changes['deleted'])}):")
        for p in sorted(changes["deleted"]):
            print(f"    - {p}")

    if total == 0:
        print("  (nothing to do)")
    else:
        # Estimate affected slugs
        all_affected = set()
        for rel_key in changes["added"] + changes["changed"]:
            html_path = WIKI_DIR / rel_key
            if not html_path.exists():
                continue
            old_entry = manifest.pages.get(rel_key)
            new_entry = manifest.scan_page(html_path)
            if new_entry:
                affected = manifest.compute_affected_slugs(old_entry, new_entry)
                all_affected.update(affected)

        for rel_key in changes["deleted"]:
            old_entry = manifest.pages.get(rel_key)
            if old_entry:
                affected = manifest.compute_affected_slugs(old_entry, None)
                all_affected.update(affected)

        print(f"\n  Would rewrite backlinks for {len(all_affected)} pages: {', '.join(sorted(all_affected)[:10])}{'...' if len(all_affected) > 10 else ''}")


def cmd_pages(args):
    """Rebuild for specific pages only."""
    manifest = ManifestV2().load()
    all_affected = set()

    for rel_key in args.pages:
        html_path = WIKI_DIR / rel_key
        if not html_path.exists():
            print(f"  WARNING: {rel_key} not found, skipping", file=sys.stderr)
            continue

        old_entry = manifest.pages.get(rel_key)
        new_entry = manifest.scan_page(html_path)
        if new_entry:
            manifest.pages[rel_key] = new_entry
            affected = manifest.compute_affected_slugs(old_entry, new_entry)
            all_affected.update(affected)
            print(f"  Scanned: {rel_key}")

    if not all_affected:
        print("No changes to process.")
        return

    print(f"Affected slugs: {len(all_affected)}")

    stats = rebuild_from_manifest(manifest, affected_slugs=all_affected)
    manifest.save()

    if not args.no_sync:
        run_v1_script("sync-to-gcs.py")
        print("  sync: done")

    print(f"\nTotal: {stats['total_ms']}ms")


def main():
    parser = argparse.ArgumentParser(
        description="Wiki v2 incremental rebuild",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--no-sync", action="store_true", help="Skip GCS sync")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--auto", action="store_true", help="Auto-detect changes, rebuild incrementally")
    group.add_argument("--full", action="store_true", help="Full scan + rebuild (bootstrap/verify)")
    group.add_argument("--dry-run", action="store_true", help="Show changes without executing")
    group.add_argument("--pages", nargs="+", metavar="REL_PATH",
                       help="Rebuild specific pages (e.g. entities/tpu-v7.html)")

    args = parser.parse_args()

    if args.auto:
        cmd_auto(args)
    elif args.full:
        cmd_full(args)
    elif args.dry_run:
        cmd_dry_run(args)
    elif args.pages:
        cmd_pages(args)


if __name__ == "__main__":
    main()
