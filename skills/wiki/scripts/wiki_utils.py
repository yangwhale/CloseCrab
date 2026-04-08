"""wiki_utils.py — Shared utilities for Wiki scripts."""
import hashlib
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))

# Files that are not regular wiki pages (used by rebuild-index and rebuild-graph)
SKIP_FILES = {"index.html", "search.html", "log.html", "graph.html", "health.html",
              "style.css", "overview.html", "local-graph.js"}

# Type display config
TYPE_ORDER = ["source", "entity", "concept", "analysis"]
TYPE_COLORS = {
    "source": "#F59E0B",
    "entity": "#0EA5E9",
    "concept": "#10B981",
    "analysis": "#F43F5E",
}
TYPE_LABELS = {
    "source": "Sources 来源摘要",
    "entity": "Entities 实体",
    "concept": "Concepts 概念",
    "analysis": "Analyses 分析",
}


class WikiMetaParser(HTMLParser):
    """Extract wiki-* meta tags, <title>, and wiki-summary from HTML."""

    def __init__(self):
        super().__init__()
        self.meta = {}
        self.title = ""
        self.summary = ""
        self._in_title = False
        self._in_summary = False

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            d = dict(attrs)
            name = d.get("name", "")
            if name.startswith("wiki-"):
                self.meta[name] = d.get("content", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "p":
            d = dict(attrs)
            if "wiki-summary" in d.get("class", ""):
                self._in_summary = True

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_summary:
            self.summary += data

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "p" and self._in_summary:
            self._in_summary = False

    def clean_title(self):
        """Return title without ' — CC Wiki' suffix."""
        return self.title.replace(" — CC Wiki", "").strip()


class TextExtractor(HTMLParser):
    """Extract plain text from <main> tag, stripping HTML tags."""

    def __init__(self):
        super().__init__()
        self.text_parts = []
        self._in_main = False
        self._depth = 0
        self._skip_tags = {"script", "style", "nav", "footer"}
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self._in_main = True
            self._depth = 1
        elif self._in_main:
            self._depth += 1
            if tag in self._skip_tags:
                self._skip_depth = self._depth
        if self._in_main and tag in ("p", "h1", "h2", "h3", "h4", "li", "tr", "br"):
            self.text_parts.append("\n")

    def handle_endtag(self, tag):
        if self._in_main:
            if self._depth == self._skip_depth:
                self._skip_depth = 0
            self._depth -= 1
            if tag == "main":
                self._in_main = False

    def handle_data(self, data):
        if self._in_main and not self._skip_depth:
            self.text_parts.append(data)

    def get_text(self):
        text = "".join(self.text_parts)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()


# ── Manifest helpers ──

def compute_file_hash(path):
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(wiki_repo=None):
    """Load compile-manifest.json, return empty dict if not found."""
    repo = Path(wiki_repo) if wiki_repo else WIKI_REPO
    manifest_path = repo / "wiki-data" / "compile-manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"version": 1, "pages": {}}


def save_manifest(manifest, wiki_repo=None):
    """Save compile-manifest.json."""
    repo = Path(wiki_repo) if wiki_repo else WIKI_REPO
    manifest_path = repo / "wiki-data" / "compile-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
