#!/usr/bin/env python3
"""rebuild-health.py — Generate wiki/health.html health dashboard.

Reads graph.json, log.json, lint-report.json, and compile-manifest.json
to produce a visual health dashboard with:
  - Overview cards (pages, links, health score)
  - Type distribution (SVG donut chart)
  - Activity timeline (last 30 days from log.json)
  - Issues summary
  - Knowledge discovery suggestions
"""
import html
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import WIKI_REPO, TYPE_COLORS, TYPE_ORDER

WIKI_DIR = WIKI_REPO / "wiki"
DATA_DIR = WIKI_REPO / "wiki-data"
WIKI_URL = os.environ.get("CC_PAGES_URL_PREFIX", "https://cc.higcp.com") + "/wiki"


def load_json(path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def compute_health_score(graph, lint_report):
    """Compute health score 0-100."""
    score = 100.0
    nodes = graph.get("nodes", [])
    links = graph.get("links", [])

    if not nodes:
        return 0

    # Orphan penalty — source pages are leaf nodes by nature, lower weight
    inbound = defaultdict(int)
    for link in links:
        inbound[link["target"]] += 1
    for n in nodes:
        if inbound[n["id"]] == 0:
            if n["type"] == "source":
                score -= 0.1  # sources are naturally terminal
            else:
                score -= 2.0  # entity/concept/analysis orphans are serious

    # Use lint report if available
    if lint_report:
        score -= len(lint_report.get("html_broken_links", [])) * 2
        score -= lint_report.get("missing_backlinks", 0) * 0.3
        score -= len(lint_report.get("content_issues", [])) * 1

    # Bonus for diversity (having all 4 types)
    types_present = {n["type"] for n in nodes}
    if len(types_present) >= 4:
        score += 2

    # Bonus for good link density
    link_ratio = len(links) / len(nodes) if nodes else 0
    if link_ratio >= 1.5:
        score += 3

    return max(0, min(100, round(score)))


def get_type_counts(nodes):
    counts = defaultdict(int)
    for n in nodes:
        counts[n["type"]] += 1
    return counts


def get_activity_data(log_data, days=30):
    """Extract daily activity counts for the last N days."""
    if not log_data:
        return []

    entries = log_data.get("entries", []) if isinstance(log_data, dict) else log_data
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    daily = defaultdict(int)

    for entry in entries:
        ts_str = entry.get("timestamp", entry.get("time", ""))
        if not ts_str:
            continue
        try:
            day = ts_str[:10]
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts >= cutoff:
                daily[day] += 1
        except (ValueError, IndexError):
            continue

    # Fill gaps
    result = []
    for i in range(days):
        d = (datetime.now(timezone.utc) - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        result.append({"date": d, "count": daily.get(d, 0)})

    return result


def get_recent_entries(log_data, n=10):
    if not log_data:
        return []
    entries = log_data.get("entries", []) if isinstance(log_data, dict) else log_data
    return entries[-n:]


def detect_orphans(nodes, links):
    inbound = defaultdict(int)
    for link in links:
        inbound[link["target"]] += 1
    return [n for n in nodes if inbound[n["id"]] == 0]


def detect_short_pages(nodes):
    short = []
    for n in nodes:
        if n["type"] != "source":
            continue
        path = WIKI_DIR / n["path"]
        if not path.exists():
            short.append({"id": n["id"], "title": n["title"], "reason": "missing"})
        elif path.stat().st_size < 2000:
            short.append({"id": n["id"], "title": n["title"], "reason": "short",
                          "size": path.stat().st_size})
    return short


def detect_stale_pages(nodes, days=30):
    """Find pages not updated in N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    stale = []
    for n in nodes:
        updated = n.get("updated", n.get("created", ""))
        if updated and updated < cutoff:
            stale.append({"id": n["id"], "title": n["title"], "type": n["type"],
                          "updated": updated})
    return stale


def build_donut_svg(type_counts, total):
    """Build an SVG donut chart for type distribution."""
    if total == 0:
        return '<svg width="200" height="200"></svg>'

    cx, cy, r = 100, 100, 80
    inner_r = 50
    segments = []
    offset = 0

    for t in TYPE_ORDER:
        count = type_counts.get(t, 0)
        if count == 0:
            continue
        pct = count / total
        angle = pct * 360

        # SVG arc
        start_rad = math.radians(offset - 90)
        end_rad = math.radians(offset + angle - 90)

        x1_out = cx + r * math.cos(start_rad)
        y1_out = cy + r * math.sin(start_rad)
        x2_out = cx + r * math.cos(end_rad)
        y2_out = cy + r * math.sin(end_rad)

        x1_in = cx + inner_r * math.cos(end_rad)
        y1_in = cy + inner_r * math.sin(end_rad)
        x2_in = cx + inner_r * math.cos(start_rad)
        y2_in = cy + inner_r * math.sin(start_rad)

        large = 1 if angle > 180 else 0
        color = TYPE_COLORS.get(t, "#64748B")

        path = (f'M {x1_out:.1f} {y1_out:.1f} '
                f'A {r} {r} 0 {large} 1 {x2_out:.1f} {y2_out:.1f} '
                f'L {x1_in:.1f} {y1_in:.1f} '
                f'A {inner_r} {inner_r} 0 {large} 0 {x2_in:.1f} {y2_in:.1f} Z')

        segments.append(f'<path d="{path}" fill="{color}" opacity="0.85">'
                        f'<title>{t}: {count} ({pct:.0%})</title></path>')
        offset += angle

    return (f'<svg viewBox="0 0 200 200" width="200" height="200">\n'
            + '\n'.join(segments)
            + f'\n<text x="100" y="95" text-anchor="middle" font-size="28" '
              f'font-weight="600" fill="#202124">{total}</text>'
            f'\n<text x="100" y="115" text-anchor="middle" font-size="12" '
            f'fill="#5f6368">pages</text>'
            f'\n</svg>')


def build_activity_svg(activity_data):
    """Build an SVG bar chart for activity timeline."""
    if not activity_data:
        return ''

    max_count = max((d["count"] for d in activity_data), default=1) or 1
    bar_w = 18
    gap = 2
    total_w = len(activity_data) * (bar_w + gap)
    chart_h = 120
    svg_h = chart_h + 30

    bars = []
    for i, d in enumerate(activity_data):
        x = i * (bar_w + gap)
        h = (d["count"] / max_count) * (chart_h - 10) if d["count"] > 0 else 0
        y = chart_h - h
        color = "#1a73e8" if d["count"] > 0 else "#dadce0"
        opacity = min(0.4 + d["count"] / max_count * 0.6, 1) if d["count"] > 0 else 0.3
        bars.append(
            f'<rect x="{x}" y="{y}" width="{bar_w}" height="{max(h, 2)}" '
            f'rx="3" fill="{color}" opacity="{opacity:.2f}">'
            f'<title>{d["date"]}: {d["count"]} operations</title></rect>')
        # Date labels every 7 days
        if i % 7 == 0:
            label = d["date"][5:]  # MM-DD
            bars.append(
                f'<text x="{x + bar_w / 2}" y="{svg_h - 2}" text-anchor="middle" '
                f'font-size="9" fill="#80868b">{label}</text>')

    return (f'<svg viewBox="0 0 {total_w} {svg_h}" width="100%" '
            f'height="{svg_h}" preserveAspectRatio="xMinYMid meet">\n'
            + '\n'.join(bars) + '\n</svg>')


def score_color(score):
    if score >= 80:
        return "#1e8e3e"  # green
    elif score >= 60:
        return "#f9ab00"  # amber
    else:
        return "#d93025"  # red


def score_label(score):
    if score >= 90:
        return "Excellent"
    elif score >= 80:
        return "Good"
    elif score >= 60:
        return "Fair"
    else:
        return "Needs Attention"


def build_html(score, type_counts, total_pages, total_links,
               activity_data, recent_entries, orphans, short_pages, stale_pages,
               lint_report, manifest):
    """Generate health.html content."""

    donut_svg = build_donut_svg(type_counts, total_pages)
    activity_svg = build_activity_svg(activity_data)
    sc = score_color(score)
    sl = score_label(score)

    # Type legend
    type_legend = ""
    for t in TYPE_ORDER:
        c = type_counts.get(t, 0)
        color = TYPE_COLORS.get(t, "#64748B")
        type_legend += (f'<div class="legend-item">'
                        f'<span class="legend-dot" style="background:{color}"></span>'
                        f'<span>{t.title()}: {c}</span></div>\n')

    # Issues HTML
    issues_html = ""
    total_issues = 0

    if orphans:
        issues_html += f'<h3>Orphan Pages ({len(orphans)})</h3><ul>\n'
        for o in orphans[:15]:
            issues_html += (f'<li><a class="wiki-link" href="{html.escape(o["path"])}">{html.escape(o["title"])}</a> '
                            f'<span class="issue-type">{html.escape(o["type"])}</span></li>\n')
        if len(orphans) > 15:
            issues_html += f'<li>... and {len(orphans) - 15} more</li>\n'
        issues_html += '</ul>\n'
        total_issues += len(orphans)

    if short_pages:
        issues_html += f'<h3>Content Issues ({len(short_pages)})</h3><ul>\n'
        for s in short_pages[:10]:
            reason = "Missing file" if s["reason"] == "missing" else f'{s.get("size", 0)}B'
            issues_html += f'<li><code>{html.escape(s["id"])}</code> — {html.escape(reason)}</li>\n'
        issues_html += '</ul>\n'
        total_issues += len(short_pages)

    if lint_report:
        bl = lint_report.get("html_broken_links", [])
        if bl:
            issues_html += f'<h3>Broken Links ({len(bl)})</h3><ul>\n'
            for b in bl[:10]:
                issues_html += f'<li><code>{html.escape(b["page"])}</code> → {html.escape(b["href"])}</li>\n'
            issues_html += '</ul>\n'
            total_issues += len(bl)

    if not issues_html:
        issues_html = '<p style="color:var(--emerald);font-weight:600">No issues found!</p>'

    # Stale pages
    stale_html = ""
    if stale_pages:
        stale_html = f'<h3>Stale Pages (&gt;30 days, showing top 10)</h3><ul>\n'
        for s in stale_pages[:10]:
            stale_html += (f'<li><code>{html.escape(s["id"])}</code> '
                           f'<span class="issue-type">{html.escape(s["type"])}</span> '
                           f'last updated {html.escape(s["updated"])}</li>\n')
        stale_html += '</ul>\n'

    # Recent activity
    recent_html = ""
    if recent_entries:
        recent_html = '<table><thead><tr><th>Date</th><th>Action</th><th>Title</th></tr></thead><tbody>\n'
        for e in reversed(recent_entries):
            ts = e.get("timestamp", "")[:10]
            action = e.get("action", e.get("operation", "?"))
            title = e.get("title", e.get("slug", ""))[:60]
            recent_html += f'<tr><td>{html.escape(ts)}</td><td>{html.escape(action)}</td><td>{html.escape(title)}</td></tr>\n'
        recent_html += '</tbody></table>\n'

    # Manifest stats
    manifest_html = ""
    if manifest:
        pages = manifest.get("pages", {})
        built_at = manifest.get("built_at", "unknown")[:19]
        manifest_html = (f'<div class="stat-card">'
                         f'<div class="stat-value">{len(pages)}</div>'
                         f'<div class="stat-label">Compiled Pages</div>'
                         f'<div class="stat-sub">Last build: {built_at}</div></div>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Health Dashboard — CC Wiki</title>
<link rel="stylesheet" href="style.css">
<style>
  .dashboard-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin: 20px 0;
  }}
  .score-ring {{
    position: relative;
    display: inline-block;
  }}
  .score-ring svg {{ transform: rotate(-90deg); filter: drop-shadow(0 2px 4px rgba(0,0,0,0.05)); }}
  .score-text {{
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    text-align: center;
  }}
  .chart-section {{ padding: 20px 24px; }}
  .chart-row {{
    display: flex;
    gap: 24px;
    align-items: center;
    flex-wrap: wrap;
  }}
  .legend-items {{ display: flex; flex-direction: column; gap: 8px; }}
  .legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text); }}
  .legend-dot {{ width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }}
  .issues-section ul {{ list-style: none; padding: 0; }}
  .issues-section li {{
    padding: 10px 0;
    border-bottom: 1px solid var(--bg);
    font-size: 13px;
    color: var(--text);
    display: flex;
    align-items: flex-start;
    gap: 8px;
  }}
  .issues-section li:last-child {{ border-bottom: none; }}
  .issue-type {{
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 4px;
    background: var(--bg);
    color: var(--text2);
    white-space: nowrap;
    font-weight: 500;
  }}
  .activity-section {{ overflow-x: auto; }}
  .timestamp {{ font-size: 11px; color: var(--text3); text-align: right; margin-top: 12px; }}
  @media (max-width: 640px) {{
    .chart-row {{ flex-direction: column; align-items: flex-start; }}
  }}
</style>
</head>
<body>
<script src="wiki-shell.js"></script>
<article class="wiki-content">
  <h1>Health Dashboard</h1>
  <p class="wiki-summary">Wiki health overview — {total_pages} pages, {total_links} links</p>

  <!-- Overview Cards -->
  <div class="dashboard-grid">
    <div class="stat-card">
      <div class="score-ring">
        <svg width="120" height="120" viewBox="0 0 120 120">
          <circle cx="60" cy="60" r="52" fill="none" stroke="#dadce0" stroke-width="8"/>
          <circle cx="60" cy="60" r="52" fill="none" stroke="{sc}" stroke-width="8"
                  stroke-dasharray="{score / 100 * 327:.0f} 327"
                  stroke-linecap="round"/>
        </svg>
        <div class="score-text">
          <div class="num" style="color:{sc}">{score}</div>
          <div class="label">{sl}</div>
        </div>
      </div>
      <div class="stat-label">Health Score</div>
    </div>
    <div class="stat-card">
      <div class="stat-value" style="color:var(--blue)">{total_pages}</div>
      <div class="stat-label">Total Pages</div>
    </div>
    <div class="stat-card">
      <div class="stat-value" style="color:var(--blue)">{total_links}</div>
      <div class="stat-label">Knowledge Links</div>
    </div>
    <div class="stat-card">
      <div class="stat-value" style="color:{score_color(100 - total_issues * 2)}">{total_issues}</div>
      <div class="stat-label">Open Issues</div>
    </div>
    {manifest_html}
  </div>

  <!-- Type Distribution -->
  <div class="chart-section">
    <h2>Type Distribution</h2>
    <div class="chart-row">
      {donut_svg}
      <div class="legend-items">
        {type_legend}
      </div>
    </div>
  </div>

  <!-- Activity Timeline -->
  <div class="chart-section activity-section">
    <h2>Activity (Last 30 Days)</h2>
    {activity_svg if activity_svg else '<p class="wiki-empty">No activity data</p>'}
  </div>

  <!-- Recent Activity -->
  <div class="chart-section">
    <h2>Recent Operations</h2>
    {recent_html if recent_html else '<p class="wiki-empty">No log entries</p>'}
  </div>

  <!-- Issues -->
  <div class="chart-section issues-section">
    <h2>Issues</h2>
    {issues_html}
  </div>

  <!-- Stale Pages -->
  {"<div class='chart-section issues-section'>" + stale_html + "</div>" if stale_html else ""}

  <div class="timestamp">
    Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
  </div>
</article>
</body>
</html>"""


def main():
    if not WIKI_DIR.exists():
        print(f"Error: Wiki directory not found at {WIKI_DIR}")
        return

    # Load data
    graph = load_json(DATA_DIR / "graph.json")
    if not graph:
        print("Error: graph.json not found. Run rebuild-graph.py first.")
        return

    log_data = load_json(DATA_DIR / "log.json")
    lint_report = load_json(DATA_DIR / "lint-report.json")
    manifest = load_json(DATA_DIR / "compile-manifest.json")

    nodes = graph.get("nodes", [])
    links = graph.get("links", [])
    total_pages = len(nodes)
    total_links = len(links)

    # Compute metrics
    type_counts = get_type_counts(nodes)
    score = compute_health_score(graph, lint_report)
    activity_data = get_activity_data(log_data, days=30)
    recent_entries = get_recent_entries(log_data, n=10)
    orphans = detect_orphans(nodes, links)
    short_pages = detect_short_pages(nodes)
    stale_pages = detect_stale_pages(nodes, days=30)

    # Generate HTML
    html = build_html(
        score=score,
        type_counts=type_counts,
        total_pages=total_pages,
        total_links=total_links,
        activity_data=activity_data,
        recent_entries=recent_entries,
        orphans=orphans,
        short_pages=short_pages,
        stale_pages=stale_pages,
        lint_report=lint_report,
        manifest=manifest,
    )

    out_path = WIKI_DIR / "health.html"
    out_path.write_text(html, encoding="utf-8")

    # Write score to JSON for other scripts (e.g. rebuild-index.py)
    score_path = DATA_DIR / "health-score.json"
    score_path.write_text(json.dumps({"score": score, "label": score_label(score)},
                                     ensure_ascii=False), encoding="utf-8")

    print(f"Health dashboard: {out_path}")
    print(f"  Score: {score}/100 ({score_label(score)})")
    print(f"  Pages: {total_pages} | Links: {total_links} | Issues: {len(orphans) + len(short_pages)}")


if __name__ == "__main__":
    main()
