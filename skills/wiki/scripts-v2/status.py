#!/usr/bin/env python3
"""Wiki v2 Status — 页面统计、最近变更、构建时间。

用法:
  python3 status.py
  python3 status.py --json
"""

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wiki_utils import WIKI_CONTENT, WIKI_REPO, WIKI_PUBLIC, WIKI_URL, all_pages, parse_frontmatter


def get_status() -> dict:
    """收集 Wiki 状态信息。"""
    pages = all_pages()
    type_counts = Counter()
    tag_counts = Counter()
    oldest = None
    newest = None

    for p in pages:
        meta, _ = parse_frontmatter(p)
        ptype = meta.get("type", "unknown")
        type_counts[ptype] += 1
        for tag in (meta.get("tags") or []):
            tag_counts[str(tag).lower()] += 1

        d = meta.get("date")
        if d:
            ds = str(d)
            if oldest is None or ds < oldest:
                oldest = ds
            if newest is None or ds > newest:
                newest = ds

    # 最近 git 变更
    recent_changes = []
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-10", "--", "content/"],
            capture_output=True, text=True, cwd=WIKI_REPO,
        )
        if result.returncode == 0:
            recent_changes = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
    except Exception:
        pass

    # 上次构建时间
    last_build = None
    index_html = WIKI_PUBLIC / "index.html"
    if index_html.exists():
        mtime = index_html.stat().st_mtime
        last_build = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")

    # Top 10 标签
    top_tags = tag_counts.most_common(10)

    # 知识覆盖度分析
    from wiki_utils import extract_wikilinks

    orphans = []  # 无 wikilinks 的页面
    deprecated_count = 0
    total_wikilinks = 0
    pages_with_links = 0
    recent_30d = 0

    for p in pages:
        meta, body = parse_frontmatter(p)
        links = extract_wikilinks(body)
        total_wikilinks += len(links)
        if links:
            pages_with_links += 1
        else:
            orphans.append(p.stem)

        if meta.get("deprecated"):
            deprecated_count += 1

        d = meta.get("date")
        if d:
            try:
                from datetime import date as date_type
                if isinstance(d, date_type):
                    page_date = d
                else:
                    page_date = datetime.strptime(str(d), "%Y-%m-%d").date()
                if (datetime.now().date() - page_date).days <= 30:
                    recent_30d += 1
            except (ValueError, TypeError):
                pass

    # 知识覆盖度评分（0-100）
    connectivity = pages_with_links / max(len(pages), 1)  # 有链接的比例
    freshness = min(recent_30d / 10, 1.0)  # 最近 30 天新增（10+ 满分）
    tag_coverage = min(len(tag_counts) / 50, 1.0)  # tag 多样性（50+ 满分）
    coverage_score = round((connectivity * 40 + freshness * 30 + tag_coverage * 30), 1)

    return {
        "total_pages": len(pages),
        "by_type": dict(type_counts),
        "date_range": {"oldest": oldest, "newest": newest},
        "top_tags": [{"tag": t, "count": c} for t, c in top_tags],
        "total_tags": len(tag_counts),
        "last_build": last_build,
        "recent_changes": recent_changes[:5],
        "url": WIKI_URL,
        "knowledge_coverage": {
            "score": coverage_score,
            "connectivity": round(connectivity * 100, 1),
            "pages_with_links": pages_with_links,
            "orphan_count": len(orphans),
            "orphan_pages": orphans[:10],
            "total_wikilinks": total_wikilinks,
            "avg_links_per_page": round(total_wikilinks / max(len(pages), 1), 1),
            "recent_30d_pages": recent_30d,
            "deprecated_count": deprecated_count,
            "freshness_pct": round(freshness * 100, 1),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Wiki v2 Status")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    args = parser.parse_args()

    status = get_status()

    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return

    print(f"📚 Wiki v2 状态")
    print(f"   URL: {status['url']}")
    print(f"   总页面: {status['total_pages']}")
    print(f"   类型: {status['by_type']}")
    print(f"   日期范围: {status['date_range']['oldest']} ~ {status['date_range']['newest']}")
    print(f"   标签数: {status['total_tags']}")
    print()

    if status["top_tags"]:
        print("🏷️  热门标签:")
        for t in status["top_tags"]:
            print(f"   {t['tag']}: {t['count']}")
        print()

    if status["last_build"]:
        print(f"🔨 上次构建: {status['last_build']}")

    if status["recent_changes"]:
        print(f"\n📝 最近变更:")
        for c in status["recent_changes"]:
            print(f"   {c}")


if __name__ == "__main__":
    main()
