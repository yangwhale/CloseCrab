#!/usr/bin/env python3
"""wiki-query.py — Query the Wiki knowledge base using BM25 + graph augmentation.

Usage:
  python3 wiki-query.py "TPU v7 和 B200 谁更适合 MoE？"
  python3 wiki-query.py "MFU 怎么算" --top-k 3
  python3 wiki-query.py "FSDP vs TP" --save
  python3 wiki-query.py "并行策略" --format json
"""
import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import WIKI_REPO

DATA_DIR = WIKI_REPO / "wiki-data"
WIKI_URL = os.environ.get("CC_PAGES_URL_PREFIX", "") + "/wiki"


# ── Tokenizer (no external dependencies) ──

# CJK function characters. They carry no retrieval meaning but poison bigrams:
# "如何用微波炉烤惠灵顿牛排" degenerates into 如何/何用/用微/微波/... and a page can
# score a "hit" purely on 用 or 配, which is how unrelated pages surface for a
# question the wiki never covered. Used to split a query into content-word runs.
STOP_CHARS = set("的了是在有和与也就都很会要能可以对把被将从到而及其之你我他她它"
                 "吗呢吧啊呀哦怎么什麼甚如何为请问一下这那些个")

# A page must match at least this fraction of the query's content terms to count
# as a real hit. Ratio (not absolute BM25 score) on purpose: scores drift with
# query length and corpus size, coverage does not.
MIN_COVERAGE = 0.34


def key_terms(text):
    """Extract the terms that actually carry meaning in a query.

    Splits CJK runs on function characters, then takes bigrams *within* each
    content-word run, so 虚词 never enter the denominator.
    """
    text = (text or "").lower()
    terms = set(t for t in re.findall(r'[a-z][a-z0-9_-]{1,}', text))
    terms.update(re.findall(r'\d+', text))
    for seg in re.findall(r'[一-鿿]+', text):
        splitter = ''.join(STOP_CHARS & set(seg)) or '　'
        for piece in re.split(f"[{splitter}]", seg):
            if len(piece) >= 2:
                terms.update(piece[i:i + 2] for i in range(len(piece) - 1))
            elif len(piece) == 1:
                terms.add(piece)
    return terms


def coverage(q_terms, doc_tokens):
    """Fraction of the query's content terms present in a document."""
    if not q_terms:
        return 0.0, set()
    hit = q_terms & doc_tokens
    return len(hit) / len(q_terms), hit


def tokenize(text):
    """Simple mixed Chinese/English tokenizer.

    Extracts: English words, Chinese character bigrams, numbers.
    """
    text = text.lower()
    tokens = []

    # English words and numbers
    tokens.extend(re.findall(r'[a-z][a-z0-9_-]*[a-z0-9]|[a-z]', text))

    # Chinese characters → bigrams for better matching
    chinese = re.findall(r'[\u4e00-\u9fff]+', text)
    for segment in chinese:
        # Unigrams
        tokens.extend(list(segment))
        # Bigrams
        for i in range(len(segment) - 1):
            tokens.append(segment[i:i+2])

    return tokens


# ── BM25 Engine ──

class BM25:
    """Okapi BM25 scoring, no external dependencies."""

    def __init__(self, corpus, k1=1.5, b=0.75):
        """
        Args:
            corpus: list of (doc_id, tokens) tuples
        """
        self.k1 = k1
        self.b = b
        self.corpus = corpus
        self.doc_count = len(corpus)
        self.avgdl = sum(len(toks) for _, toks in corpus) / max(self.doc_count, 1)

        # Document frequency
        self.df = Counter()
        self.doc_tokens = {}
        self.doc_tf = {}

        for doc_id, tokens in corpus:
            self.doc_tokens[doc_id] = tokens
            tf = Counter(tokens)
            self.doc_tf[doc_id] = tf
            for term in set(tokens):
                self.df[term] += 1

    def _idf(self, term):
        df = self.df.get(term, 0)
        return math.log((self.doc_count - df + 0.5) / (df + 0.5) + 1)

    def score(self, query_tokens, doc_id):
        tf = self.doc_tf.get(doc_id, {})
        dl = len(self.doc_tokens.get(doc_id, []))
        score = 0.0
        for term in query_tokens:
            f = tf.get(term, 0)
            if f == 0:
                continue
            idf = self._idf(term)
            num = f * (self.k1 + 1)
            den = f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            score += idf * num / den
        return score

    def search(self, query_tokens, top_k=10):
        """Return top-k (doc_id, score) pairs."""
        scores = []
        for doc_id, _ in self.corpus:
            s = self.score(query_tokens, doc_id)
            if s > 0:
                scores.append((doc_id, s))
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]


