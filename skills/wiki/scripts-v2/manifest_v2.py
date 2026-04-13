#!/usr/bin/env python3
"""manifest_v2.py — Enhanced manifest with cached metadata + outgoing links.

The manifest is the single source of truth for graph/index/backlinks generation,
eliminating the need to re-parse all HTML files on every rebuild.

Data flow:
  HTML page change → scan_page() → update manifest entry → rebuild from manifest
"""
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

# Import shared utilities from v1
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from wiki_utils import (
    WIKI_REPO, SKIP_FILES, WikiMetaParser, TextExtractor,
    compute_file_hash, TYPE_ORDER, TYPE_COLORS, TYPE_LABELS,
)

WIKI_DIR = WIKI_REPO / "wiki"
DATA_DIR = WIKI_REPO / "wiki-data"
MANIFEST_PATH = DATA_DIR / "compile-manifest-v2.json"


class LinkExtractor(HTMLParser):
    """Extract wiki-link hrefs from <main> only, ignoring backlinks section.

    The backlinks section contains reverse references (pages that link TO this page).
    Those should not be counted as outgoing links from this page.
    """

    def __init__(self):
        super().__init__()
        self.links = []
        self._in_main = False
        self._in_backlinks = False

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "main":
            self._in_main = True
        elif tag == "section" and "wiki-backlinks" in (d.get("class") or ""):
            self._in_backlinks = True
        elif tag == "a" and self._in_main and not self._in_backlinks:
            cls = d.get("class") or ""
            href = d.get("href") or ""
            if "wiki-link" in cls and href:
                match = re.search(r'([\w][\w.-]*)\.html$', href)
                if match:
                    self.links.append(match.group(1))

    def handle_endtag(self, tag):
        if tag == "main":
            self._in_main = False
        elif tag == "section" and self._in_backlinks:
            self._in_backlinks = False


