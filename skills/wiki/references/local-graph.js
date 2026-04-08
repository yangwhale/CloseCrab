/* local-graph.js — Render a local knowledge graph centered on the current page.
 * Loads graph.json and displays 1-hop neighbors using D3.js force layout.
 * Include via <script src="../local-graph.js"></script> on each wiki page.
 */
(function () {
  const TYPE_COLORS = {
    source: "#F59E0B",
    entity: "#0EA5E9",
    concept: "#10B981",
    analysis: "#F43F5E",
  };

  const container = document.querySelector(".local-graph-container");
  if (!container) return;

  const slug = container.dataset.pageSlug;
  if (!slug) return;

  const W = Math.min(container.parentElement.clientWidth, 800);
  const H = 300;

  fetch("../wiki-data/graph.json")
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      if (!data || !data.nodes.length) return;
      render(data, slug);
    })
    .catch(() => {});

  function render(data, centerSlug) {
    // Find center node
    const centerNode = data.nodes.find((n) => n.id === centerSlug);
    if (!centerNode) return;

    // Collect 1-hop neighbors
    const neighborIds = new Set();
    const localLinks = [];
    data.links.forEach((l) => {
      const src = typeof l.source === "object" ? l.source.id : l.source;
      const tgt = typeof l.target === "object" ? l.target.id : l.target;
      if (src === centerSlug) {
        neighborIds.add(tgt);
        localLinks.push({ source: src, target: tgt });
      } else if (tgt === centerSlug) {
        neighborIds.add(src);
        localLinks.push({ source: src, target: tgt });
      }
    });

    if (neighborIds.size === 0) {
      container.innerHTML =
        '<p style="color:#94A3B8;font-size:0.85rem;padding:1rem 0">No linked pages yet.</p>';
      return;
    }

    neighborIds.add(centerSlug);
    const localNodes = data.nodes
      .filter((n) => neighborIds.has(n.id))
      .map((n) => ({ ...n }));

    // D3 force layout
    const svg = d3
      .select(container)
      .append("svg")
      .attr("width", W)
      .attr("height", H)
      .attr("viewBox", [0, 0, W, H]);

    const g = svg.append("g");

    // Zoom
    svg.call(
      d3
        .zoom()
        .scaleExtent([0.5, 3])
        .on("zoom", (e) => g.attr("transform", e.transform))
    );

    const sim = d3
      .forceSimulation(localNodes)
      .force(
        "link",
        d3
          .forceLink(localLinks)
          .id((d) => d.id)
          .distance(100)
      )
      .force("charge", d3.forceManyBody().strength(-300))
      .force("center", d3.forceCenter(W / 2, H / 2))
      .force("collision", d3.forceCollide().radius(30));

    const link = g
      .append("g")
      .selectAll("line")
      .data(localLinks)
      .join("line")
      .attr("stroke", "#CBD5E1")
      .attr("stroke-width", 1.5)
      .attr("stroke-opacity", 0.5);

    const node = g
      .append("g")
      .selectAll("circle")
      .data(localNodes)
      .join("circle")
      .attr("r", (d) => (d.id === centerSlug ? 12 : 8))
      .attr("fill", (d) => TYPE_COLORS[d.type] || "#64748B")
      .attr("stroke", (d) => (d.id === centerSlug ? "#7C3AED" : "#fff"))
      .attr("stroke-width", (d) => (d.id === centerSlug ? 3 : 1.5))
      .style("cursor", (d) => (d.id === centerSlug ? "default" : "pointer"))
      .on("click", (e, d) => {
        if (d.id !== centerSlug) {
          // Navigate relative to current page
          const parts = d.path.split("/");
          window.location.href = "../" + d.path;
        }
      })
      .call(
        d3
          .drag()
          .on("start", (e, d) => {
            if (!e.active) sim.alphaTarget(0.3).restart();
            d.fx = d.x;
            d.fy = d.y;
          })
          .on("drag", (e, d) => {
            d.fx = e.x;
            d.fy = e.y;
          })
          .on("end", (e, d) => {
            if (!e.active) sim.alphaTarget(0);
            d.fx = null;
            d.fy = null;
          })
      );

    const label = g
      .append("g")
      .selectAll("text")
      .data(localNodes)
      .join("text")
      .text((d) =>
        d.title.length > 16 ? d.title.slice(0, 14) + "..." : d.title
      )
      .attr("font-size", (d) => (d.id === centerSlug ? "11px" : "9px"))
      .attr("font-weight", (d) => (d.id === centerSlug ? "700" : "400"))
      .attr("fill", "#475569")
      .attr("text-anchor", "middle")
      .attr("dy", (d) => (d.id === centerSlug ? -18 : -14));

    sim.on("tick", () => {
      link
        .attr("x1", (d) => d.source.x)
        .attr("y1", (d) => d.source.y)
        .attr("x2", (d) => d.target.x)
        .attr("y2", (d) => d.target.y);
      node.attr("cx", (d) => d.x).attr("cy", (d) => d.y);
      label.attr("x", (d) => d.x).attr("y", (d) => d.y);
    });
  }
})();
