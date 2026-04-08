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

# Allow running from any directory
import sys
sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import WikiMetaParser, SKIP_FILES

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
WIKI_DIR = WIKI_REPO / "wiki"
DATA_DIR = WIKI_REPO / "wiki-data"


class LinkExtractor(HTMLParser):
    """Extract wiki-link hrefs from HTML body."""
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            d = dict(attrs)
            cls = d.get("class") or ""
            href = d.get("href") or ""
            if "wiki-link" in cls and href:
                # Extract slug from path like ../concepts/rag.html
                match = re.search(r'([\w][\w.-]*)\.html$', href)
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

        # Parse meta + summary
        meta_parser = WikiMetaParser()
        try:
            meta_parser.feed(content)
        except Exception:
            continue

        if not meta_parser.meta.get("wiki-type"):
            continue

        slug = html_file.stem
        title = meta_parser.clean_title()
        tags = [t.strip() for t in meta_parser.meta.get("wiki-tags", "").split(",") if t.strip()]

        nodes.append({
            "id": slug,
            "title": title,
            "type": meta_parser.meta.get("wiki-type", ""),
            "path": str(rel),
            "tags": tags,
            "summary": meta_parser.summary.strip(),
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


def inject_backlinks(nodes, links):
    """Update each page's backlinks section based on actual wiki-link references."""
    node_map = {n["id"]: n for n in nodes}

    # Build reverse index: target_slug -> [(source_slug, source_title, source_path)]
    reverse = {}
    seen = set()
    for link in links:
        key = (link["source"], link["target"])
        if key in seen:
            continue
        seen.add(key)
        src = node_map.get(link["source"])
        if src and link["target"] in node_map:
            reverse.setdefault(link["target"], []).append(
                (src["id"], src["title"], src["path"])
            )

    updated = 0
    for node in nodes:
        slug = node["id"]
        page_path = WIKI_DIR / node["path"]
        if not page_path.exists():
            continue

        backrefs = reverse.get(slug, [])
        # Sort by title for stable output
        backrefs.sort(key=lambda x: x[1])

        # Build new backlinks HTML
        if backrefs:
            items = []
            for _, title, path in backrefs:
                # Compute relative path from this page to the linking page
                this_dir = page_path.parent
                target = WIKI_DIR / path
                try:
                    rel_path = os.path.relpath(target, this_dir)
                except ValueError:
                    rel_path = path
                import html as _html
                items.append(f'<li><a class="wiki-link" href="{rel_path}">{_html.escape(title)}</a></li>')
            backlinks_html = (
                '<section class="wiki-backlinks" data-pagefind-ignore="">\n'
                '<h3>引用了此页面的页面</h3>\n'
                '<ul>\n' + '\n'.join(items) + '\n</ul>\n'
                '</section>'
            )
        else:
            backlinks_html = ""

        content = page_path.read_text(encoding="utf-8")

        # Remove existing backlinks section
        cleaned = re.sub(
            r'<section class="wiki-backlinks"[^>]*>.*?</section>',
            '', content, flags=re.DOTALL
        ).rstrip('\n')

        # Insert backlinks after </main>
        if backlinks_html:
            if "</main>" in cleaned:
                cleaned = cleaned.replace("</main>", "</main>\n" + backlinks_html, 1)
            else:
                cleaned += "\n" + backlinks_html

        # Only write if changed
        if cleaned != content:
            page_path.write_text(cleaned, encoding="utf-8")
            updated += 1

    print(f"Backlinks injected: {updated} pages updated, {len(reverse)} pages have incoming links")


def assign_clusters(nodes, links):
    """Label propagation community detection. Assigns 'cluster' to each node."""
    import random
    random.seed(42)

    node_ids = {n["id"] for n in nodes}
    adj = {}
    for n in nodes:
        adj[n["id"]] = set()
    for link in links:
        s, t = link["source"], link["target"]
        if s in node_ids and t in node_ids:
            adj[s].add(t)
            adj[t].add(s)

    labels = {n["id"]: i for i, n in enumerate(nodes)}
    all_ids = list(labels.keys())

    for _ in range(50):
        changed = False
        random.shuffle(all_ids)
        for nid in all_ids:
            neighbors = adj.get(nid, set())
            if not neighbors:
                continue
            counts = {}
            for nb in neighbors:
                lbl = labels[nb]
                counts[lbl] = counts.get(lbl, 0) + 1
            best = max(counts, key=lambda k: counts[k])
            if labels[nid] != best:
                labels[nid] = best
                changed = True
        if not changed:
            break

    # Renumber clusters 0..N-1 by size
    from collections import Counter
    cluster_sizes = Counter(labels.values())
    rank = {lbl: i for i, (lbl, _) in enumerate(cluster_sizes.most_common())}
    for n in nodes:
        n["cluster"] = rank[labels[n["id"]]]


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

    # Assign community clusters
    assign_clusters(nodes, valid_links)

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
    """Generate graph.html with D3.js visualization (cluster coloring + timeline)."""
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
  .graph-controls {{ display: flex; gap: 0.8rem; margin: 0.5rem 0; flex-wrap: wrap; align-items: center; }}
  .graph-search {{ flex: 1; min-width: 200px; padding: 0.5rem 1rem; border: 1px solid #E2E8F0; border-radius: 8px; font-size: 0.85rem; background: rgba(255,255,255,0.6); }}
  .graph-search:focus {{ outline: none; border-color: #8B5CF6; box-shadow: 0 0 0 2px rgba(139,92,246,0.1); }}
  .graph-filters {{ display: flex; gap: 0.3rem; flex-wrap: wrap; }}
  .graph-filter {{ padding: 0.3rem 0.8rem; border-radius: 8px; border: 1px solid #E2E8F0; font-size: 0.8rem; cursor: pointer; background: white; display: inline-flex; align-items: center; }}
  .graph-filter:hover {{ border-color: rgba(139,92,246,0.3); }}
  .graph-filter.active {{ background: #8B5CF6; color: white; border-color: #8B5CF6; }}
  .graph-slider {{ display: flex; align-items: center; gap: 0.5rem; margin: 0.5rem 0; font-size: 0.8rem; color: #64748B; }}
  .graph-slider input[type="range"] {{ flex: 1; accent-color: #8B5CF6; }}
  .graph-slider label {{ min-width: 60px; font-weight: 500; }}
  .color-toggle {{ display: flex; gap: 0.3rem; }}
</style>
</head>
<body>
<nav class="wiki-nav">
  <a href="index.html">Index</a>
  <a href="search.html">Search</a>
  <a href="graph.html" class="active">Graph</a>
  <a href="log.html">Log</a>
  <a href="health.html">Health</a>
</nav>
<article class="wiki-content">
  <h1>Knowledge Graph</h1>

  <div class="graph-legend">
    <div class="graph-legend-item"><div class="graph-legend-dot" style="background:#0EA5E9"></div>Entity</div>
    <div class="graph-legend-item"><div class="graph-legend-dot" style="background:#10B981"></div>Concept</div>
    <div class="graph-legend-item"><div class="graph-legend-dot" style="background:#F59E0B"></div>Source</div>
    <div class="graph-legend-item"><div class="graph-legend-dot" style="background:#F43F5E"></div>Analysis</div>
  </div>

  <div class="graph-controls">
    <input type="text" class="graph-search" id="graph-search" placeholder="Search nodes..." oninput="filterGraph()">
    <div class="graph-filters" id="filters">
      <button class="graph-filter active" data-type="all" onclick="setTypeFilter(this, 'all')">All</button>
      <button class="graph-filter" data-type="source" onclick="setTypeFilter(this, 'source')"><span class="graph-legend-dot" style="background:#F59E0B;display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px"></span>Source</button>
      <button class="graph-filter" data-type="entity" onclick="setTypeFilter(this, 'entity')"><span class="graph-legend-dot" style="background:#0EA5E9;display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px"></span>Entity</button>
      <button class="graph-filter" data-type="concept" onclick="setTypeFilter(this, 'concept')"><span class="graph-legend-dot" style="background:#10B981;display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px"></span>Concept</button>
      <button class="graph-filter" data-type="analysis" onclick="setTypeFilter(this, 'analysis')"><span class="graph-legend-dot" style="background:#F43F5E;display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px"></span>Analysis</button>
    </div>
    <div class="color-toggle">
      <button class="graph-filter active" id="color-type" onclick="setColorMode('type')">By Type</button>
      <button class="graph-filter" id="color-cluster" onclick="setColorMode('cluster')">By Cluster</button>
    </div>
  </div>

  <div class="graph-slider">
    <label>Timeline:</label>
    <span id="date-start"></span>
    <input type="range" id="time-slider" min="0" max="100" value="100" oninput="filterByTime(this.value)">
    <span id="date-label">All</span>
  </div>

  <div id="graph-container">
    <div class="graph-tooltip" id="tooltip"></div>
  </div>
</article>
<footer class="wiki-footer">CC Wiki · Maintained by CloseCrab Bot</footer>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const TYPE_COLORS = {{ source: "#F59E0B", entity: "#0EA5E9", concept: "#10B981", analysis: "#F43F5E" }};
const CLUSTER_PALETTE = ["#8B5CF6","#0EA5E9","#10B981","#F59E0B","#F43F5E","#6366F1","#EC4899","#14B8A6","#F97316","#06B6D4","#A855F7","#EAB308"];
const DATA_URL = "../wiki-data/graph.json";
let GS = {{ node: null, link: null, label: null, activeType: 'all', colorMode: 'type', timeValue: 100, allDates: [] }};

function nodeColor(d) {{
  if (GS.colorMode === 'cluster') return CLUSTER_PALETTE[(d.cluster || 0) % CLUSTER_PALETTE.length];
  return TYPE_COLORS[d.type] || '#64748B';
}}

function setColorMode(mode) {{
  GS.colorMode = mode;
  document.getElementById('color-type').classList.toggle('active', mode === 'type');
  document.getElementById('color-cluster').classList.toggle('active', mode === 'cluster');
  if (GS.node) GS.node.attr('fill', nodeColor);
}}

function setTypeFilter(el, type) {{
  document.querySelectorAll('#filters .graph-filter').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  GS.activeType = type;
  filterGraph();
}}

function filterByTime(val) {{
  GS.timeValue = parseInt(val);
  if (GS.timeValue >= 100) {{
    document.getElementById('date-label').textContent = 'All';
  }} else {{
    const idx = Math.floor(GS.timeValue / 100 * (GS.allDates.length - 1));
    document.getElementById('date-label').textContent = GS.allDates[Math.min(idx, GS.allDates.length - 1)];
  }}
  filterGraph();
}}

function nodeVisible(d) {{
  const type = GS.activeType;
  const q = document.getElementById('graph-search').value.toLowerCase();
  const typeMatch = type === 'all' || d.type === type;
  const searchMatch = !q || d.title.toLowerCase().includes(q) || d.tags.some(t => t.includes(q));
  let timeMatch = true;
  if (GS.timeValue < 100 && GS.allDates.length > 0) {{
    const idx = Math.floor(GS.timeValue / 100 * (GS.allDates.length - 1));
    const cutoff = GS.allDates[Math.min(idx, GS.allDates.length - 1)];
    timeMatch = (d.created || '9999') <= cutoff;
  }}
  return typeMatch && searchMatch && timeMatch;
}}

function filterGraph() {{
  if (!GS.node) return;
  GS.node.attr('opacity', d => nodeVisible(d) ? 1 : 0.06).attr('fill', nodeColor);
  GS.label.attr('opacity', d => nodeVisible(d) ? 1 : 0.04);
  GS.link.attr('opacity', d => (nodeVisible(d.source) && nodeVisible(d.target)) ? 0.6 : 0.02);
}}

async function init() {{
  const resp = await fetch(DATA_URL);
  if (!resp.ok) {{ document.getElementById('graph-container').innerHTML = '<p style="padding:2rem;color:#94A3B8">No graph data yet.</p>'; return; }}
  const data = await resp.json();
  if (!data.nodes.length) {{ document.getElementById('graph-container').innerHTML = '<p style="padding:2rem;color:#94A3B8">Empty graph.</p>'; return; }}
  GS.allDates = data.nodes.map(n => n.created).filter(Boolean).sort();
  if (GS.allDates.length) document.getElementById('date-start').textContent = GS.allDates[0];
  renderGraph(data);
}}

function renderGraph(data) {{
  const container = document.getElementById('graph-container');
  const width = container.clientWidth;
  const height = container.clientHeight;
  const tooltip = document.getElementById('tooltip');

  const degree = {{}};
  data.nodes.forEach(n => degree[n.id] = 0);
  data.links.forEach(l => {{
    const s = typeof l.source === 'string' ? l.source : l.source.id;
    const t = typeof l.target === 'string' ? l.target : l.target.id;
    degree[s] = (degree[s] || 0) + 1;
    degree[t] = (degree[t] || 0) + 1;
  }});

  const svg = d3.select(container).append('svg').attr('viewBox', [0, 0, width, height]);
  const g = svg.append('g');
  svg.call(d3.zoom().scaleExtent([0.2, 6]).on('zoom', (e) => g.attr('transform', e.transform)));

  const sim = d3.forceSimulation(data.nodes)
    .force('link', d3.forceLink(data.links).id(d => d.id).distance(80))
    .force('charge', d3.forceManyBody().strength(-180))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius(d => 8 + (degree[d.id] || 0) * 0.8));

  const link = g.append('g').selectAll('line')
    .data(data.links).join('line')
    .attr('stroke', '#CBD5E1').attr('stroke-width', 1).attr('stroke-opacity', 0.5);

  const node = g.append('g').selectAll('circle')
    .data(data.nodes).join('circle')
    .attr('r', d => Math.min(5 + (degree[d.id] || 0) * 1.2, 25))
    .attr('fill', nodeColor)
    .attr('stroke', '#fff').attr('stroke-width', 1.5)
    .style('cursor', 'pointer')
    .call(d3.drag().on('start', dragStart).on('drag', dragging).on('end', dragEnd));

  const label = g.append('g').selectAll('text')
    .data(data.nodes).join('text')
    .text(d => d.title.length > 22 ? d.title.slice(0, 20) + '...' : d.title)
    .attr('font-size', d => (degree[d.id] || 0) > 10 ? '10px' : '8px')
    .attr('font-weight', d => (degree[d.id] || 0) > 10 ? '600' : '400')
    .attr('fill', '#475569').attr('text-anchor', 'middle').attr('dy', -12)
    .style('pointer-events', 'none');

  GS.node = node; GS.link = link; GS.label = label;

  node.on('mouseover', (e, d) => {{
    tooltip.style.opacity = 1;
    const cl = d.cluster !== undefined ? ` · C${{d.cluster}}` : '';
    tooltip.innerHTML = `<strong>${{d.title}}</strong><br>${{d.type}}${{cl}} · ${{d.tags.join(', ')}}<br>${{d.summary || ''}}<br><em>${{d.created || ''}}</em>`;
    const connected = new Set([d.id]);
    data.links.forEach(l => {{
      if (l.source.id === d.id) connected.add(l.target.id);
      if (l.target.id === d.id) connected.add(l.source.id);
    }});
    node.attr('opacity', n => connected.has(n.id) ? 1 : 0.1);
    link.attr('opacity', l => (l.source.id === d.id || l.target.id === d.id) ? 1 : 0.03);
    label.attr('opacity', n => connected.has(n.id) ? 1 : 0.05);
  }}).on('mousemove', (e) => {{
    tooltip.style.left = (e.offsetX + 15) + 'px';
    tooltip.style.top = (e.offsetY - 10) + 'px';
  }}).on('mouseout', () => {{
    tooltip.style.opacity = 0;
    filterGraph();
  }}).on('click', (e, d) => {{
    window.location.href = d.path;
  }});

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

    # Auto-inject backlinks into pages
    inject_backlinks(nodes, links)

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