# ── Query Engine ──

def load_search_index():
    """Load search-chunks.json."""
    path = DATA_DIR / "search-chunks.json"
    if not path.exists():
        print(f"Error: Search index not found at {path}", file=sys.stderr)
        print("Run: python3 build-search-index.py", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def load_graph():
    """Load graph.json for augmentation."""
    path = DATA_DIR / "graph.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def get_graph_neighbors(graph, slug, depth=1):
    """Get N-hop neighbors from graph."""
    if not graph:
        return set()

    node_ids = {n["id"] for n in graph.get("nodes", [])}
    adj = defaultdict(set)
    for link in graph.get("links", []):
        src = link["source"] if isinstance(link["source"], str) else (link["source"].get("id", "") if isinstance(link["source"], dict) else "")
        tgt = link["target"] if isinstance(link["target"], str) else (link["target"].get("id", "") if isinstance(link["target"], dict) else "")
        if src in node_ids and tgt in node_ids:
            adj[src].add(tgt)
            adj[tgt].add(src)

    visited = {slug}
    frontier = {slug}
    for _ in range(depth):
        next_frontier = set()
        for node in frontier:
            for neighbor in adj.get(node, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.add(neighbor)
        frontier = next_frontier

    visited.discard(slug)
    return visited


def query(question, top_k=5, save=False, min_coverage=MIN_COVERAGE):
    """Execute a query against the wiki search index."""
    index = load_search_index()
    graph = load_graph()
    chunks = index.get("chunks", [])

    if not chunks:
        return {"query": question, "results": [], "message": "Empty search index"}

    # Build BM25 corpus from chunks
    corpus = []
    chunk_map = {}
    page_tokens = defaultdict(set)      # page_id -> all tokens, for coverage
    for chunk in chunks:
        chunk_id = chunk["id"]
        # Include title, tags, and text in tokenization for better matching
        combined = f"{chunk['page_title']} {' '.join(chunk['tags'])} {chunk['text']}"
        tokens = tokenize(combined)
        corpus.append((chunk_id, tokens))
        chunk_map[chunk_id] = chunk
        page_tokens[chunk["page_id"]].update(tokens)

    bm25 = BM25(corpus)
    query_tokens = tokenize(question)

    # BM25 search at chunk level
    chunk_results = bm25.search(query_tokens, top_k=top_k * 3)

    # Aggregate scores by page
    page_scores = defaultdict(float)
    page_chunks = defaultdict(list)
    page_info = {}

    for chunk_id, score in chunk_results:
        chunk = chunk_map[chunk_id]
        page_id = chunk["page_id"]
        page_scores[page_id] += score
        page_chunks[page_id].append({
            "text": chunk["text"][:300],  # Truncate for output
            "score": round(score, 4),
        })
        if page_id not in page_info:
            page_info[page_id] = {
                "title": chunk["page_title"],
                "type": chunk["page_type"],
                "path": chunk["path"],
                "tags": chunk["tags"],
            }

    # Relevance gate: drop pages that only brushed a stray character.
    # Without this a query the wiki never covered still returns a full page of
    # results — every one of them matching on a single CJK function character.
    # Graph neighbours are deliberately NOT injected into page_scores anymore:
    # they have near-zero coverage by construction and would all be filtered
    # right back out. They still surface per-result via "related_pages" below.
    q_terms = key_terms(question)
    scored, weak = [], []
    for page_id, score in page_scores.items():
        cov, hit = coverage(q_terms, page_tokens.get(page_id, set()))
        entry = (page_id, score, cov, sorted(hit))
        (scored if cov >= min_coverage else weak).append(entry)

    scored.sort(key=lambda x: -x[1])
    weak.sort(key=lambda x: -x[1])
    sorted_pages = scored[:top_k]

    results = []
    for page_id, score, cov, matched in sorted_pages:
        info = page_info.get(page_id, {})
        result = {
            "page_id": page_id,
            "title": info.get("title", page_id),
            "type": info.get("type", ""),
            "path": info.get("path", ""),
            "url": f"{WIKI_URL}/{info.get('path', '')}",
            "tags": info.get("tags", []),
            "relevance_score": round(score, 4),
            "coverage": round(cov, 2),
            "matched_terms": matched[:12],
            "matched_chunks": page_chunks.get(page_id, [])[:3],
        }

        # Add graph neighbors as related pages
        neighbors = get_graph_neighbors(graph, page_id, depth=1)
        result["related_pages"] = sorted(neighbors)[:5]

        results.append(result)

    output = {
        "query": question,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "result_count": len(results),
        "results": results,
        "found": bool(results),
        # Pages that scored but failed the relevance gate. Kept visible so a
        # near-miss is debuggable instead of silently vanishing.
        "weak_matches": [
            {"page_id": pid, "relevance_score": round(sc, 4),
             "coverage": round(cv, 2), "matched_terms": mt[:6]}
            for pid, sc, cv, mt in weak[:5]
        ],
    }

    # Save query to log if --save
    if save:
        save_query_log(output)
        output["saved"] = True

    return output


def save_query_log(query_result):
    """Append query result to query-log.json."""
    log_path = DATA_DIR / "query-log.json"

    if log_path.exists():
        log = json.loads(log_path.read_text(encoding="utf-8"))
    else:
        log = {"queries": []}

    log["queries"].append({
        "timestamp": query_result["timestamp"],
        "question": query_result["query"],
        "result_count": query_result["result_count"],
        "pages_consulted": [r["page_id"] for r in query_result["results"]],
    })

    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def format_text(output):
    """Format query results as human-readable text."""
    lines = [f"Query: {output['query']}", f"Found: {output['result_count']} relevant pages", ""]

    for i, r in enumerate(output["results"], 1):
        lines.append(f"{i}. [{r['type']}] {r['title']} (score: {r['relevance_score']})")
        lines.append(f"   URL: {r['url']}")
        if r["tags"]:
            lines.append(f"   Tags: {', '.join(r['tags'])}")
        if r.get("related_pages"):
            lines.append(f"   Related: {', '.join(r['related_pages'][:3])}")
        if r.get("matched_chunks"):
            preview = r["matched_chunks"][0]["text"][:150].replace("\n", " ")
            lines.append(f"   Preview: {preview}...")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Query the CC Wiki knowledge base")
    parser.add_argument("question", help="Question to search for")
    parser.add_argument("--top-k", type=int, default=5, help="Number of results (default: 5)")
    parser.add_argument("--save", action="store_true", help="Save query to query-log.json")
    parser.add_argument("--format", choices=["json", "text"], default="text", help="Output format")
    parser.add_argument("--min-coverage", type=float, default=MIN_COVERAGE,
                        help=f"Fraction of query content terms a page must match "
                             f"(default: {MIN_COVERAGE}; use 0 for pre-gate behaviour)")
    parser.add_argument("--check", action="store_true",
                        help="Only answer whether the wiki covers this at all "
                             "(exit 0 = covered, 1 = not covered)")
    args = parser.parse_args()

    result = query(args.question, top_k=args.top_k, save=args.save,
                   min_coverage=args.min_coverage)

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.check:
        if result["found"]:
            print(f"COVERED: {args.question} — {result['result_count']} page(s)")
            for r in result["results"]:
                print(f"  · {r['title']} ({int(r['coverage']*100)}% of query terms)  {r['url']}")
        else:
            print(f"NOT COVERED: {args.question}")
            for w in result["weak_matches"]:
                got = '/'.join(w["matched_terms"][:3]) or "no content term"
                print(f"  · {w['page_id']} — only matched {got}, ignored")
    else:
        print(format_text(result))

    return 0 if result["found"] else 1


if __name__ == "__main__":
    sys.exit(main())
