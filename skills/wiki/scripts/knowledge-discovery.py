#!/usr/bin/env python3
"""knowledge-discovery.py — Proactive knowledge discovery engine for CC Wiki.

Analyzes the wiki to find:
  1. Missing concepts: frequently mentioned terms without dedicated pages
  2. Potential links: pages with overlapping tags but no connections
  3. Synthesis opportunities: 3+ sources referencing same entity without analysis
  4. Stale pages: not updated in >30 days
  5. Knowledge gaps: underrepresented topic areas

Outputs wiki-data/discoveries.json, consumed by health dashboard and lint.
"""
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import WIKI_REPO

WIKI_DIR = WIKI_REPO / "wiki"
DATA_DIR = WIKI_REPO / "wiki-data"


def load_graph():
    path = DATA_DIR / "graph.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def extract_frequent_terms(graph):
    """Find terms that appear frequently across pages but have no dedicated page."""
    node_ids = {n["id"] for n in graph.get("nodes", [])}
    node_titles_lower = {n["title"].lower() for n in graph.get("nodes", [])}

    # Count terms from tags
    tag_counts = Counter()
    for n in graph.get("nodes", []):
        for tag in n.get("tags", []):
            tag_counts[tag.lower()] += 1

    # Find tags that appear 3+ times but don't have dedicated pages
    missing = []
    for tag, count in tag_counts.most_common():
        if count < 3:
            break
        # Check if a page exists for this tag
        tag_slug = tag.lower().replace(" ", "-")
        if tag_slug not in node_ids and tag not in node_titles_lower:
            missing.append({"term": tag, "mentions": count, "type": "frequent_tag"})

    return missing[:20]


def find_potential_links(graph):
    """Find pages with overlapping tags but no direct connection."""
    nodes = graph.get("nodes", [])
    links = graph.get("links", [])

    # Build connection set
    connected = set()
    for link in links:
        s = link["source"] if isinstance(link["source"], str) else link["source"].get("id", "")
        t = link["target"] if isinstance(link["target"], str) else link["target"].get("id", "")
        connected.add((s, t))
        connected.add((t, s))

    # Find pairs with 2+ shared tags but no link
    suggestions = []
    for i, a in enumerate(nodes):
        tags_a = set(t.lower() for t in a.get("tags", []))
        if len(tags_a) < 2:
            continue
        for b in nodes[i + 1:]:
            if (a["id"], b["id"]) in connected:
                continue
            tags_b = set(t.lower() for t in b.get("tags", []))
            shared = tags_a & tags_b
            if len(shared) >= 2:
                suggestions.append({
                    "page_a": a["id"],
                    "title_a": a["title"],
                    "page_b": b["id"],
                    "title_b": b["title"],
                    "shared_tags": sorted(shared),
                    "overlap": len(shared),
                })

    suggestions.sort(key=lambda x: -x["overlap"])
    return suggestions[:20]


def find_synthesis_opportunities(graph):
    """Find entities referenced by 3+ sources but with no analysis page."""
    nodes = graph.get("nodes", [])
    links = graph.get("links", [])
    node_map = {n["id"]: n for n in nodes}

    # Count how many source pages reference each entity/concept
    entity_sources = defaultdict(set)
    for link in links:
        s = link["source"] if isinstance(link["source"], str) else link["source"].get("id", "")
        t = link["target"] if isinstance(link["target"], str) else link["target"].get("id", "")
        src_node = node_map.get(s)
        tgt_node = node_map.get(t)
        if src_node and src_node["type"] == "source" and tgt_node and tgt_node["type"] in ("entity", "concept"):
            entity_sources[t].add(s)

    # Check for existing analysis pages
    analysis_targets = set()
    for n in nodes:
        if n["type"] == "analysis":
            for link in links:
                s = link["source"] if isinstance(link["source"], str) else link["source"].get("id", "")
                if s == n["id"]:
                    t = link["target"] if isinstance(link["target"], str) else link["target"].get("id", "")
                    analysis_targets.add(t)

    opportunities = []
    for entity_id, sources in entity_sources.items():
        if len(sources) >= 3 and entity_id not in analysis_targets:
            node = node_map.get(entity_id, {})
            opportunities.append({
                "entity": entity_id,
                "title": node.get("title", entity_id),
                "type": node.get("type", ""),
                "source_count": len(sources),
                "sources": sorted(sources)[:5],
            })

    opportunities.sort(key=lambda x: -x["source_count"])
    return opportunities[:15]


