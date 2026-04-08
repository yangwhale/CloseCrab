#!/usr/bin/env python3
"""lint.py — Wiki health check and knowledge discovery.

Checks:
  1. Orphan pages (0 inbound links)
  2. Broken links (links-to targets that don't exist)
  3. HTML wiki-link broken paths
  4. Backlinks consistency
  5. Content quality (empty/short source pages)
  6. Tag analysis (duplicates, similar tags)
  7. Uningested CC Pages

Outputs JSON report to wiki-data/lint-report.json and prints summary.
"""
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
WIKI_DIR = WIKI_REPO / "wiki"
GRAPH_PATH = WIKI_REPO / "wiki-data" / "graph.json"
CC_PAGES = Path(os.environ.get("CC_PAGES_WEB_ROOT", os.path.expanduser("~/gcs-mount/cc-pages"))) / "pages"

SUBDIRS = ["sources", "entities", "concepts", "analyses"]


def load_graph():
    if not GRAPH_PATH.exists():
        print("Error: graph.json not found. Run rebuild-graph.py first.")
        sys.exit(1)
    return json.loads(GRAPH_PATH.read_text())


def check_orphans(nodes, links):
    """Find pages with 0 inbound links."""
    inbound = defaultdict(int)
    for link in links:
        inbound[link["target"]] += 1
    return [
        {"id": n["id"], "type": n["type"], "title": n["title"]}
        for n in nodes
        if inbound[n["id"]] == 0
    ]


def check_meta_broken_links(nodes, node_ids):
    """Find links-to targets that don't exist as pages."""
    broken = []
    for n in nodes:
        path = WIKI_DIR / n["path"]
        if not path.exists():
            continue
        soup = BeautifulSoup(path.read_text(), "html.parser")
        meta = soup.find("meta", attrs={"name": "wiki-links-to"})
        if not meta or not meta.get("content"):
            continue
        for t in meta["content"].split(","):
            t = t.strip()
            if t and t not in node_ids:
                broken.append({"page": n["id"], "target": t})
    return broken


def check_html_broken_links(nodes):
    """Find wiki-link hrefs that point to non-existent files."""
    broken = []
    for subdir in SUBDIRS:
        dir_path = WIKI_DIR / subdir
        if not dir_path.exists():
            continue
        for f in dir_path.glob("*.html"):
            soup = BeautifulSoup(f.read_text(), "html.parser")
            for a in soup.find_all("a", class_="wiki-link"):
                href = a.get("href", "")
                if href.startswith("http"):
                    continue
                target_path = (f.parent / href).resolve()
                if not target_path.exists():
                    broken.append({"page": f.stem, "href": href})
    return broken


def check_backlinks(nodes, node_ids):
    """Find missing backlinks (A links-to B, but B's backlinks section doesn't list A)."""
    actual_refs = defaultdict(set)
    for n in nodes:
        path = WIKI_DIR / n["path"]
        if not path.exists():
            continue
        soup = BeautifulSoup(path.read_text(), "html.parser")
        meta = soup.find("meta", attrs={"name": "wiki-links-to"})
        if not meta or not meta.get("content"):
            continue
        for t in meta["content"].split(","):
            t = t.strip()
            if t in node_ids:
                actual_refs[t].add(n["id"])

    missing = []
    for target_id, sources in actual_refs.items():
        n = node_ids.get(target_id)
        if not n:
            continue
        path = WIKI_DIR / n["path"]
        if not path.exists():
            continue

        soup = BeautifulSoup(path.read_text(), "html.parser")
        bl_section = soup.find("section", class_="wiki-backlinks")
        existing_slugs = set()
        if bl_section:
            for a in bl_section.find_all("a"):
                href = a.get("href", "")
                slug = re.sub(r"\.html$", "", href.split("/")[-1])
                existing_slugs.add(slug)

        for src_id in sources:
            if src_id not in existing_slugs:
                missing.append({"target": target_id, "source": src_id})

    return missing


def check_content_quality(nodes):
    """Find source pages with very little content."""
    short = []
    for n in nodes:
        if n["type"] != "source":
            continue
        path = WIKI_DIR / n["path"]
        if not path.exists():
            short.append({"id": n["id"], "title": n["title"], "reason": "file_missing"})
            continue
        size = path.stat().st_size
        if size < 2000:
            short.append({"id": n["id"], "title": n["title"], "size": size, "reason": "too_short"})
    return short


