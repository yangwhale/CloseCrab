#!/usr/bin/env python3
"""graph-query.py — Graph traversal and analysis for CC Wiki.

Usage:
  python3 graph-query.py path tpu-v7 b200              # BFS shortest path
  python3 graph-query.py neighbors tpu-v7 --depth 2    # N-hop neighbors
  python3 graph-query.py cluster                        # Community detection
  python3 graph-query.py central --metric degree        # Centrality analysis
  python3 graph-query.py stats                          # Graph statistics
"""
import argparse
import json
import os
import sys
from collections import defaultdict, deque

sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import WIKI_REPO

DATA_DIR = WIKI_REPO / "wiki-data"


def load_graph():
    path = DATA_DIR / "graph.json"
    if not path.exists():
        print("Error: graph.json not found. Run rebuild-graph.py first.", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def build_adjacency(graph):
    """Build undirected adjacency list."""
    adj = defaultdict(set)
    for link in graph.get("links", []):
        src = link["source"] if isinstance(link["source"], str) else link["source"].get("id", "")
        tgt = link["target"] if isinstance(link["target"], str) else link["target"].get("id", "")
        adj[src].add(tgt)
        adj[tgt].add(src)
    return adj


def build_directed(graph):
    """Build directed adjacency (outgoing only)."""
    out = defaultdict(set)
    inc = defaultdict(set)
    for link in graph.get("links", []):
        src = link["source"] if isinstance(link["source"], str) else link["source"].get("id", "")
        tgt = link["target"] if isinstance(link["target"], str) else link["target"].get("id", "")
        out[src].add(tgt)
        inc[tgt].add(src)
    return out, inc


def node_map(graph):
    return {n["id"]: n for n in graph.get("nodes", [])}


# ── Commands ──

def cmd_path(graph, source, target):
    """BFS shortest path between two nodes."""
    adj = build_adjacency(graph)
    nodes = node_map(graph)

    if source not in nodes:
        print(f"Node '{source}' not found")
        return
    if target not in nodes:
        print(f"Node '{target}' not found")
        return

    # BFS
    queue = deque([(source, [source])])
    visited = {source}

    while queue:
        current, path = queue.popleft()
        if current == target:
            print(f"Path ({len(path) - 1} hops):")
            for i, nid in enumerate(path):
                n = nodes.get(nid, {})
                prefix = "  → " if i > 0 else "  "
                print(f"{prefix}[{n.get('type', '?')}] {n.get('title', nid)}")
            return

        for neighbor in adj.get(current, set()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))

    print(f"No path found between '{source}' and '{target}'")


