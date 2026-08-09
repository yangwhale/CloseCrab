#!/usr/bin/env python3
"""Wiki v2 Ingest — 保存 raw 资料 + 创建骨架 Markdown 页面。

用法:
  # URL 文章（Bot 先 WebFetch 获取内容后调用）
  python3 ingest.py url --slug article-name --title "Title" --tags "tag1,tag2" \
      --text "fetched content..." [--source-url "https://..."]

  # PDF 论文
  python3 ingest.py pdf /path/to/paper.pdf --slug paper-name --title "Title" --tags "ml,training"

  # 纯文本
  python3 ingest.py text --slug note-name --title "Title" --tags "misc" --text "内容..."

  # 指定页面类型（默认 source）
  python3 ingest.py url --slug name --title "T" --type entity --tags "t1"

输出: 创建的 Markdown 文件路径（Bot 随后填充详细内容）
"""

import argparse
import shutil
import sys
from datetime import date
from pathlib import Path

# 允许从任意位置调用
sys.path.insert(0, str(Path(__file__).parent))
from wiki_utils import WIKI_CONTENT, WIKI_RAW, TYPE_DIRS, find_page_by_slug, validate_slug


def save_raw(source_type: str, slug: str, content: str, src_path: str = None) -> Path:
    """保存原始资料到 raw/ 目录。"""
    validate_slug(slug)
    type_to_dir = {"url": "articles", "pdf": "papers", "text": "notes"}
    raw_dir = WIKI_RAW / type_to_dir.get(source_type, "notes")
    raw_dir.mkdir(parents=True, exist_ok=True)

    if source_type == "pdf" and src_path:
        dst = raw_dir / f"{slug}.pdf"
        shutil.copy2(src_path, dst)
        return dst

    ext = ".md" if source_type == "url" else ".txt"
    dst = raw_dir / f"{slug}{ext}"
    dst.write_text(content, encoding="utf-8")
    return dst


def create_skeleton(slug: str, title: str, page_type: str, tags: list[str],
                    source_url: str = None, raw_path: Path = None) -> Path:
    """创建骨架 Markdown 页面（Bot 随后填充详细内容）。"""
    subdir = TYPE_DIRS.get(page_type, "sources")
    out_dir = WIKI_CONTENT / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.md"

    if out_path.exists():
        print(f"⚠️  页面已存在: {out_path}", file=sys.stderr)
        print(str(out_path))
        return out_path

    today = date.today().isoformat()
    tags_yaml = "\n".join(f"  - {t.strip()}" for t in tags if t.strip())

    source_line = ""
    if source_url:
        source_line = f"\n[原文链接]({source_url}) · {today}\n"
    elif raw_path:
        source_line = f"\n原始资料: `{raw_path.name}`\n"

    # 转义 title 中的双引号，避免 YAML 注入
    safe_title = title.replace('"', '\\"')

    # 空标签时生成 tags: []，避免 null
    tags_block = f"\n{tags_yaml}" if tags_yaml else " []"

    content = f"""---
title: "{safe_title}"
description: ""
type: {page_type}
date: {today}
lastmod: {today}
tags:{tags_block}
---

## 原文
{source_line}
## 核心要点

1. **要点一**：待填充
2. **要点二**：待填充
3. **要点三**：待填充

## 详细内容

<!-- Bot: 请替换此部分为详细的结构化内容 -->
"""

    out_path.write_text(content, encoding="utf-8")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Wiki v2 Ingest")
    parser.add_argument("source_type", choices=["url", "pdf", "text"],
                        help="资料类型")
    parser.add_argument("src_path", nargs="?", default=None,
                        help="PDF 文件路径（仅 pdf 类型需要）")
    parser.add_argument("--slug", required=True, help="页面 slug（kebab-case）")
    parser.add_argument("--title", required=True, help="页面标题")
    parser.add_argument("--type", default="source", dest="page_type",
                        choices=TYPE_DIRS.keys(), help="页面类型（默认 source）")
    parser.add_argument("--tags", default="", help="标签（逗号分隔）")
    parser.add_argument("--text", default="", help="内容文本（url/text 类型）")
    parser.add_argument("--text-file", default=None, help="从文件读取内容（避免 CLI 参数过长）")
    parser.add_argument("--source-url", default=None, help="原文 URL")

    args = parser.parse_args()
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    # 支持从文件读取大内容
    if args.text_file:
        args.text = Path(args.text_file).read_text(encoding="utf-8")
    elif not args.text and not sys.stdin.isatty():
        args.text = sys.stdin.read()

    # 检查是否已存在
    existing = find_page_by_slug(args.slug)
    if existing:
        print(f"⚠️  slug '{args.slug}' 已存在: {existing}", file=sys.stderr)
        print(str(existing))
        return

    # 保存 raw
    raw_path = None
    if args.source_type == "pdf":
        if not args.src_path:
            print("错误: pdf 类型需要提供文件路径", file=sys.stderr)
            sys.exit(1)
        raw_path = save_raw("pdf", args.slug, "", src_path=args.src_path)
    elif args.text:
        raw_path = save_raw(args.source_type, args.slug, args.text)

    # 创建骨架页面
    page_path = create_skeleton(
        slug=args.slug,
        title=args.title,
        page_type=args.page_type,
        tags=tags,
        source_url=args.source_url,
        raw_path=raw_path,
    )

    print(str(page_path))
    print(f"✅ 骨架页面已创建，Bot 请填充详细内容后运行 build-and-sync.sh", file=sys.stderr)


if __name__ == "__main__":
    main()