def check_tags(nodes):
    """Analyze tags for potential duplicates and inconsistencies."""
    tag_counts = defaultdict(int)
    for n in nodes:
        for tag in n.get("tags", []):
            tag_counts[tag] += 1

    # Find similar tags
    similar = []
    all_tags = list(tag_counts.keys())
    for i, t1 in enumerate(all_tags):
        for t2 in all_tags[i + 1 :]:
            if len(t1) > 2 and len(t2) > 2 and t1 != t2:
                if t1 in t2 or t2 in t1:
                    similar.append({"tag1": t1, "count1": tag_counts[t1], "tag2": t2, "count2": tag_counts[t2]})

    return dict(tag_counts), similar


def scan_uningested():
    """Find CC Pages HTML files not yet in Wiki sources."""
    if not CC_PAGES.exists():
        return []

    existing = set()
    src_dir = WIKI_DIR / "sources"
    if src_dir.exists():
        existing = {f.name for f in src_dir.glob("*.html")}

    skip = {"index.html", "cc_fun.html"}
    uningested = []
    for f in sorted(CC_PAGES.glob("*.html")):
        if f.name in skip or f.name in existing:
            continue
        size = f.stat().st_size
        # Quick title extraction
        title = f.stem
        try:
            content = f.read_text(errors="replace")[:3000]
            m = re.search(r"<title>(.*?)</title>", content, re.DOTALL)
            if m:
                title = m.group(1).strip()
        except Exception:
            pass
        uningested.append({"file": f.name, "title": title, "size": size})

    return uningested


def main():
    g = load_graph()
    nodes = g["nodes"]
    links = g["links"]
    node_ids = {n["id"]: n for n in nodes}

    # Type counts
    type_counts = defaultdict(int)
    for n in nodes:
        type_counts[n["type"]] += 1

    print("=" * 50)
    print("  CC Wiki Lint Report")
    print("=" * 50)
    print(f"\n📊 Stats: {len(nodes)} pages, {len(links)} links")
    for t in ["source", "entity", "concept", "analysis"]:
        print(f"   {t}: {type_counts.get(t, 0)}")

    # Run all checks
    orphans = check_orphans(nodes, links)
    meta_broken = check_meta_broken_links(nodes, node_ids)
    html_broken = check_html_broken_links(nodes)
    missing_bl = check_backlinks(nodes, node_ids)
    short_pages = check_content_quality(nodes)
    tag_counts, similar_tags = check_tags(nodes)
    uningested = scan_uningested()

    # Print results
    issues = 0

    print(f"\n🔗 Broken links (meta): {len(meta_broken)}")
    for b in meta_broken:
        print(f"   [{b['page']}] → '{b['target']}' NOT FOUND")
    issues += len(meta_broken)

    print(f"\n🔗 Broken links (HTML): {len(html_broken)}")
    for b in html_broken:
        print(f"   [{b['page']}] → {b['href']}")
    issues += len(html_broken)

    print(f"\n↩️  Missing backlinks: {len(missing_bl)}")
    if missing_bl:
        # Group by target
        by_target = defaultdict(list)
        for m in missing_bl:
            by_target[m["target"]].append(m["source"])
        for target, sources in sorted(by_target.items()):
            print(f"   [{target}] missing from: {', '.join(sources)}")
    issues += len(missing_bl)

    print(f"\n📄 Content quality issues: {len(short_pages)}")
    for s in short_pages:
        print(f"   [{s['id']}] {s['reason']} ({s.get('size', '?')}B)")
    issues += len(short_pages)

    print(f"\n👻 Orphan pages: {len(orphans)}")
    for o in orphans:
        print(f"   [{o['type']}] {o['title']} ({o['id']})")

    print(f"\n🏷️  Similar tags (possible duplicates): {len(similar_tags)}")
    for s in similar_tags:
        print(f"   '{s['tag1']}' ({s['count1']}) vs '{s['tag2']}' ({s['count2']})")

    print(f"\n📥 Uningested CC Pages: {len(uningested)}")
    for u in uningested:
        size_kb = u["size"] / 1024
        print(f"   {u['file']} ({size_kb:.0f}KB) — {u['title'][:60]}")

    # Summary
    print(f"\n{'=' * 50}")
    print(f"  Issues: {issues}  |  Orphans: {len(orphans)}  |  Uningested: {len(uningested)}")
    if issues == 0:
        print("  ✅ Wiki is healthy!")
    else:
        print("  Run fix-backlinks.py and fix-broken-links.py to auto-fix.")
    print(f"{'=' * 50}")

    # Save report
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stats": {"pages": len(nodes), "links": len(links), "types": dict(type_counts)},
        "orphans": orphans,
        "meta_broken_links": meta_broken,
        "html_broken_links": html_broken,
        "missing_backlinks": len(missing_bl),
        "content_issues": short_pages,
        "similar_tags": similar_tags,
        "uningested": uningested,
        "total_issues": issues,
    }
    report_path = WIKI_REPO / "wiki-data" / "lint-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nReport saved: {report_path}")


if __name__ == "__main__":
    main()
