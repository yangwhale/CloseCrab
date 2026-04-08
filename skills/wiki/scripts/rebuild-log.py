#!/usr/bin/env python3
"""rebuild-log.py — Generate wiki/log.html from wiki-data/log.json.

Creates a chronological operation log page showing all wiki operations
(ingest, create, lint, etc.) with proper styling and navigation.
"""
import html
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import WIKI_REPO

WIKI_DIR = WIKI_REPO / "wiki"
DATA_DIR = WIKI_REPO / "wiki-data"

# Action → display config
ACTION_STYLES = {
    "ingest":  {"icon": "\U0001F4E5", "color": "var(--blue)",   "label": "Ingest"},
    "create":  {"icon": "\U0001F195", "color": "var(--green)",  "label": "Create"},
    "update":  {"icon": "\u270F\uFE0F",  "color": "var(--orange)", "label": "Update"},
    "delete":  {"icon": "\U0001F5D1\uFE0F",  "color": "var(--red)",    "label": "Delete"},
    "lint":    {"icon": "\U0001F50D", "color": "var(--yellow)", "label": "Lint"},
    "rebuild": {"icon": "\u2699\uFE0F",  "color": "var(--text3)",  "label": "Rebuild"},
    "query":   {"icon": "\u2753", "color": "var(--blue)",   "label": "Query"},
}

DEFAULT_STYLE = {"icon": "\U0001F4CB", "color": "var(--text2)", "label": "Operation"}


def load_log():
    log_path = DATA_DIR / "log.json"
    if not log_path.exists():
        return []
    data = json.loads(log_path.read_text(encoding="utf-8"))
    entries = data.get("entries", []) if isinstance(data, dict) else data
    return entries


def format_timestamp(ts_str):
    """Format ISO timestamp to readable string."""
    if not ts_str:
        return ""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return ts.strftime("%Y-%m-%d %H:%M")
    except (ValueError, IndexError):
        return ts_str[:16]


def build_entry_html(entry):
    """Build HTML for a single log entry."""
    action = entry.get("action", entry.get("operation", "unknown"))
    style = ACTION_STYLES.get(action, DEFAULT_STYLE)
    title = html.escape(entry.get("title", entry.get("slug", action)))
    ts = format_timestamp(entry.get("timestamp", entry.get("time", "")))
    details = entry.get("details", entry.get("description", ""))
    slug = entry.get("slug", "")
    page_type = entry.get("type", "")

    # Build page link if slug exists
    link_html = ""
    if slug and page_type:
        type_dir = {"source": "sources", "entity": "entities",
                    "concept": "concepts", "analysis": "analyses"}.get(page_type, "")
        if type_dir:
            href = html.escape(f"{type_dir}/{slug}.html")
            link_html = f' <a class="wiki-link" href="{href}">View page &rarr;</a>'

    # Pages created/updated lists
    pages_html = ""
    created = entry.get("pages_created", [])
    updated = entry.get("pages_updated", [])
    if created:
        pages_html += '<div style="margin-top:6px;font-size:12px;color:var(--text3)">'
        pages_html += f'Created: {", ".join(html.escape(p) for p in created)}'
        pages_html += '</div>'
    if updated:
        pages_html += '<div style="margin-top:4px;font-size:12px;color:var(--text3)">'
        pages_html += f'Updated: {", ".join(html.escape(p) for p in updated)}'
        pages_html += '</div>'

    # Source URL
    source_html = ""
    source = entry.get("source", "")
    if source:
        esc_source = html.escape(source)
        source_html = (f'<div style="margin-top:4px;font-size:12px">'
                       f'<a href="{esc_source}" style="color:var(--text3)">{esc_source}</a></div>')

    # Type badge
    type_badge = ""
    if page_type:
        type_badge = (f' <span class="wiki-type" data-type="{html.escape(page_type)}">'
                      f'{html.escape(page_type)}</span>')

    return f"""<div class="log-entry">
  <div class="log-header">
    <span class="log-action" style="color:{style['color']}">{style['icon']} {html.escape(style['label'])}</span>
    {type_badge}
    <span class="log-time">{html.escape(ts)}</span>
  </div>
  <div class="log-title">{title}{link_html}</div>
  {f'<div class="log-details">{html.escape(details)}</div>' if details else ''}
  {pages_html}
  {source_html}
</div>"""


def build_html(entries):
    """Generate log.html content."""
    # Reverse chronological order
    sorted_entries = sorted(
        entries,
        key=lambda e: e.get("timestamp", e.get("time", "")),
        reverse=True
    )

    entries_html = "\n".join(build_entry_html(e) for e in sorted_entries)
    if not entries_html:
        entries_html = '<div class="wiki-empty">No log entries yet.</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Operation Log — CC Wiki</title>
<link rel="stylesheet" href="style.css">
<style>
  .log-entry {{
    padding: 16px 20px;
    margin-bottom: 12px;
  }}
  .log-header {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
  }}
  .log-action {{
    font-weight: 600;
    font-size: 13px;
    color: var(--blue);
    background: var(--blue-light);
    padding: 2px 8px;
    border-radius: 4px;
  }}
  .log-time {{
    font-size: 12px;
    color: var(--text3);
    margin-left: auto;
    font-family: 'Roboto Mono', monospace;
  }}
  .log-title {{
    font-size: 15px;
    font-weight: 500;
    color: var(--text);
  }}
  .log-details {{
    font-size: 13px;
    color: var(--text2);
    margin-top: 8px;
    line-height: 1.5;
  }}
  .log-stats {{
    display: flex;
    gap: 24px;
    margin-bottom: 24px;
    font-size: 13px;
    color: var(--text2);
    background: var(--surface);
    padding: 16px 20px;
    border-radius: var(--radius);
    border: 1px solid var(--border);
  }}
  .log-stats strong {{
    color: var(--text);
    font-size: 14px;
    margin-left: 4px;
  }}
</style>
</head>
<body>
<script src="wiki-shell.js"></script>
<article class="wiki-content">
  <h1>Operation Log</h1>
  <p class="wiki-summary">Chronological log of all wiki operations — {len(sorted_entries)} entries</p>

  <div class="log-stats">
    <span>Total: <strong>{len(sorted_entries)}</strong></span>
    <span>Ingests: <strong>{sum(1 for e in entries if e.get('action', e.get('operation', '')) == 'ingest')}</strong></span>
    <span>Creates: <strong>{sum(1 for e in entries if e.get('action', e.get('operation', '')) == 'create')}</strong></span>
  </div>

  <div id="log-entries">
    {entries_html}
  </div>
</article>
</body>
</html>"""


def main():
    if not WIKI_DIR.exists():
        print(f"Error: Wiki directory not found at {{WIKI_DIR}}")
        return

    entries = load_log()
    out_html = build_html(entries)
    out_path = WIKI_DIR / "log.html"
    out_path.write_text(out_html, encoding="utf-8")
    print(f"Log page: {out_path} ({len(entries)} entries)")


if __name__ == "__main__":
    main()