def find_stale_pages(graph, days=30):
    """Find pages not updated in N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    stale = []
    for n in graph.get("nodes", []):
        updated = n.get("updated", n.get("created", ""))
        if updated and updated < cutoff:
            stale.append({
                "id": n["id"],
                "title": n["title"],
                "type": n["type"],
                "last_updated": updated,
                "days_stale": (datetime.now(timezone.utc) - datetime.fromisoformat(
                    updated + "T00:00:00+00:00" if "T" not in updated else updated
                )).days,
            })

    stale.sort(key=lambda x: x.get("days_stale", 0), reverse=True)
    return stale[:30]


def analyze_coverage(graph):
    """Analyze topic coverage to find knowledge gaps."""
    nodes = graph.get("nodes", [])
    type_counts = Counter(n["type"] for n in nodes)
    tag_counts = Counter()
    for n in nodes:
        for tag in n.get("tags", []):
            tag_counts[tag.lower()] += 1

    # Entity/concept ratio: should be roughly 1:1 with sources
    source_count = type_counts.get("source", 0)
    entity_count = type_counts.get("entity", 0)
    concept_count = type_counts.get("concept", 0)
    analysis_count = type_counts.get("analysis", 0)

    gaps = []
    if source_count > 0 and entity_count / max(source_count, 1) < 0.15:
        gaps.append({
            "type": "low_entity_ratio",
            "message": f"Only {entity_count} entities for {source_count} sources. Consider extracting more entities from sources.",
            "ratio": round(entity_count / source_count, 2),
        })
    if source_count > 0 and concept_count / max(source_count, 1) < 0.1:
        gaps.append({
            "type": "low_concept_ratio",
            "message": f"Only {concept_count} concepts for {source_count} sources. Consider identifying more abstract concepts.",
            "ratio": round(concept_count / source_count, 2),
        })
    if source_count > 20 and analysis_count < 3:
        gaps.append({
            "type": "low_analysis_count",
            "message": f"Only {analysis_count} analyses for {source_count} sources. Cross-source analyses create high knowledge value.",
            "count": analysis_count,
        })

    # Find underrepresented tags (many sources but few entities/concepts)
    tag_type_counts = defaultdict(lambda: defaultdict(int))
    for n in nodes:
        for tag in n.get("tags", []):
            tag_type_counts[tag.lower()][n["type"]] += 1

    for tag, types in tag_type_counts.items():
        sources = types.get("source", 0)
        entities = types.get("entity", 0) + types.get("concept", 0)
        if sources >= 5 and entities == 0:
            gaps.append({
                "type": "tag_gap",
                "message": f"Tag '{tag}' has {sources} sources but no entity/concept pages",
                "tag": tag,
                "source_count": sources,
            })

    return gaps


def main():
    graph = load_graph()
    if not graph:
        print("Error: graph.json not found")
        return

    print("Running knowledge discovery...\n")

    missing_concepts = extract_frequent_terms(graph)
    potential_links = find_potential_links(graph)
    synthesis_opps = find_synthesis_opportunities(graph)
    stale_pages = find_stale_pages(graph)
    coverage_gaps = analyze_coverage(graph)

    # Print summary
    print(f"Missing concepts (frequent tags without pages): {len(missing_concepts)}")
    for m in missing_concepts[:5]:
        print(f"  '{m['term']}' ({m['mentions']} mentions)")

    print(f"\nPotential links (shared tags, no connection): {len(potential_links)}")
    for p in potential_links[:5]:
        print(f"  {p['title_a']} <-> {p['title_b']} (shared: {', '.join(p['shared_tags'])})")

    print(f"\nSynthesis opportunities (3+ sources, no analysis): {len(synthesis_opps)}")
    for s in synthesis_opps[:5]:
        print(f"  [{s['type']}] {s['title']} ({s['source_count']} sources)")

    print(f"\nStale pages (>30 days): {len(stale_pages)}")

    print(f"\nCoverage gaps: {len(coverage_gaps)}")
    for g in coverage_gaps[:5]:
        print(f"  {g['message']}")

    # Save report
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "missing_concepts": missing_concepts,
        "potential_links": potential_links,
        "synthesis_opportunities": synthesis_opps,
        "stale_pages": stale_pages,
        "coverage_gaps": coverage_gaps,
        "summary": {
            "missing_concepts": len(missing_concepts),
            "potential_links": len(potential_links),
            "synthesis_opportunities": len(synthesis_opps),
            "stale_pages": len(stale_pages),
            "coverage_gaps": len(coverage_gaps),
        },
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "discoveries.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReport saved: {out_path}")


if __name__ == "__main__":
    main()
