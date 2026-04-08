#!/usr/bin/env python3
"""rebuild-search-page.py — Generate search.html with Pagefind UI.

Creates a dedicated search page with Pagefind's search UI, supporting
full-text search, type/tag filters, and result highlighting.
"""
import os
from pathlib import Path

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
WIKI_DIR = WIKI_REPO / "wiki"


def build_search_html() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Search — CC Wiki</title>
<link rel="stylesheet" href="style.css">
<link rel="stylesheet" href="_pagefind/pagefind-ui.css">
<style>
  .search-container { margin: 24px 0; }
  :root {
    --pagefind-ui-scale: 1;
    --pagefind-ui-primary: var(--blue);
    --pagefind-ui-text: var(--text);
    --pagefind-ui-background: var(--surface);
    --pagefind-ui-border: var(--border);
    --pagefind-ui-tag: var(--blue-light);
    --pagefind-ui-border-width: 1px;
    --pagefind-ui-border-radius: 4px;
    --pagefind-ui-image-border-radius: 4px;
    --pagefind-ui-image-box-ratio: 0;
    --pagefind-ui-font: 'Google Sans', 'Noto Sans SC', 'Roboto', sans-serif;
  }
  .pagefind-ui__search-input {
    font-size: 14px !important;
    padding: 10px 12px 10px 44px !important;
    border: 1px solid var(--border) !important;
    border-radius: 4px !important;
    background: var(--surface) !important;
    transition: border-color 0.2s, box-shadow 0.2s !important;
  }
  .pagefind-ui__search-input:focus {
    border-color: var(--blue) !important;
    box-shadow: 0 0 0 2px var(--blue-light) !important;
    outline: none !important;
  }
  .pagefind-ui__search-clear {
    border-radius: 4px !important;
    background: var(--bg) !important;
    color: var(--text2) !important;
    border: none !important;
  }
  .pagefind-ui__search-clear:hover { background: var(--border) !important; }
  .pagefind-ui__filter-panel {
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
    padding: 12px 16px !important;
    margin-bottom: 12px !important;
  }
  .pagefind-ui__filter-name {
    font-weight: 500 !important;
    color: var(--text) !important;
    text-transform: uppercase !important;
    font-size: 11px !important;
    letter-spacing: 0.5px !important;
  }
  .pagefind-ui__filter-value {
    border-radius: 4px !important;
    padding: 4px 8px !important;
    font-size: 12px !important;
  }
  .pagefind-ui__filter-value:hover { background: var(--blue-light) !important; }
  .pagefind-ui__filter-value.pagefind-ui__filter-value--selected {
    background: var(--blue) !important;
    color: var(--surface) !important;
  }
  .pagefind-ui__result {
    border-bottom: 1px solid var(--bg) !important;
    padding: 12px 0 !important;
  }
  .pagefind-ui__result-link {
    color: var(--pagefind-ui-primary) !important;
    font-weight: 500 !important;
  }
  .pagefind-ui__result-excerpt {
    color: var(--text2) !important;
    line-height: 1.6 !important;
  }
  .pagefind-ui__result-tag {
    background: var(--blue-light) !important;
    color: var(--blue) !important;
    border-radius: 4px !important;
    font-size: 11px !important;
  }
  mark {
    background: rgba(26,115,232,0.15) !important;
    color: inherit !important;
    border-radius: 2px;
    padding: 0 2px;
  }
  .pagefind-ui__message {
    color: var(--text2) !important;
    font-size: 13px !important;
  }
  .pagefind-ui__button {
    background: var(--blue) !important;
    color: var(--surface) !important;
    border: none !important;
    border-radius: 4px !important;
    padding: 8px 20px !important;
    font-weight: 500 !important;
    cursor: pointer !important;
    font-family: 'Google Sans', sans-serif !important;
    transition: background 0.15s !important;
  }
  .pagefind-ui__button:hover { background: var(--blue-hover) !important; }
  .search-tips {
    margin-top: 20px;
    padding: 16px 20px;
    font-size: 13px;
    color: var(--text2);
  }
  .search-tips code { font-size: 12px; }
  .search-shortcut {
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--pagefind-ui-primary);
    color: var(--surface);
    border: none;
    border-radius: 4px;
    padding: 8px 16px;
    font-size: 12px;
    cursor: pointer;
    box-shadow: var(--shadow-2);
    display: none;
    transition: transform 0.2s ease;
  }
  .search-shortcut:hover { transform: translateY(-2px); }
</style>
</head>
<body>
<script src="wiki-shell.js"></script>
<article class="wiki-content">
  <h1>Search</h1>
  <p class="wiki-summary">Full-text search across all wiki pages. Supports Chinese and English.</p>

  <div class="search-container">
    <div id="search"></div>
  </div>

  <div class="search-tips">
    <strong>Search Tips</strong>
    <ul style="margin-top:0.5rem; padding-left:1.2rem;">
      <li>Use filters on the left to narrow by <strong>type</strong> (source/entity/concept) or <strong>tag</strong></li>
      <li>Search supports Chinese — try <code>训练框架</code> or <code>TPU v7</code></li>
      <li>Use quotes for exact phrases: <code>"knowledge compounding"</code></li>
      <li>Press <kbd>Ctrl+K</kbd> / <kbd>Cmd+K</kbd> on any page to quick-search</li>
    </ul>
  </div>
</article>
<script src="_pagefind/pagefind-ui.js"></script>
<script>
  window.addEventListener('DOMContentLoaded', () => {
    new PagefindUI({
      element: "#search",
      showSubResults: true,
      showImages: false,
      excerptLength: 20,
      resetStyles: false,
      translations: {
        placeholder: "Search wiki pages...",
        zero_results: "No results found for [SEARCH_TERM]",
        many_results: "[COUNT] results",
        one_result: "1 result",
        filters: "Filters",
        load_more: "Load more results",
      }
    });

    // Auto-focus search input
    const input = document.querySelector('.pagefind-ui__search-input');
    if (input) input.focus();

    // Read query from URL
    const params = new URLSearchParams(window.location.search);
    const q = params.get('q');
    if (q && input) {
      input.value = q;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
</script>
</body>
</html>"""


def main():
    if not WIKI_DIR.exists():
        print(f"Error: Wiki directory not found at {WIKI_DIR}")
        return

    search_path = WIKI_DIR / "search.html"
    search_path.write_text(build_search_html(), encoding="utf-8")
    print(f"Wrote {search_path}")


if __name__ == "__main__":
    main()
