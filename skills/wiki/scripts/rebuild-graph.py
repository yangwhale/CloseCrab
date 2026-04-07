#!/usr/bin/env python3
"""rebuild-graph.py — Scan wiki/ HTML files and rebuild graph.json + graph.html.

Reads wiki-* meta tags from each HTML to build the knowledge graph data,
then generates an interactive D3.js visualization.
"""
import os
import re
import json
from datetime import datetime, timezone
from pathlib import Path
from html.parser import HTMLParser

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
WIKI_DIR = WIKI_REPO / "wiki"
DATA_DIR = WIKI_REPO / "wiki-data"

SKIP_FILES = {"index.html", "log.html", "graph.html", "style.css", "overview.html"}

TYPE_COLORS = {
    "source": "#F59E0B",
    "entity": "#0EA5E9",
    "concept": "#10B981",
    "analysis": "#F43F5E",
}


class WikiMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            d = dict(attrs)
            name = d.get("name", "")
            if name.startswith("wiki-"):
                self.meta[name] = d.get("content", "")
        elif tag == "title":
            self._in_title = True

    def handle_data(self, data):
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False


class LinkExtractor(HTMLParser):
    """Extract wiki-link hrefs from HTML body."""
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            d = dict(attrs)
            cls = d.get("class", "")
            href = d.get("href", "")
            if "wiki-link" in cls and href:
                # Extract slug from path like ../concepts/rag.html
                match = re.search(r'(\w[\w-]*)\.html$', href)
                if match:
                    self.links.append(match.group(1))


def scan_pages():
    """Scan all wiki HTML pages and extract metadata + links."""
    nodes = []
    links = []

    for html_file in WIKI_DIR.rglob("*.html"):
        rel = html_file.relative_to(WIKI_DIR)
        if rel.name in SKIP_FILES or str(rel).startswith("."):
            continue

        try:
            content = html_file.read_text(encoding="utf-8")
        except Exception:
            continue

        # Parse meta
        meta_parser = WikiMetaParser()
        try:
            meta_parser.feed(content)
        except Exception:
            continue

        if not meta_parser.meta.get("wiki-type"):
            continue

        slug = html_file.stem
        title = meta_parser.title.replace(" — CC Wiki", "").strip()
        tags = [t.strip() for t in meta_parser.meta.get("wiki-tags", "").split(",") if t.strip()]

        nodes.append({
            "id": slug,
            "title": title,
            "type": meta_parser.meta.get("wiki-type", ""),
            "path": str(rel),
            "tags": tags,
            "created": meta_parser.meta.get("wiki-created", ""),
            "updated": meta_parser.meta.get("wiki-updated", ""),
            "source_count": int(meta_parser.meta.get("wiki-sources", "0") or "0"),
        })

        # Parse links
        link_parser = LinkExtractor()
        try:
            link_parser.feed(content)
        except Exception:
            continue

        for target_slug in link_parser.links:
            links.append({
                "source": slug,
                "target": target_slug,
                "type": "mentions",
            })

    return nodes, links


def build_graph_json(nodes, links):
    """Build graph.json."""
    # Deduplicate links
    seen = set()
    unique_links = []
    for link in links:
        key = (link["source"], link["target"])
        if key not in seen:
            seen.add(key)
            unique_links.append(link)

    # Filter links to only reference existing nodes
    node_ids = {n["id"] for n in nodes}
    valid_links = [l for l in unique_links if l["source"] in node_ids and l["target"] in node_ids]

    return {
        "meta": {
            "updated": datetime.now(timezone.utc).isoformat(),
            "node_count": len(nodes),
            "link_count": len(valid_links),
        },
        "nodes": nodes,
        "links": valid_links,
    }


def build_graph_html():
    """Generate graph.html with D3.js visualization."""
    url_prefix = os.environ.get("CC_PAGES_URL_PREFIX", "https://cc.higcp.com")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Knowledge Graph — CC Wiki</title>
