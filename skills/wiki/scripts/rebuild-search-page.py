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
  .search-container { margin: 2rem 0; }
  /* Pagefind UI customization */
  :root {
    --pagefind-ui-scale: 1;
    --pagefind-ui-primary: #8B5CF6;
    --pagefind-ui-text: #334155;
    --pagefind-ui-background: rgba(255,255,255,0.6);
    --pagefind-ui-border: #E2E8F0;
    --pagefind-ui-tag: #F5F3FF;
    --pagefind-ui-border-width: 1px;
    --pagefind-ui-border-radius: 12px;
    --pagefind-ui-image-border-radius: 8px;
    --pagefind-ui-image-box-ratio: 0;
    --pagefind-ui-font: 'Inter', 'Noto Sans SC', sans-serif;
  }
  /* Search input — thin border, soft shadow */
  .pagefind-ui__search-input {
    font-size: 1rem !important;
    padding: 0.8rem 1.2rem !important;
    border: 1px solid #E2E8F0 !important;
    border-radius: 12px !important;
    background: rgba(255,255,255,0.7) !important;
    box-shadow: 0 2px 8px rgba(0,0,0,0.04) !important;
    transition: border-color 0.2s, box-shadow 0.2s !important;
  }
  .pagefind-ui__search-input:focus {
    border-color: #8B5CF6 !important;
    box-shadow: 0 0 0 3px rgba(139,92,246,0.1) !important;
    outline: none !important;
  }
  /* Search clear button */
  .pagefind-ui__search-clear {
    border-radius: 8px !important;
    background: #F1F5F9 !important;
    color: #64748B !important;
    border: none !important;
  }
  .pagefind-ui__search-clear:hover { background: #E2E8F0 !important; }
  /* Filter panel — glassmorphism card */
  .pagefind-ui__filter-panel {
    background: rgba(255,255,255,0.5) !important;
    backdrop-filter: blur(12px) !important;
    border: 1px solid rgba(255,255,255,0.3) !important;
    border-radius: 12px !important;
    padding: 1rem 1.2rem !important;
    margin-bottom: 1rem !important;
  }
  .pagefind-ui__filter-name {
    font-weight: 600 !important;
    color: #334155 !important;
    text-transform: uppercase !important;
    font-size: 0.75rem !important;
    letter-spacing: 0.05em !important;
  }
  .pagefind-ui__filter-value {
    border-radius: 8px !important;
    padding: 0.25rem 0.6rem !important;
    font-size: 0.8rem !important;
  }
  .pagefind-ui__filter-value:hover { background: #F5F3FF !important; }
  .pagefind-ui__filter-value.pagefind-ui__filter-value--selected {
    background: #8B5CF6 !important;
    color: white !important;
  }
  /* Result styling */
  .pagefind-ui__result {
    border-bottom: 1px solid #F1F5F9 !important;
    padding: 1rem 0 !important;
  }
  .pagefind-ui__result-link {
    color: var(--pagefind-ui-primary) !important;
    font-weight: 600 !important;
  }
  .pagefind-ui__result-excerpt {
    color: #475569 !important;
    line-height: 1.6 !important;
  }
  .pagefind-ui__result-tag {
    background: #F5F3FF !important;
    color: #7C3AED !important;
    border-radius: 6px !important;
    font-size: 0.75rem !important;
  }
  /* Highlight marks */
  mark {
    background: rgba(139,92,246,0.15) !important;
    color: inherit !important;
    border-radius: 2px;
    padding: 0 2px;
  }
  /* Message / loading */
  .pagefind-ui__message {
    color: #64748B !important;
    font-size: 0.85rem !important;
  }
  /* Load more button */
  .pagefind-ui__button {
    background: #8B5CF6 !important;
    color: white !important;
    border: none !important;
    border-radius: 10px !important;
    padding: 0.6rem 1.5rem !important;
    font-weight: 500 !important;
    cursor: pointer !important;
  }
  .pagefind-ui__button:hover { background: #7C3AED !important; }
  .search-tips {
    margin-top: 1.5rem;
    padding: 1rem 1.5rem;
    background: var(--glass-bg);
    border: 1px solid var(--glass-border);
    border-radius: 12px;
    font-size: 0.85rem;
    color: #64748B;
  }
  .search-tips code { font-size: 0.8rem; }
  .search-shortcut {
    position: fixed;
    bottom: 2rem;
    right: 2rem;
    background: var(--pagefind-ui-primary);
    color: white;
    border: none;
    border-radius: 12px;
    padding: 0.6rem 1rem;
    font-size: 0.8rem;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(139,92,246,0.3);
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
