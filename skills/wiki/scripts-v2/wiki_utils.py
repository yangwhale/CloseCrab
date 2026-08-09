"""Wiki v2 公共工具函数 — 常量、frontmatter 解析、slug 查找"""

import os
import re
import sys
import yaml
from pathlib import Path
from typing import Optional

# ── 路径常量 ──────────────────────────────────────────────────
# WIKI_REPO 的取值顺序：环境变量 → 自定位到本文件所在仓库根 → 兜底常见目录。
#
# 本文件原本写死 ~/my-wiki-v2，导致 deploy 在 env.sh 里做的 WIKI_REPO 注入完全失效：
# 换台机器就指向不存在的目录，content 找不到、查询恒为空、而且不报错。
#
# 自定位（Path(__file__).parent.parent）只对**运行副本**成立：
#   ~/my-wiki-v2/scripts/wiki_utils.py        → ~/my-wiki-v2        ✅ 有 content/
#   CloseCrab/skills/wiki/scripts-v2/...      → skills/wiki         ❌ 永远没有 content/
# 主仓这份是分发副本，恰恰是新机器会用的那份，所以自定位在这里靠不住。
# 结论：与其挑一个「更好的默认值」，不如让取不到有效值时**明确报错** ——
# 今天连撞六次的都是同一个模式：静默回落到不存在的默认值，系统看着在跑，实际什么都没做。
def _resolve_wiki_repo() -> Path:
    env = os.environ.get("WIKI_REPO")
    if env:
        return Path(os.path.expanduser(env))
    here = Path(__file__).resolve().parent.parent      # 运行副本时即仓库根
    if (here / "content").is_dir():
        return here
    for cand in ("~/my-wiki-v2", "~/my-wiki", "~/my-wiki-study"):
        p = Path(os.path.expanduser(cand))
        if (p / "content").is_dir():
            return p
    return here                                        # 交给 require_content() 报错


WIKI_REPO = _resolve_wiki_repo()
WIKI_CONTENT = WIKI_REPO / "content"
WIKI_RAW = WIKI_REPO / "raw"
WIKI_PUBLIC = WIKI_REPO / "public"

# 发布目标。**不写死** —— 不同机器管不同的 Wiki，各自发到各自的地方。
# 缺省留空表示「本机不发布，只做本地索引和查询」，而不是悄悄发到别人的桶里。
WIKI_GCS = os.environ.get("WIKI_GCS", "")          # 例: gs://YOUR_BUCKET/cc-pages/wiki-v2/
WIKI_URL = os.environ.get("WIKI_URL", "")          # 例: https://wiki.example.com/wiki-v2


def require_content(exit_on_error: bool = True) -> bool:
    """确认 WIKI_REPO 指向的目录真的是个 Wiki；不是就**明确报错**而不是返回空结果。

    在任何要读内容的入口（query / lint / status / ingest）开头调用。
    没有这道检查时，指错目录的表现是「查什么都没有」——跟「Wiki 里确实没这条」
    长得一模一样，最难排查。宁可吵闹地失败。
    """
    if WIKI_CONTENT.is_dir():
        return True
    msg = [
        f"[wiki] WIKI_REPO 指向 {WIKI_REPO}，但其下没有 content/ 目录。",
        f"       当前取值来源: {'环境变量 WIKI_REPO' if os.environ.get('WIKI_REPO') else '自动探测（未设环境变量）'}",
        "       修法：export WIKI_REPO=/path/to/your/wiki，或在 ~/.claude/settings.json 的 env 里配置。",
        "       注意本文件若是 CloseCrab 仓库内的分发副本，自动探测无法定位到 Wiki，必须显式设置。",
    ]
    print("\n".join(msg), file=sys.stderr)
    if exit_on_error:
        sys.exit(2)
    return False

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
