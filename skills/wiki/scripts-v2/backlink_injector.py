#!/usr/bin/env python3
"""backlink_injector.py — Write backlinks to only affected pages.

Instead of rewriting ALL pages' backlinks (O(N)), this module only touches
pages whose backlinks have actually changed due to added/removed links.
"""
import html as _html
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from wiki_utils import WIKI_REPO

WIKI_DIR = WIKI_REPO / "wiki"


def build_backlinks_html(
    backrefs: list[tuple[str, str, str]], page_path: Path
) -> str:
    """Build backlinks section HTML for a page.

    Args:
        backrefs: [(source_slug, source_title, source_path), ...]
        page_path: Absolute path to the page being updated (for relative path calc).

    Returns:
        HTML string for the backlinks section, or "" if no backrefs.
    """
    if not backrefs:
        return ""

    items = []
    this_dir = page_path.parent
    for _, title, path in backrefs:
        target = WIKI_DIR / path
        try:
            rel_path = os.path.relpath(target, this_dir)
        except ValueError:
            rel_path = path
        items.append(
            f'<li><a class="wiki-link" href="{rel_path}">'
            f'{_html.escape(title)}</a></li>'
        )

    return (
        '<section class="wiki-backlinks" data-pagefind-ignore="">\n'
        '<h3>引用了此页面的页面</h3>\n'
        '<ul>\n' + '\n'.join(items) + '\n</ul>\n'
        '</section>'
    )


def inject_backlinks_for(
    slugs: set[str],
    backlinks: dict[str, list[tuple[str, str, str]]],
    slug_to_path: dict[str, str],
) -> int:
    """Rewrite backlinks for only the specified slugs.

    Args:
        slugs: Set of slugs whose backlinks need rewriting.
        backlinks: Full reverse index {target_slug: [(src_slug, src_title, src_path)]}.
        slug_to_path: Mapping {slug: relative_path} for all pages.

    Returns:
        Number of pages actually written.
    """
    updated = 0

    for slug in slugs:
        rel_path = slug_to_path.get(slug)
        if not rel_path:
            continue

        page_path = WIKI_DIR / rel_path
        if not page_path.exists():
            continue

        backrefs = backlinks.get(slug, [])
        new_section = build_backlinks_html(backrefs, page_path)

        content = page_path.read_text(encoding="utf-8")

        # Remove existing backlinks section
        cleaned = re.sub(
            r'<section class="wiki-backlinks"[^>]*>.*?</section>',
            '', content, flags=re.DOTALL
        ).rstrip('\n')

        # Insert after </main>
        if new_section:
            if "</main>" in cleaned:
                cleaned = cleaned.replace("</main>", "</main>\n" + new_section, 1)
            else:
                cleaned += "\n" + new_section

        if cleaned != content:
            page_path.write_text(cleaned, encoding="utf-8")
            updated += 1

    return updated


def inject_all_backlinks(
    backlinks: dict[str, list[tuple[str, str, str]]],
    slug_to_path: dict[str, str],
) -> int:
    """Rewrite backlinks for ALL pages (full rebuild fallback)."""
    all_slugs = set(slug_to_path.keys())
    return inject_backlinks_for(all_slugs, backlinks, slug_to_path)