<link rel="stylesheet" href="style.css">
<style>
  #graph-container {{ width: 100%; height: 70vh; border-radius: 16px; background: rgba(255,255,255,0.6); border: 1px solid rgba(255,255,255,0.3); margin: 1rem 0; position: relative; overflow: hidden; }}
  #graph-container svg {{ width: 100%; height: 100%; }}
  .graph-legend {{ display: flex; gap: 1.5rem; margin: 0.5rem 0; flex-wrap: wrap; }}
  .graph-legend-item {{ display: flex; align-items: center; gap: 0.4rem; font-size: 0.85rem; color: #475569; }}
  .graph-legend-dot {{ width: 12px; height: 12px; border-radius: 50%; }}
  .graph-tooltip {{ position: absolute; background: rgba(15,23,42,0.9); color: white; padding: 0.5rem 0.8rem; border-radius: 8px; font-size: 0.8rem; pointer-events: none; opacity: 0; transition: opacity 0.15s; max-width: 250px; }}
  .graph-filters {{ display: flex; gap: 0.5rem; margin: 0.5rem 0; flex-wrap: wrap; }}
  .graph-filter {{ padding: 0.3rem 0.8rem; border-radius: 8px; border: 1px solid #E2E8F0; font-size: 0.8rem; cursor: pointer; background: white; }}
  .graph-filter.active {{ background: #8B5CF6; color: white; border-color: #8B5CF6; }}
</style>
</head>
<body>
<nav class="wiki-nav">
  <a href="index.html">Index</a>
  <a href="graph.html" class="active">Graph</a>
  <a href="log.html">Log</a>
</nav>
<article class="wiki-content">
  <h1>Knowledge Graph</h1>

  <div class="graph-legend">
    <div class="graph-legend-item"><div class="graph-legend-dot" style="background:#0EA5E9"></div>Entity</div>
    <div class="graph-legend-item"><div class="graph-legend-dot" style="background:#10B981"></div>Concept</div>
    <div class="graph-legend-item"><div class="graph-legend-dot" style="background:#F59E0B"></div>Source</div>
    <div class="graph-legend-item"><div class="graph-legend-dot" style="background:#F43F5E"></div>Analysis</div>
  </div>

  <div class="graph-filters" id="filters"></div>

  <div id="graph-container">
    <div class="graph-tooltip" id="tooltip"></div>
  </div>
</article>
<footer class="wiki-footer">CC Wiki · Maintained by CloseCrab Bot</footer>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const TYPE_COLORS = {{ source: "#F59E0B", entity: "#0EA5E9", concept: "#10B981", analysis: "#F43F5E" }};
const DATA_URL = "../wiki-data/graph.json";

async function init() {{
  const resp = await fetch(DATA_URL);
  if (!resp.ok) {{ document.getElementById('graph-container').innerHTML = '<p style="padding:2rem;color:#94A3B8">No graph data yet. Use /wiki ingest to add content.</p>'; return; }}
  const data = await resp.json();
  if (!data.nodes.length) {{ document.getElementById('graph-container').innerHTML = '<p style="padding:2rem;color:#94A3B8">Empty graph. Use /wiki ingest to add content.</p>'; return; }}
  renderGraph(data);
}}

function renderGraph(data) {{
  const container = document.getElementById('graph-container');
  const width = container.clientWidth;
  const height = container.clientHeight;
  const tooltip = document.getElementById('tooltip');

  const svg = d3.select(container).append('svg')
    .attr('viewBox', [0, 0, width, height]);

  const g = svg.append('g');

  // Zoom
  svg.call(d3.zoom().scaleExtent([0.3, 5]).on('zoom', (e) => g.attr('transform', e.transform)));

  // Simulation
  const sim = d3.forceSimulation(data.nodes)
    .force('link', d3.forceLink(data.links).id(d => d.id).distance(80))
    .force('charge', d3.forceManyBody().strength(-200))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius(25));

  // Links
  const link = g.append('g').selectAll('line')
    .data(data.links).join('line')
    .attr('stroke', '#CBD5E1').attr('stroke-width', 1).attr('stroke-opacity', 0.6);

  // Nodes
  const node = g.append('g').selectAll('circle')
    .data(data.nodes).join('circle')
    .attr('r', d => 6 + (data.links.filter(l => l.source.id === d.id || l.target.id === d.id).length || 0) * 1.5)
    .attr('fill', d => TYPE_COLORS[d.type] || '#64748B')
    .attr('stroke', '#fff').attr('stroke-width', 1.5)
    .style('cursor', 'pointer')
    .call(d3.drag().on('start', dragStart).on('drag', dragging).on('end', dragEnd));

  // Labels
  const label = g.append('g').selectAll('text')
    .data(data.nodes).join('text')
    .text(d => d.title.length > 20 ? d.title.slice(0, 18) + '...' : d.title)
    .attr('font-size', '9px').attr('fill', '#475569').attr('text-anchor', 'middle').attr('dy', -12);

  // Interactions
  node.on('mouseover', (e, d) => {{
    tooltip.style.opacity = 1;
    tooltip.innerHTML = `<strong>${{d.title}}</strong><br>${{d.type}} · ${{d.tags.join(', ')}}<br>Created: ${{d.created}}`;
  }}).on('mousemove', (e) => {{
    tooltip.style.left = (e.offsetX + 15) + 'px';
    tooltip.style.top = (e.offsetY - 10) + 'px';
  }}).on('mouseout', () => {{ tooltip.style.opacity = 0; }})
  .on('click', (e, d) => {{ window.location.href = d.path; }});

  sim.on('tick', () => {{
    link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    node.attr('cx', d => d.x).attr('cy', d => d.y);
    label.attr('x', d => d.x).attr('y', d => d.y);
  }});

  function dragStart(e, d) {{ if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }}
  function dragging(e, d) {{ d.fx = e.x; d.fy = e.y; }}
  function dragEnd(e, d) {{ if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }}
}}

init();
</script>
</body>
</html>""";


def main():
    if not WIKI_DIR.exists():
        print(f"Error: Wiki directory not found at {WIKI_DIR}")
        return

    nodes, links = scan_pages()
    print(f"Found {len(nodes)} nodes, {len(links)} links")

    # Write graph.json
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    graph_data = build_graph_json(nodes, links)
    graph_json_path = DATA_DIR / "graph.json"
    graph_json_path.write_text(json.dumps(graph_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {graph_json_path}")

    # Write graph.html
    graph_html_path = WIKI_DIR / "graph.html"
    graph_html_path.write_text(build_graph_html(), encoding="utf-8")
    print(f"Wrote {graph_html_path}")


if __name__ == "__main__":
    main()