def cmd_neighbors(graph, slug, depth=1):
    """N-hop neighbors of a node."""
    adj = build_adjacency(graph)
    nodes = node_map(graph)

    if slug not in nodes:
        print(f"Node '{slug}' not found")
        return

    visited = {slug}
    frontier = {slug}
    layers = []

    for d in range(depth):
        next_frontier = set()
        for node in frontier:
            for neighbor in adj.get(node, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.add(neighbor)
        layers.append(next_frontier)
        frontier = next_frontier

    center = nodes[slug]
    print(f"Neighbors of [{center['type']}] {center['title']}:")

    for d, layer in enumerate(layers, 1):
        print(f"\n  Depth {d} ({len(layer)} nodes):")
        for nid in sorted(layer):
            n = nodes.get(nid, {})
            print(f"    [{n.get('type', '?')}] {n.get('title', nid)}")


def cmd_cluster(graph):
    """Simple community detection using label propagation."""
    adj = build_adjacency(graph)
    nodes = node_map(graph)
    all_ids = list(nodes.keys())

    if not all_ids:
        print("Empty graph")
        return

    # Label propagation
    labels = {nid: i for i, nid in enumerate(all_ids)}
    import random
    random.seed(42)

    for _ in range(50):  # iterations
        changed = False
        random.shuffle(all_ids)
        for nid in all_ids:
            neighbors = adj.get(nid, set())
            if not neighbors:
                continue
            # Most common label among neighbors
            label_counts = defaultdict(int)
            for nb in neighbors:
                label_counts[labels[nb]] += 1
            best_label = max(label_counts, key=lambda k: label_counts[k])
            if labels[nid] != best_label:
                labels[nid] = best_label
                changed = True
        if not changed:
            break

    # Group by cluster
    clusters = defaultdict(list)
    for nid, label in labels.items():
        clusters[label].append(nid)

    # Sort clusters by size
    sorted_clusters = sorted(clusters.values(), key=len, reverse=True)

    print(f"Found {len(sorted_clusters)} communities:\n")
    for i, members in enumerate(sorted_clusters):
        if len(members) < 2:
            continue
        type_counts = defaultdict(int)
        for nid in members:
            type_counts[nodes[nid]["type"]] += 1
        type_str = ", ".join(f"{t}:{c}" for t, c in sorted(type_counts.items()))
        print(f"  Cluster {i + 1} ({len(members)} nodes, {type_str}):")
        for nid in sorted(members, key=lambda x: nodes[x].get("title", x)):
            n = nodes[nid]
            print(f"    [{n['type']}] {n['title']}")
        print()


def cmd_central(graph, metric="degree"):
    """Centrality analysis."""
    nodes = node_map(graph)
    adj = build_adjacency(graph)
    out_adj, in_adj = build_directed(graph)

    if metric == "degree":
        scores = {nid: len(adj.get(nid, set())) for nid in nodes}
    elif metric == "in-degree":
        scores = {nid: len(in_adj.get(nid, set())) for nid in nodes}
    elif metric == "out-degree":
        scores = {nid: len(out_adj.get(nid, set())) for nid in nodes}
    elif metric == "betweenness":
        # Simplified betweenness (sampling for performance)
        scores = {nid: 0.0 for nid in nodes}
        all_ids = list(nodes.keys())
        import random
        random.seed(42)
        sample = random.sample(all_ids, min(50, len(all_ids)))

        for source in sample:
            # BFS
            queue = deque([source])
            dist = {source: 0}
            paths = {source: 1}
            order = []

            while queue:
                v = queue.popleft()
                order.append(v)
                for w in adj.get(v, set()):
                    if w not in dist:
                        dist[w] = dist[v] + 1
                        queue.append(w)
                        paths[w] = 0
                    if dist[w] == dist[v] + 1:
                        paths[w] += paths[v]

            delta = {v: 0.0 for v in order}
            for v in reversed(order):
                for w in adj.get(v, set()):
                    if dist.get(w, -1) == dist.get(v, -1) + 1:
                        delta[v] += (paths[v] / max(paths[w], 1)) * (1 + delta[w])
                if v != source:
                    scores[v] += delta[v]

        # Normalize
        n = len(all_ids)
        if n > 2:
            for nid in scores:
                scores[nid] /= (n - 1) * (n - 2) / 2
    else:
        print(f"Unknown metric: {metric}. Use: degree, in-degree, out-degree, betweenness")
        return

    # Top 20
    top = sorted(scores.items(), key=lambda x: -x[1])[:20]
    print(f"Top 20 nodes by {metric} centrality:\n")
    for rank, (nid, score) in enumerate(top, 1):
        n = nodes[nid]
        print(f"  {rank:2d}. [{n['type']}] {n['title']:<40s} {score:.4f}")


def cmd_stats(graph):
    """Graph statistics."""
    nodes = graph.get("nodes", [])
    links = graph.get("links", [])
    adj = build_adjacency(graph)

    type_counts = defaultdict(int)
    for n in nodes:
        type_counts[n["type"]] += 1

    degrees = [len(adj.get(n["id"], set())) for n in nodes]
    avg_degree = sum(degrees) / len(degrees) if degrees else 0
    max_degree = max(degrees) if degrees else 0
    isolated = sum(1 for d in degrees if d == 0)

    # Connected components (BFS)
    visited = set()
    components = 0
    for n in nodes:
        if n["id"] not in visited:
            components += 1
            queue = deque([n["id"]])
            while queue:
                v = queue.popleft()
                if v in visited:
                    continue
                visited.add(v)
                for nb in adj.get(v, set()):
                    if nb not in visited:
                        queue.append(nb)

    density = (2 * len(links)) / (len(nodes) * (len(nodes) - 1)) if len(nodes) > 1 else 0

    print("Graph Statistics:")
    print(f"  Nodes: {len(nodes)}")
    print(f"  Links: {len(links)}")
    print(f"  Density: {density:.4f}")
    print(f"  Components: {components}")
    print(f"  Isolated: {isolated}")
    print(f"  Avg degree: {avg_degree:.1f}")
    print(f"  Max degree: {max_degree}")
    print(f"\nBy type:")
    for t, c in sorted(type_counts.items()):
        print(f"  {t}: {c}")


def main():
    parser = argparse.ArgumentParser(description="CC Wiki graph query tool")
    subparsers = parser.add_subparsers(dest="command")

    p_path = subparsers.add_parser("path", help="BFS shortest path")
    p_path.add_argument("source", help="Source node slug")
    p_path.add_argument("target", help="Target node slug")

    p_nb = subparsers.add_parser("neighbors", help="N-hop neighbors")
    p_nb.add_argument("slug", help="Node slug")
    p_nb.add_argument("--depth", type=int, default=1, help="Hop depth (default: 1)")

    p_cl = subparsers.add_parser("cluster", help="Community detection")

    p_ct = subparsers.add_parser("central", help="Centrality analysis")
    p_ct.add_argument("--metric", default="degree",
                       choices=["degree", "in-degree", "out-degree", "betweenness"],
                       help="Centrality metric")

    p_st = subparsers.add_parser("stats", help="Graph statistics")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    graph = load_graph()

    if args.command == "path":
        cmd_path(graph, args.source, args.target)
    elif args.command == "neighbors":
        cmd_neighbors(graph, args.slug, args.depth)
    elif args.command == "cluster":
        cmd_cluster(graph)
    elif args.command == "central":
        cmd_central(graph, args.metric)
    elif args.command == "stats":
        cmd_stats(graph)


if __name__ == "__main__":
    main()
