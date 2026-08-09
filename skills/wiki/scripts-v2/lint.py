#!/usr/bin/env python3
"""Wiki v2 Lint — 健康检查：断链、孤儿、缺失 frontmatter、标签一致性。

用法:
  python3 lint.py              # 完整报告
  python3 lint.py --json       # JSON 格式输出
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wiki_utils import (WIKI_CONTENT, all_pages, parse_frontmatter,
                        extract_wikilinks)


def check_all() -> dict:
    """运行所有检查，返回报告。"""
    pages = all_pages()
    page_slugs = set()  # 所有已知 slug
    all_links = defaultdict(list)  # slug → [被谁引用]
    issues = []
    stats = {"total_pages": len(pages), "by_type": Counter()}

    # 第一遍：收集所有 slug 和 frontmatter
    page_data = {}
    for p in pages:
        slug = p.stem
        page_slugs.add(slug)

        meta, body = parse_frontmatter(p)
        page_data[slug] = {"meta": meta, "body": body, "path": p}
        stats["by_type"][meta.get("type", "unknown")] += 1

        # 检查 frontmatter 完整性
        required = ["title", "type", "date", "tags"]
        missing = [f for f in required if not meta.get(f)]
        if missing:
            issues.append({
                "type": "missing_frontmatter",
                "severity": "warning",
                "page": slug,
                "path": str(p),
                "detail": f"缺失字段: {', '.join(missing)}",
            })

        # 检查内容长度
        if len(body) < 100 and slug != "index":
            issues.append({
                "type": "short_content",
                "severity": "info",
                "page": slug,
                "path": str(p),
                "detail": f"正文仅 {len(body)} 字符",
            })

        # 收集 wikilinks
        links = extract_wikilinks(body)
        for link in links:
            all_links[link].append(slug)

    # 第二遍：检查断链
    for target_slug, source_slugs in all_links.items():
        if target_slug not in page_slugs:
            issues.append({
                "type": "broken_link",
                "severity": "error",
                "page": target_slug,
                "detail": f"[[{target_slug}]] 引用自 {', '.join(source_slugs[:3])}{'...' if len(source_slugs) > 3 else ''} 但页面不存在",
            })

    # 第三遍：检查孤儿页面（没有任何入链的页面）
    referenced_slugs = set(all_links.keys())
    for slug in page_slugs:
        if slug == "index":
            continue
        if slug not in referenced_slugs:
            meta = page_data[slug]["meta"]
            page_type = meta.get("type", "unknown")
            severity = "info" if page_type == "source" else "warning"
            issues.append({
                "type": "orphan",
                "severity": severity,
                "page": slug,
                "path": str(page_data[slug]["path"]),
                "detail": f"孤儿页面（{page_type}），无入链",
            })

    # 标签一致性检查
    all_tags = Counter()
    for slug, data in page_data.items():
        for tag in (data["meta"].get("tags") or []):
            all_tags[str(tag).lower()] += 1

    # 检查相似标签（复数/连字符变体）
    tag_list = sorted(all_tags.keys())
    similar_pairs = []
    for i, t1 in enumerate(tag_list):
        for t2 in tag_list[i + 1:]:
            if _are_similar(t1, t2):
                similar_pairs.append((t1, t2))

    if similar_pairs:
        for t1, t2 in similar_pairs[:10]:
            issues.append({
                "type": "similar_tags",
                "severity": "info",
                "page": "",
                "detail": f"相似标签: '{t1}' ({all_tags[t1]}次) vs '{t2}' ({all_tags[t2]}次)",
            })

    # 汇总
    report = {
        "stats": {
            "total_pages": stats["total_pages"],
            "by_type": dict(stats["by_type"]),
            "total_tags": len(all_tags),
            "total_wikilinks": sum(len(v) for v in all_links.values()),
        },
        "issues": sorted(issues, key=lambda x: {"error": 0, "warning": 1, "info": 2}[x["severity"]]),
        "issue_summary": {
            "errors": sum(1 for i in issues if i["severity"] == "error"),
            "warnings": sum(1 for i in issues if i["severity"] == "warning"),
            "info": sum(1 for i in issues if i["severity"] == "info"),
        },
    }
    return report


def _are_similar(t1: str, t2: str) -> bool:
    """标签相似性判断：只检测明确的重复变体。"""
    # 去掉连字符/下划线后相同（如 tpu-v7 vs tpuv7）
    if t1.replace("-", "").replace("_", "") == t2.replace("-", "").replace("_", ""):
        return True
    # 单复数（如 benchmark vs benchmarks）
    if t1 + "s" == t2 or t2 + "s" == t1:
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Wiki v2 Lint")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    args = parser.parse_args()

    report = check_all()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    # 文本格式输出
    s = report["stats"]
    print(f"📊 Wiki 统计: {s['total_pages']} 页面, {s['total_tags']} 标签, {s['total_wikilinks']} 内链")
    print(f"   类型分布: {dict(s['by_type'])}")
    print()

    summary = report["issue_summary"]
    print(f"🔍 检查结果: {summary['errors']} 错误, {summary['warnings']} 警告, {summary['info']} 提示")
    print()

    if not report["issues"]:
        print("✅ 一切正常！")
        return

    severity_icons = {"error": "❌", "warning": "⚠️", "info": "💡"}
    for issue in report["issues"]:
        icon = severity_icons[issue["severity"]]
        page = issue.get("page", "")
        print(f"  {icon} [{issue['type']}] {page}: {issue['detail']}")


if __name__ == "__main__":
    main()
