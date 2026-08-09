"""Wiki v2 公共工具函数 — 常量、frontmatter 解析、slug 查找"""

import os
import re
import sys
import yaml
from pathlib import Path
from typing import Optional

# ── 路径常量 ──────────────────────────────────────────────────
WIKI_REPO = Path(os.path.expanduser("~/my-wiki-v2"))
WIKI_CONTENT = WIKI_REPO / "content"
WIKI_RAW = WIKI_REPO / "raw"
WIKI_PUBLIC = WIKI_REPO / "public"

# 发布目标。**不写死** —— 不同机器管不同的 Wiki，各自发到各自的地方。
# 缺省留空表示「本机不发布，只做本地索引和查询」，而不是悄悄发到别人的桶里。
WIKI_GCS = os.environ.get("WIKI_GCS", "")          # 例: gs://YOUR_BUCKET/cc-pages/wiki-v2/
WIKI_URL = os.environ.get("WIKI_URL", "")          # 例: https://wiki.example.com/wiki-v2

# 页面类型 → 子目录
TYPE_DIRS = {
    "source": "sources",
    "entity": "entities",
    "concept": "concepts",
    "analysis": "analyses",
}

# ── Frontmatter 解析 ─────────────────────────────────────────

def parse_frontmatter(filepath: Path) -> tuple[dict, str]:
    """解析 Markdown 文件的 YAML frontmatter 和正文。
    返回 (metadata_dict, body_text)。
    """
    text = filepath.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text

    # 搜索结束标记 \n---，避免匹配 frontmatter 内的 ---
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    fm_str = text[3:end].strip()
    body = text[end + 4:].strip()  # skip \n---
    try:
        meta = yaml.safe_load(fm_str) or {}
    except yaml.YAMLError as e:
        print(f"⚠️  YAML 解析错误 {filepath.name}: {e}", file=sys.stderr)
        meta = {}
    return meta, body


def validate_slug(slug: str) -> str:
    """校验 slug 安全性，防止路径穿越。返回清洗后的 slug。"""
    if not slug or "/" in slug or "\\" in slug or ".." in slug:
        raise ValueError(f"非法 slug: {slug!r}（不允许含 / \\ ..）")
    return slug


def find_page_by_slug(slug: str) -> Optional[Path]:
    """在 content/ 下按 slug（文件名不含扩展名）查找页面。"""
    validate_slug(slug)
    for subdir in TYPE_DIRS.values():
        p = WIKI_CONTENT / subdir / f"{slug}.md"
        if p.exists():
            return p
    # 也检查 content 根目录
    p = WIKI_CONTENT / f"{slug}.md"
    if p.exists():
        return p
    return None


def all_pages() -> list[Path]:
    """列出 content/ 下所有 .md 文件。"""
    return sorted(WIKI_CONTENT.rglob("*.md"))


def extract_wikilinks(text: str) -> list[str]:
    """提取 Markdown 正文中的 [[wikilink]] slug 列表。
    过滤掉：文件夹链接、代码块内的引用、通用占位符。
    """
    # 先移除代码块和行内代码中的内容
    cleaned = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"`[^`]+`", "", cleaned)
    raw = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", cleaned)
    # 过滤文件夹链接和占位符；剥离 #anchor fragment 拿 slug 部分
    skip = {"wikilink", "wikilinks", "slug"}
    return [s.split("#")[0] for s in raw if not s.endswith("/") and s.split("#")[0].lower() not in skip and s.split("#")[0]]


def page_url(filepath: Path) -> str:
    """根据文件路径生成公网 URL。"""
    rel = filepath.relative_to(WIKI_CONTENT)
    slug = rel.with_suffix("")
    return f"{WIKI_URL}/{slug}"
