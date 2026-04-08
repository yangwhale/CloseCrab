#!/usr/bin/env python3
"""status.py — Display Wiki statistics.

Shows page counts by type, link count, recent log entries,
health score, manifest stats, and last lint/query timestamps.
"""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import WIKI_REPO

WIKI_DIR = WIKI_REPO / "wiki"
DATA_DIR = WIKI_REPO / "wiki-data"
GRAPH_PATH = DATA_DIR / "graph.json"
LOG_PATH = DATA_DIR / "log.json"
LINT_PATH = DATA_DIR / "lint-report.json"
MANIFEST_PATH = DATA_DIR / "compile-manifest.json"
QUERY_LOG_PATH = DATA_DIR / "query-log.json"


def load_json(path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def compute_health_score(nodes, links, lint):
    """Quick health score (same logic as rebuild-health.py)."""
    score = 100.0
    if not nodes:
        return 0
    inbound = defaultdict(int)
    for link in links:
        inbound[link["target"]] += 1
    orphans = sum(1 for n in nodes if inbound[n["id"]] == 0)
    score -= orphans * 0.5
    if lint:
        score -= len(lint.get("html_broken_links", [])) * 2
        score -= lint.get("missing_backlinks", 0) * 0.3
        score -= len(lint.get("content_issues", [])) * 1
    types_present = {n["type"] for n in nodes}
    if len(types_present) >= 4:
        score += 2
    if len(links) / max(len(nodes), 1) >= 1.5:
        score += 3
    return max(0, min(100, round(score)))


def main():
    # Graph stats
    graph = load_json(GRAPH_PATH)
    if not graph:
        print("Error: graph.json not found")
        return

    nodes = graph.get("nodes", [])
    links = graph.get("links", [])
    type_counts = defaultdict(int)
    tag_counts = defaultdict(int)
    for n in nodes:
        type_counts[n["type"]] += 1
        for tag in n.get("tags", []):
            tag_counts[tag] += 1

    lint = load_json(LINT_PATH)
    score = compute_health_score(nodes, links, lint)
    score_icon = "🟢" if score >= 80 else ("🟡" if score >= 60 else "🔴")

    print("=" * 40)
    print("  CC Wiki Status")
    print("=" * 40)

    print(f"\n{score_icon} Health: {score}/100  |  Pages: {len(nodes)}  |  Links: {len(links)}")
    for t in ["source", "entity", "concept", "analysis"]:
        count = type_counts.get(t, 0)
        if count > 0:
            print(f"   {t:>10}: {count}")

    # Top tags
    top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:10]
    print(f"\n🏷️  Top tags:")
    for tag, count in top_tags:
        print(f"   {tag}: {count}")

    # Recent log entries
    log_data = load_json(LOG_PATH)
    if log_data:
        entries = log_data.get("entries", []) if isinstance(log_data, dict) else log_data
        recent = entries[-5:] if entries else []
        print(f"\n📝 Recent activity ({len(entries)} total):")
        for e in reversed(recent):
            ts = e.get("timestamp", "?")[:10]
            action = e.get("action", e.get("operation", "?"))
            title = e.get("title", e.get("slug", ""))
            print(f"   {ts} {action:>8}: {title[:50]}")
    else:
        print("\n📝 No log entries")

    # Last lint
    if lint:
        ts = lint.get("timestamp", "?")[:19].replace("T", " ")
        issues = lint.get("total_issues", "?")
        orphans = len(lint.get("orphans", []))
        uningested = len(lint.get("uningested", []))
        print(f"\n🔍 Last lint: {ts}")
        print(f"   Issues: {issues} | Orphans: {orphans} | Uningested: {uningested}")
    else:
        print("\n🔍 No lint report (run lint.py)")

    # Manifest (incremental compile stats)
    manifest = load_json(MANIFEST_PATH)
    if manifest:
        pages = manifest.get("pages", {})
        built_at = manifest.get("built_at", "?")[:19].replace("T", " ")
        print(f"\n🔧 Compile manifest: {len(pages)} pages tracked")
        print(f"   Last build: {built_at}")

    # Last query
    query_log = load_json(QUERY_LOG_PATH)
    if query_log:
        queries = query_log.get("queries", [])
        if queries:
            last_q = queries[-1]
            print(f"\n🔎 Last query: {last_q.get('question', '?')[:50]}")
            print(f"   Time: {last_q.get('timestamp', '?')[:19].replace('T', ' ')}")
            print(f"   Total queries: {len(queries)}")

    # Disk usage
    wiki_size = sum(f.stat().st_size for f in WIKI_DIR.rglob("*") if f.is_file())
    print(f"\n💾 Wiki size: {wiki_size / 1024 / 1024:.1f} MB")
    print(f"   URL: https://cc.higcp.com/wiki/index.html")
    print(f"   Health: https://cc.higcp.com/wiki/health.html")
    print("=" * 40)


if __name__ == "__main__":
    main()
