#!/usr/bin/env python3
"""show_diff.py — content delta between the OLD mirror and the freshly-synced doc.

The DingTalk API only gives node-level `contentUpdatedTime`, so incremental sync
can tell *which* doc changed but not *what* changed. This script fills that gap by
comparing the previously-mirrored rendered HTML against the new one, block by
block, and printing a human-readable "新增 X 段 / 改 Y 段 / 删 Z 段" report.

Why diff the RENDERED docs/<slug>.html (not raw/<slug>.html): the raw adoc
innerHTML is full of cangjie editor cruft (styled-component classes, selection
layers) that churns between sessions → noisy false-positive diffs. The cleaned
semantic HTML in docs/ is stable, so its block-level text diff is meaningful.

Old-version source = a SNAPSHOT taken before re-rendering. Because local
docs/<slug>.html still holds the last-synced version until process_batch /
render_files overwrites it, we copy it to prev/<slug>.html first.

Flow inside the skill:
  1. diff_manifest.py            → worklist with `stale` uuids
  2. show_diff.py snapshot ...   → docs/<slug>.html  →  prev/<slug>.html   (BEFORE render)
  3. extract + render            → overwrites docs/<slug>.html with NEW
  4. show_diff.py diff ...       → prev/<slug>.html  vs  docs/<slug>.html  (report)

Usage:
  # snapshot before render (one or more slugs, or a worklist of stale uuids)
  show_diff.py snapshot --slugs <slug> [<slug> ...]
  show_diff.py snapshot --worklist /tmp/ant-worklist.json

  # diff after render
  show_diff.py diff --slugs <slug> [<slug> ...] [--json]
  show_diff.py diff --worklist /tmp/ant-worklist.json [--json]
  show_diff.py diff --old a.html --new b.html        # ad-hoc two-file compare
"""
import sys, json, argparse, shutil, difflib, pathlib, re
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import common as C
from bs4 import BeautifulSoup

STAGE = C.STAGE
DOCS = STAGE / "docs"
PREV = STAGE / "prev"

# Block-level tags we extract as comparable units, in document order.
_BLOCK = {
    "h1": "标题", "h2": "标题", "h3": "标题", "h4": "标题",
    "h5": "标题", "h6": "标题",
    "p": "段落", "li": "列表", "tr": "表格行",
    "pre": "代码", "blockquote": "引用", "img": "图片",
}
_WS = re.compile(r"\s+")


def _norm(s):
    return _WS.sub(" ", (s or "")).strip()


