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
  /* Pagefind UI customization */
  :root {
    --pagefind-ui-scale: 1;
    --pagefind-ui-primary: #1a73e8;
    --pagefind-ui-text: #202124;
    --pagefind-ui-background: #ffffff;
    --pagefind-ui-border: #dadce0;
    --pagefind-ui-tag: #e8f0fe;
    --pagefind-ui-border-width: 1px;
    --pagefind-ui-border-radius: 4px;
    --pagefind-ui-image-border-radius: 4px;
    --pagefind-ui-image-box-ratio: 0;
    --pagefind-ui-font: 'Google Sans', 'Noto Sans SC', 'Roboto', sans-serif;
  }
  /* Search input */
  .pagefind-ui__search-input {
    font-size: 14px !important;
    padding: 10px 12px !important;
    border: 1px solid #dadce0 !important;
    border-radius: 4px !important;
    background: #ffffff !important;
    transition: border-color 0.2s, box-shadow 0.2s !important;
  }
  .pagefind-ui__search-input:focus {
    border-color: #1a73e8 !important;
    box-shadow: 0 0 0 2px #e8f0fe !important;
    outline: none !important;
  }
  /* Search clear button */
  .pagefind-ui__search-clear {
    border-radius: 4px !important;
    background: #f1f3f4 !important;
    color: #5f6368 !important;
    border: none !important;
  }
  .pagefind-ui__search-clear:hover { background: #dadce0 !important; }
  /* Filter panel */
  .pagefind-ui__filter-panel {
    background: #ffffff !important;
    border: 1px solid #dadce0 !important;
    border-radius: 8px !important;
    padding: 12px 16px !important;
    margin-bottom: 12px !important;
  }
  .pagefind-ui__filter-name {
    font-weight: 500 !important;
    color: #202124 !important;
    text-transform: uppercase !important;
    font-size: 11px !important;
    letter-spacing: 0.5px !important;
  }
  .pagefind-ui__filter-value {
    border-radius: 4px !important;
    padding: 4px 8px !important;
    font-size: 12px !important;
  }
  .pagefind-ui__filter-value:hover { background: #e8f0fe !important; }
  .pagefind-ui__filter-value.pagefind-ui__filter-value--selected {
    background: #1a73e8 !important;
    color: white !important;
  }
  /* Result styling */
  .pagefind-ui__result {
    border-bottom: 1px solid #f1f3f4 !important;
    padding: 12px 0 !important;
  }
  .pagefind-ui__result-link {
    color: var(--pagefind-ui-primary) !important;
    font-weight: 500 !important;
  }
  .pagefind-ui__result-excerpt {
    color: #3c4043 !important;
    line-height: 1.6 !important;
  }
  .pagefind-ui__result-tag {
    background: #e8f0fe !important;
    color: #1a73e8 !important;
    border-radius: 4px !important;
    font-size: 11px !important;
  }
  /* Highlight marks */
  mark {
    background: rgba(26,115,232,0.15) !important;
    color: inherit !important;
    border-radius: 2px;
    padding: 0 2px;
  }
  /* Message / loading */
  .pagefind-ui__message {
    color: #5f6368 !important;
    font-size: 13px !important;
  }
  /* Load more button */
  .pagefind-ui__button {
    background: #1a73e8 !important;
    color: white !important;
    border: none !important;
    border-radius: 4px !important;
    padding: 8px 20px !important;
    font-weight: 500 !important;
    cursor: pointer !important;
    font-family: 'Google Sans', sans-serif !important;
  }
  .pagefind-ui__button:hover { background: #1765cc !important; }
  .search-tips {
    margin-top: 20px;
    padding: 16px 20px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    font-size: 13px;
    color: #5f6368;
  }
  .search-tips code { font-size: 12px; }
  .search-shortcut {
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--pagefind-ui-primary);
    color: white;
    border: none;
    border-radius: 4px;
    padding: 8px 16px;
    font-size: 12px;
    cursor: pointer;
    box-shadow: var(--shadow-1);
    display: none;
  }
</style>
</head>
<body>
<nav class="wiki-nav">
  <a href="index.html">Index</a>
  <a href="search.html" class="active">Search</a>
  <a href="graph.html">Graph</a>
  <a href="log.html">Log</a>
</nav>
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
<footer class="wiki-footer">CC Wiki · Maintained by CloseCrab Bot</footer>

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
