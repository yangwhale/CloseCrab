"""wiki_utils.py — Shared utilities for Wiki scripts."""
from html.parser import HTMLParser

# Files that are not regular wiki pages (used by rebuild-index and rebuild-graph)
SKIP_FILES = {"index.html", "log.html", "graph.html", "style.css", "overview.html"}

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