class ManifestV2:
    """Enhanced manifest that caches page metadata + outgoing links.

    With this manifest, graph.json and index.html can be rebuilt by iterating
    the manifest dict — no HTML parsing needed.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or MANIFEST_PATH
        self.data = {"version": 2, "updated": "", "pages": {}}

    def load(self) -> "ManifestV2":
        """Load manifest from disk. Returns self for chaining."""
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        return self

    def save(self) -> None:
        """Save manifest to disk."""
        self.data["updated"] = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @property
    def pages(self) -> dict:
        return self.data["pages"]

    # ── Single-page operations ──

    def scan_page(self, html_path: Path) -> dict | None:
        """Parse a single HTML page and return its manifest entry.

        Returns None if the file is not a valid wiki page.
        """
        try:
            content = html_path.read_text(encoding="utf-8")
        except Exception:
            return None

        # Extract metadata
        meta_parser = WikiMetaParser()
        try:
            meta_parser.feed(content)
        except Exception:
            return None

        if not meta_parser.meta.get("wiki-type"):
            return None

        # Extract outgoing wiki-links
        link_parser = LinkExtractor()
        try:
            link_parser.feed(content)
        except Exception:
            pass

        slug = html_path.stem
        rel_path = str(html_path.relative_to(WIKI_DIR))
        tags = [t.strip() for t in meta_parser.meta.get("wiki-tags", "").split(",") if t.strip()]

        return {
            "sha256": compute_file_hash(html_path),
            "slug": slug,
            "title": meta_parser.clean_title(),
            "type": meta_parser.meta.get("wiki-type", ""),
            "path": rel_path,
            "tags": tags,
            "summary": meta_parser.summary.strip(),
            "created": meta_parser.meta.get("wiki-created", ""),
            "updated": meta_parser.meta.get("wiki-updated", ""),
            "source_count": int(meta_parser.meta.get("wiki-sources", "0") or "0"),
            "outgoing_links": sorted(set(link_parser.links)),
        }

    def update_page(self, html_path: Path) -> tuple[str, dict | None, dict | None]:
        """Scan a page and update its manifest entry.

        Returns (rel_key, old_entry_or_None, new_entry_or_None).
        If the page hasn't changed (same sha256), returns (key, old, None).
        """
        rel_key = str(html_path.relative_to(WIKI_DIR))
        old_entry = self.pages.get(rel_key)

        # Quick hash check — skip if unchanged
        if old_entry:
            current_hash = compute_file_hash(html_path)
            if current_hash == old_entry.get("sha256"):
                return rel_key, old_entry, None  # unchanged

        new_entry = self.scan_page(html_path)
        if new_entry:
            self.pages[rel_key] = new_entry
        return rel_key, old_entry, new_entry

    # ── Bulk operations ──

    def detect_changes(self) -> dict:
        """Compare manifest against actual files on disk.

        Returns {"added": [...], "changed": [...], "deleted": [...]}.
        Each item is a relative path key like "entities/tpu-v7.html".
        """
        result = {"added": [], "changed": [], "deleted": []}

        # Find all current HTML files
        current_files = set()
        for html_file in WIKI_DIR.rglob("*.html"):
            rel = html_file.relative_to(WIKI_DIR)
            if rel.name in SKIP_FILES or str(rel).startswith(".") or str(rel).startswith("_"):
                continue
            rel_key = str(rel)
            current_files.add(rel_key)

            if rel_key not in self.pages:
                result["added"].append(rel_key)
            else:
                current_hash = compute_file_hash(html_file)
                if current_hash != self.pages[rel_key].get("sha256"):
                    result["changed"].append(rel_key)

        # Detect deleted pages
        for rel_key in list(self.pages.keys()):
            if rel_key not in current_files:
                result["deleted"].append(rel_key)

        return result

    def full_scan(self) -> dict:
        """Scan ALL wiki pages and rebuild the entire manifest.

        Returns change summary {"added": N, "changed": N, "deleted": N}.
        """
        old_keys = set(self.pages.keys())
        new_pages = {}
        stats = {"added": 0, "changed": 0, "deleted": 0}

        for html_file in WIKI_DIR.rglob("*.html"):
            rel = html_file.relative_to(WIKI_DIR)
            if rel.name in SKIP_FILES or str(rel).startswith(".") or str(rel).startswith("_"):
                continue

            entry = self.scan_page(html_file)
            if entry:
                rel_key = str(rel)
                new_pages[rel_key] = entry

                if rel_key not in old_keys:
                    stats["added"] += 1
                elif entry["sha256"] != self.pages.get(rel_key, {}).get("sha256"):
                    stats["changed"] += 1

        # Count deleted
        new_keys = set(new_pages.keys())
        stats["deleted"] = len(old_keys - new_keys)

        self.data["pages"] = new_pages
        return stats

    # ── Graph generation (from manifest, no file I/O) ──

    def build_graph_json(self) -> dict:
        """Build graph.json directly from manifest data.

        No HTML files are read — all data comes from cached manifest entries.
        """
        nodes = []
        links = []
        node_ids = set()

        for rel_key, entry in self.pages.items():
            slug = entry["slug"]
            node_ids.add(slug)
            nodes.append({
                "id": slug,
                "title": entry["title"],
                "type": entry["type"],
                "path": entry["path"],
                "tags": entry["tags"],
                "summary": entry["summary"],
                "created": entry["created"],
                "updated": entry["updated"],
                "source_count": entry.get("source_count", 0),
            })

            for target_slug in entry.get("outgoing_links", []):
                links.append({
                    "source": slug,
                    "target": target_slug,
                    "type": "mentions",
                })

        # Deduplicate and add reverse links (make graph bidirectional)
        # Content links are directional (A mentions B), but the graph should
        # be bidirectional for visualization and community detection.
        seen = set()
        unique_links = []
        for link in links:
            s, t = link["source"], link["target"]
            if s not in node_ids or t not in node_ids:
                continue
            # Add forward link
            if (s, t) not in seen:
                seen.add((s, t))
                unique_links.append(link)
            # Add reverse link
            if (t, s) not in seen:
                seen.add((t, s))
                unique_links.append({"source": t, "target": s, "type": "mentioned-by"})

        valid_links = unique_links

        # Assign community clusters (label propagation)
        self._assign_clusters(nodes, valid_links)

        return {
            "meta": {
                "updated": datetime.now(timezone.utc).isoformat(),
                "node_count": len(nodes),
                "link_count": len(valid_links),
            },
            "nodes": nodes,
            "links": valid_links,
        }

    @staticmethod
    def _assign_clusters(nodes, links):
        """Label propagation community detection (same algo as rebuild-graph.py)."""
        import random
        random.seed(42)

        node_ids = {n["id"] for n in nodes}
        adj = {n["id"]: set() for n in nodes}
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

        cluster_sizes = Counter(labels.values())
        rank = {lbl: i for i, (lbl, _) in enumerate(cluster_sizes.most_common())}
        for n in nodes:
            n["cluster"] = rank[labels[n["id"]]]

    # ── Index generation (from manifest, no file I/O) ──

    def build_index_data(self) -> list[dict]:
        """Extract page data for index.html generation from manifest.

        Returns a list of page dicts compatible with rebuild-index.py's build_index_html().
        """
        pages = []
        for rel_key, entry in self.pages.items():
            pages.append({
                "title": entry["title"],
                "type": entry["type"],
                "tags": entry["tags"],
                "created": entry["created"],
                "updated": entry["updated"],
                "sources": str(entry.get("source_count", 0)),
                "path": entry["path"],
                "slug": entry["slug"],
            })
        return pages

    # ── Backlink computation (from manifest, no file I/O) ──

    def compute_backlinks(self) -> dict[str, list[tuple[str, str, str]]]:
        """Compute reverse link index from manifest.

        Returns {target_slug: [(source_slug, source_title, source_path), ...]}.
        """
        node_ids = {entry["slug"] for entry in self.pages.values()}
        reverse: dict[str, list[tuple[str, str, str]]] = {}
        seen = set()

        for entry in self.pages.values():
            src_slug = entry["slug"]
            for target_slug in entry.get("outgoing_links", []):
                if target_slug not in node_ids:
                    continue
                key = (src_slug, target_slug)
                if key in seen:
                    continue
                seen.add(key)
                reverse.setdefault(target_slug, []).append(
                    (src_slug, entry["title"], entry["path"])
                )

        # Sort each backlink list by title
        for slug in reverse:
            reverse[slug].sort(key=lambda x: x[1])

        return reverse

    def compute_affected_slugs(
        self, old_entry: dict | None, new_entry: dict | None
    ) -> set[str]:
        """Compute which slugs need backlinks rewritten after a page change.

        Compares old vs new outgoing_links and returns the set of target slugs
        whose backlinks section may have changed.
        """
        old_links = set(old_entry.get("outgoing_links", [])) if old_entry else set()
        new_links = set(new_entry.get("outgoing_links", [])) if new_entry else set()

        # Added or removed link targets need backlinks refresh
        affected = old_links.symmetric_difference(new_links)

        # The page itself also needs refresh (its own backlinks might reference changed title)
        if new_entry:
            affected.add(new_entry["slug"])
        if old_entry:
            affected.add(old_entry["slug"])

        return affected


def main():
    """CLI: bootstrap or inspect manifest."""
    import argparse

    parser = argparse.ArgumentParser(description="ManifestV2 CLI")
    parser.add_argument("--full-scan", action="store_true", help="Full scan and rebuild manifest")
    parser.add_argument("--detect", action="store_true", help="Detect changes without updating")
    parser.add_argument("--stats", action="store_true", help="Show manifest stats")
    args = parser.parse_args()

    m = ManifestV2().load()

    if args.full_scan:
        stats = m.full_scan()
        m.save()
        print(f"Full scan complete: {len(m.pages)} pages")
        print(f"  Added: {stats['added']}, Changed: {stats['changed']}, Deleted: {stats['deleted']}")

    elif args.detect:
        changes = m.detect_changes()
        total = sum(len(v) for v in changes.values())
        print(f"Changes detected: {total}")
        for kind, items in changes.items():
            if items:
                print(f"  {kind}: {', '.join(items)}")

    elif args.stats:
        types = Counter(e["type"] for e in m.pages.values())
        print(f"ManifestV2: {len(m.pages)} pages")
        for t, c in types.most_common():
            print(f"  {t}: {c}")
        total_links = sum(len(e.get("outgoing_links", [])) for e in m.pages.values())
        print(f"  Total outgoing links: {total_links}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
