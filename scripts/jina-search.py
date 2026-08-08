#!/usr/bin/env python3
"""Local fallback for Jina search when mcp.jina.ai's search handlers are down.

Talks to s.jina.ai / r.jina.ai directly — those stayed healthy during the
2026-08-02 mcp.jina.ai outage where every search_* MCP tool returned
"Internal Server Error" while tools/list and read_url kept working.

Usage:
  jina-search.py "query"                    # web search
  jina-search.py "query" --num 20
  jina-search.py "q1" "q2" "q3"             # parallel
  jina-search.py "query" --site arxiv.org
  jina-search.py --read https://example.com # fetch a page as markdown
  jina-search.py "query" --json
"""
import argparse, json, os, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

KEY = os.environ.get("JINA_API_KEY") or (
    "jina_35cda26e034c43a0a8b5d6bf2a09f788EQgf2q-ne4-uQZZG1-INrOlwhWQd"
)


def _get(url, accept="application/json", timeout=60, extra=None):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {KEY}",
        "Accept": accept,
        "User-Agent": "Mozilla/5.0",
        **(extra or {}),
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def search(query, num=10, site=None, gl=None, hl=None, backend="ddg"):
    """Default backend is DuckDuckGo; s.jina.ai kept as fallback."""
    if backend == "ddg":
        import time as _t
        for attempt in range(3):
            try:
                r = _ddg(query, num, site)
                if r["results"] and not _looks_bogus(query, r["results"]):
                    return r
            except Exception:
                pass
            _t.sleep(2.5 * (attempt + 1))   # DDG throttles bursts; back off
        # 明确告知降级，避免把垃圾结果当真结果用
        j = _jina_search(query, num, site, gl, hl)
        j["degraded"] = "ddg throttled or returned off-topic results"
        return j
    return _jina_search(query, num, site, gl, hl)


_BOGUS_HOSTS = ("support.microsoft.com", "techcommunity.microsoft.com",
                "dell.com", "unep.org", "sensorstechforum.com")


def _looks_bogus(query, results):
    """DDG/Jina sometimes return a generic filler page under throttling.
    Heuristic: most hits from one unrelated mega-domain and zero token overlap."""
    if sum(any(h in (r["url"] or "") for h in _BOGUS_HOSTS)
           for r in results) >= max(2, len(results) // 2):
        return True
    import re as _re
    toks = [t.lower() for t in _re.findall(r"[A-Za-z]{4,}|[\u4e00-\u9fff]{2,}", query)]
    if not toks:
        return False
    blob = " ".join(((r["title"] or "") + " " + (r["desc"] or "")).lower()
                    for r in results)
    return not any(t in blob for t in toks)


def _jina_search(query, num=10, site=None, gl=None, hl=None):
    q = f"site:{site} {query}" if site else query
    url = "https://s.jina.ai/?q=" + urllib.parse.quote(q)
    extra = {"X-Respond-With": "no-content"}
    if gl:
        extra["X-Country"] = gl
    if hl:
        extra["X-Locale"] = hl
    try:
        d = json.loads(_get(url, extra=extra))
    except Exception as e:
        return {"query": query, "error": str(e), "results": []}
    items = d.get("data") or []
    return {"query": query, "results": [
        {"title": i.get("title"), "url": i.get("url"),
         "desc": (i.get("description") or "")[:300],
         "date": i.get("date"), "source": i.get("source")}
        for i in items[:num]
    ]}


# --- DuckDuckGo backend (keyless). Added 2026-08-02 after s.jina.ai started
# --- returning irrelevant results during the mcp.jina.ai outage. Default.
def _ddg(query, num=10, site=None):
    import re as _re, html as _html
    q = f"site:{site} {query}" if site else query
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 "
        "(Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like "
        "Gecko) Chrome/131.0 Safari/537.36"})
    h = urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "ignore")
    blocks = _re.split(r'<div class="result results_links', h)[1:]
    out = []
    for b in blocks:
        a = _re.search(r'class="result__a" href="([^"]+)".*?>(.*?)</a>', b, _re.S)
        if not a:
            continue
        u = _html.unescape(a.group(1))
        if "uddg=" in u:
            u = urllib.parse.unquote(_re.search(r"uddg=([^&]+)", u).group(1))
        t = _re.sub(r"<[^>]+>", "", _html.unescape(a.group(2))).strip()
        d = _re.search(r'class="result__snippet"[^>]*>(.*?)</a>', b, _re.S)
        desc = _re.sub(r"<[^>]+>", "", _html.unescape(d.group(1))).strip() if d else ""
        out.append({"title": t, "url": u, "desc": desc[:300],
                    "date": None, "source": None})
        if len(out) >= num:
            break
    return {"query": query, "results": out}


def read(url):
    return _get("https://r.jina.ai/" + url, accept="text/plain", timeout=90)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="*")
    ap.add_argument("--num", type=int, default=10)
    ap.add_argument("--site")
    ap.add_argument("--gl")
    ap.add_argument("--hl")
    ap.add_argument("--read", metavar="URL")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.read:
        print(read(a.read))
        return
    if not a.queries:
        ap.error("need a query or --read URL")

    with ThreadPoolExecutor(max_workers=min(5, len(a.queries))) as ex:
        out = list(ex.map(
            lambda q: search(q, a.num, a.site, a.gl, a.hl), a.queries))

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    for blk in out:
        print(f"\n=== {blk['query']}")
        if blk.get("degraded"):
            print(f"  ⚠️  降级: {blk['degraded']} — 结果可能不相关，建议改用浏览器")
        if blk.get("error"):
            print("  ERROR:", blk["error"])
        for i, r in enumerate(blk["results"], 1):
            print(f"  {i:2d}. {r['title']}")
            print(f"      {r['url']}")
            if r["desc"]:
                print(f"      {r['desc'][:160]}")


if __name__ == "__main__":
    main()