def extract_blocks(html):
    """Rendered HTML → ordered list of (kind, text) blocks (visible content only)."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(".doc") or soup.body or soup
    blocks = []
    # Avoid double-counting: a <p> inside <li> / <td> would otherwise emit twice.
    # We walk only the direct block tags and let table rows collapse their cells.
    for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6",
                             "p", "li", "tr", "pre", "blockquote", "img"]):
        name = el.name
        if name == "pre":
            # code/yaml files render as one <pre>; split per line so a 1-line
            # edit in a 200-line file shows as a 1-line change, not a whole-block churn.
            for ln in el.get_text().split("\n"):
                t = ln.rstrip()
                if t.strip():
                    blocks.append(("代码行", t))
            continue
        if name == "tr":
            cells = [_norm(c.get_text(" ", strip=True))
                     for c in el.find_all(["td", "th"], recursive=False)]
            text = " | ".join(c for c in cells if c)
        elif name == "img":
            text = _norm(el.get("alt") or "") or (C._img_hash(el.get("src", "")) or "image")
            text = "[图] " + text
        elif name in ("p", "li"):
            # skip a <p>/<li> whose text is fully owned by a nested block we also
            # emit (table row); cheap heuristic: skip if it contains a <table>.
            if el.find("table"):
                continue
            text = _norm(el.get_text(" ", strip=True))
        else:
            text = _norm(el.get_text(" ", strip=True))
        if not text:
            continue
        blocks.append((_BLOCK.get(name, "段落"), text))
    return blocks


def _trunc(s, n=80):
    s = _norm(s)
    return s if len(s) <= n else s[:n] + "…"


def diff_blocks(old_blocks, new_blocks):
    """SequenceMatcher over block texts → structured change list + counts."""
    old_t = [t for _, t in old_blocks]
    new_t = [t for _, t in new_blocks]
    sm = difflib.SequenceMatcher(a=old_t, b=new_t, autojunk=False)
    changes = []          # {op, kind, old?, new?}
    counts = {"added": 0, "removed": 0, "changed": 0,
              "by_kind": {}}

    def bump(kind, field):
        counts[field] += 1
        k = counts["by_kind"].setdefault(kind, {"added": 0, "removed": 0, "changed": 0})
        k[field] += 1

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        if op == "insert":
            for j in range(j1, j2):
                kind, text = new_blocks[j]
                changes.append({"op": "added", "kind": kind, "new": text})
                bump(kind, "added")
        elif op == "delete":
            for i in range(i1, i2):
                kind, text = old_blocks[i]
                changes.append({"op": "removed", "kind": kind, "old": text})
                bump(kind, "removed")
        elif op == "replace":
            # Pair old↔new only WITHIN the same block kind (preserving order);
            # cross-kind leftovers are genuine adds/removes, not "changes". This
            # avoids nonsense like reporting a deleted 段落 as "changed into" a
            # newly-added 表格行.
            from collections import defaultdict, deque
            o_by, n_by = defaultdict(deque), defaultdict(deque)
            for b in old_blocks[i1:i2]:
                o_by[b[0]].append(b)
            for b in new_blocks[j1:j2]:
                n_by[b[0]].append(b)
            kinds = list(dict.fromkeys(
                [b[0] for b in old_blocks[i1:i2]] +
                [b[0] for b in new_blocks[j1:j2]]))
            for kind in kinds:
                oq, nq = o_by[kind], n_by[kind]
                while oq and nq:
                    o, n = oq.popleft(), nq.popleft()
                    changes.append({"op": "changed", "kind": kind,
                                    "old": o[1], "new": n[1]})
                    bump(kind, "changed")
                while nq:
                    n = nq.popleft()
                    changes.append({"op": "added", "kind": kind, "new": n[1]})
                    bump(kind, "added")
                while oq:
                    o = oq.popleft()
                    changes.append({"op": "removed", "kind": kind, "old": o[1]})
                    bump(kind, "removed")
    return changes, counts


def _summary_line(counts):
    by = counts["by_kind"]
    parts = []
    for kind, c in by.items():
        seg = []
        if c["added"]:
            seg.append(f"+{c['added']}")
        if c["changed"]:
            seg.append(f"~{c['changed']}")
        if c["removed"]:
            seg.append(f"-{c['removed']}")
        if seg:
            parts.append(f"{kind} {'/'.join(seg)}")
    head = (f"新增 {counts['added']} 块 · 改 {counts['changed']} 块 · "
            f"删 {counts['removed']} 块")
    return head + (("  （" + "，".join(parts) + "）") if parts else "")


def report_one(slug, old_html, new_html, as_json=False):
    """Compute + render the diff report for one slug. Returns a dict."""
    if old_html is None:
        res = {"slug": slug, "status": "new",
               "summary": "全新文档，无旧版可比", "changes": []}
        if not as_json:
            print(f"\n📄 {slug}")
            print("   全新文档（首次镜像），无旧版可比对。")
        return res
    old_b = extract_blocks(old_html)
    new_b = extract_blocks(new_html)
    changes, counts = diff_blocks(old_b, new_b)
    res = {"slug": slug, "status": "diffed",
           "old_blocks": len(old_b), "new_blocks": len(new_b),
           "counts": counts, "summary": _summary_line(counts),
           "changes": changes}
    if as_json:
        return res
    print(f"\n📄 {slug}")
    if not changes:
        print(f"   旧 {len(old_b)} 块 → 新 {len(new_b)} 块：内容无变化（仅元数据/时间戳更新）")
        return res
    print(f"   旧 {len(old_b)} 块 → 新 {len(new_b)} 块")
    print(f"   {res['summary']}")
    sym = {"added": "＋", "removed": "－", "changed": "✎"}
    for ch in changes:
        s = sym[ch["op"]]
        if ch["op"] == "changed":
            print(f"   {s} [{ch['kind']}] {_trunc(ch['old'])}")
            print(f"        → {_trunc(ch['new'])}")
        elif ch["op"] == "added":
            print(f"   {s} [{ch['kind']}] {_trunc(ch['new'])}")
        else:
            print(f"   {s} [{ch['kind']}] {_trunc(ch['old'])}")
    return res


# ── slug resolution ──────────────────────────────────────────────────────────
def slugs_from_worklist(path, which="stale"):
    """Map worklist `stale`/`to_sync` uuids → slugs via the embedded live nodes."""
    wl = json.loads(pathlib.Path(path).read_text())
    live = wl.get("live", {})
    out = []
    for uuid in wl.get(which, []):
        n = live.get(uuid)
        if not n:
            continue
        out.append(C.slugify(n["name"], n["uuid"]))
    return out


# ── subcommands ──────────────────────────────────────────────────────────────
def cmd_snapshot(args):
    PREV.mkdir(parents=True, exist_ok=True)
    slugs = list(args.slugs or [])
    if args.worklist:
        slugs += slugs_from_worklist(args.worklist, "stale")
    if not slugs:
        print("snapshot: no slugs (pass --slugs or --worklist)"); return 1
    done, miss = 0, []
    for slug in slugs:
        src = DOCS / f"{slug}.html"
        if src.exists():
            shutil.copy2(src, PREV / f"{slug}.html")
            done += 1
            print(f"  snapshot {slug}")
        else:
            miss.append(slug)   # new doc, nothing to snapshot
            print(f"  (new) {slug}: no existing mirror, skip snapshot")
    print(f"snapshot: {done} saved → {PREV}"
          + (f", {len(miss)} new (no prev)" if miss else ""))
    return 0


def cmd_diff(args):
    if args.old and args.new:
        old_html = pathlib.Path(args.old).read_text()
        new_html = pathlib.Path(args.new).read_text()
        res = report_one(pathlib.Path(args.new).stem, old_html, new_html, args.json)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    slugs = list(args.slugs or [])
    if args.worklist:
        slugs += slugs_from_worklist(args.worklist, "stale")
    if not slugs:
        print("diff: no slugs (pass --slugs / --worklist / --old+--new)"); return 1
    results = []
    for slug in slugs:
        new_p = DOCS / f"{slug}.html"
        prev_p = PREV / f"{slug}.html"
        if not new_p.exists():
            print(f"  ! {slug}: no new render at {new_p}, skip"); continue
        old_html = prev_p.read_text() if prev_p.exists() else None
        results.append(report_one(slug, old_html,
                                  new_p.read_text(), args.json))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        tot = sum(r.get("counts", {}).get("added", 0)
                  + r.get("counts", {}).get("changed", 0)
                  + r.get("counts", {}).get("removed", 0) for r in results)
        print(f"\n=== diff done: {len(results)} doc(s), {tot} block-level changes ===")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("snapshot", help="copy current docs/<slug>.html → prev/ before re-render")
    sp.add_argument("--slugs", nargs="*")
    sp.add_argument("--worklist")
    sp.set_defaults(func=cmd_snapshot)

    dp = sub.add_parser("diff", help="compare prev/<slug>.html vs docs/<slug>.html")
    dp.add_argument("--slugs", nargs="*")
    dp.add_argument("--worklist")
    dp.add_argument("--old", help="ad-hoc: old HTML file")
    dp.add_argument("--new", help="ad-hoc: new HTML file")
    dp.add_argument("--json", action="store_true")
    dp.set_defaults(func=cmd_diff)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
