#!/usr/bin/env python3
"""Render one DingTalk node into a Material-styled mirror HTML page.

Three input modes (--mode):
  adoc : raw innerHTML of `.body-editor-content` (scraped via extract_adoc.js)
         → BS4 clean (strip editor chrome / attrs / svg) → semantic HTML
  md   : raw markdown text → python-markdown → HTML
  code : raw source text → escaped <pre> with a header bar

Metadata (title / path / updator / dingtalk_content_updated ms / status) comes
from --meta (JSON). Image localization map (orig src → local /assets URL) via
--imgmap (JSON dict); unmapped images keep their original src as fallback.

Usage:
  render_doc.py --mode adoc --in raw.html --meta meta.json \
                --imgmap imgs.json --out docs/<slug>.html
  echo "$SRC" | render_doc.py --mode code --lang python --meta meta.json --out ...
"""
import sys, json, argparse, re, html as _html
import markdown as md_lib
from bs4 import BeautifulSoup, NavigableString
import common as C

# Tags we keep in cleaned adoc HTML; everything else is unwrapped (children kept).
KEEP = {"h1","h2","h3","h4","h5","h6","p","br","hr","ul","ol","li",
        "table","thead","tbody","tr","td","th","img","a","blockquote",
        "pre","code","strong","b","em","i","u","s","del","sup","sub","mark","span"}
DROP_TREE = {"svg","script","style","button","input","textarea","noscript","iframe"}
ATTR_KEEP = {"a":{"href"}, "img":{"src","alt","data-src"}, "td":{"colspan","rowspan"},
             "th":{"colspan","rowspan"}, "p":{"class"}, "mark":{"class"}}


# native nested lists: browsers render disc/circle/square markers by depth and
# align marker+text+indent automatically. minimal spacing to match Material look.
LVL_CSS = (
 ".doc ul{margin:.5em 0;padding-left:1.6em}"
 ".doc ul ul{margin:.2em 0}"
 ".doc li{margin:.3em 0;line-height:1.7}"
 ".doc li::marker{color:var(--accent)}"
 ".doc p.lvl0{margin:.7em 0}"
 ".doc p.code-label{margin:.8em 0 0;font:12px 'JetBrains Mono',monospace;color:var(--ink-2)}"
 ".doc p.code-label strong{color:var(--primary-dark)}"
 ".doc p.code-label + pre{margin-top:.3em}"
)


def _img_hash(src):
    # stable id = the resource hash after /resources/img/, ignore domain + query
    m = re.search(r"/resources/img/([0-9a-f]+)", src or "")
    return m.group(1) if m else None


def _guess_lang(code):
    head = "\n".join(code.split("\n")[:6])
    if head.startswith("#!/") and "bash" in head.split("\n")[0]:
        return "bash"
    if re.search(r"^\s*(apiVersion|kind|metadata|spec)\s*:", head, re.M):
        return "yaml"
    if re.search(r"^\s*(def |class |import |from \w+ import)", head, re.M):
        return "python"
    if re.search(r"\b(gcloud|kubectl|cd |export |sudo |pip |python |git )", head):
        return "bash"
    if head.lstrip().startswith(("{", "[")):
        return "json"
    return ""


def extract_code_editor(el, soup):
    # DingTalk code blocks are CodeMirror widgets marked not-editable. get_text()
    # over the widget yields only the line-number gutter; the real code lives in
    # `.cm-line` elements. Pull those, join with newlines, and emit a bare
    # <pre><code> (survives the div-unwrap in step 3; .doc pre carries dark theme).
    # Returns the replacement node, or None if no code lines (let caller fall back).
    lines = el.select(".cm-line")
    if not lines:
        return None
    code = "\n".join(l.get_text() for l in lines)
    if not code.strip():
        return None
    tt = el.find(attrs={"data-testid": "code-toolbar-title-content"})
    label = (tt.get_text(strip=True) if tt else "") or _guess_lang(code)
    head = f'<p class="code-label"><strong>{C.esc(label)}</strong></p>' if label else ""
    frag = BeautifulSoup(f'{head}<pre><code>{C.esc(code)}</code></pre>', "html.parser")
    return frag


def _lvl_of(el):
    if getattr(el, "name", None) != "p":
        return 0
    for c in (el.get("class") or []):
        if c.startswith("lvl"):
            try:
                return int(c[3:])
            except ValueError:
                return 0
    return 0


def group_lvl_lists(soup, root):
    # merge runs of consecutive top-level <p class="lvlN"> (N>=1) into native
    # nested <ul><li> so the browser aligns markers + text + indent. lvl0 / headings
    # / tables stay as-is and terminate a run.
    children = [c for c in list(root.children) if getattr(c, "name", None)]
    idx, n = 0, len(children)
    while idx < n:
        if _lvl_of(children[idx]) >= 1:
            run, j = [], idx
            while j < n and _lvl_of(children[j]) >= 1:
                run.append(children[j]); j += 1
            base = min(_lvl_of(e) for e in run)
            top_ul = soup.new_tag("ul")
            run[0].insert_before(top_ul)
            stack = [top_ul]  # stack[k] = <ul> at nesting depth k+1
            for e in run:
                depth = _lvl_of(e) - base + 1
                while len(stack) > depth:
                    stack.pop()
                while len(stack) < depth:
                    parent_ul = stack[-1]
                    last_li = None
                    for ch in reversed(list(parent_ul.children)):
                        if getattr(ch, "name", None) == "li":
                            last_li = ch; break
                    if last_li is None:
                        last_li = soup.new_tag("li"); parent_ul.append(last_li)
                    nu = soup.new_tag("ul"); last_li.append(nu); stack.append(nu)
                li = soup.new_tag("li")
                for child in list(e.contents):
                    li.append(child)
                stack[-1].append(li)
                e.decompose()
            idx = j
        else:
            idx += 1

def merge_orphan_numbers(root):
    # DingTalk numbered headings / list items split the leading "N." into a separate
    # list-symbol node that survives as a bare text orphan after span-unwrap (e.g.
    # "1." sitting right before <h2>目标</h2>). Re-attach each "N." to the following
    # heading/paragraph/li so numbered sections keep their number; drop it if nothing
    # block-level follows. Pattern needs trailing .、) so table-cell data ("12","16")
    # never matches.
    num_re = re.compile(r"^\s*(\d+[.、)）])\s*$")
    for t in list(root.find_all(string=True)):
        m = num_re.match(str(t))
        if not m:
            continue
        if t.find_parent(["td", "th", "pre", "code", "a", "li"]):
            continue
        nxt = t.next_sibling
        while nxt is not None and getattr(nxt, "name", None) is None and not str(nxt).strip():
            nxt = nxt.next_sibling
        num = m.group(1)
        if getattr(nxt, "name", None) in ("h1","h2","h3","h4","h5","h6","p","li"):
            first = nxt.find(string=True)
            if first is not None:
                first.replace_with(num + " " + str(first))
            else:
                nxt.insert(0, NavigableString(num + " "))
        t.replace_with("")


def clean_adoc(raw_html, imgmap):
    # secondary index by resource hash so relative ("/core/...") and absolute
    # ("https://alidocs.../core/...") srcs both resolve to the local asset URL
    imgmap_by_hash = {}
    for k, v in imgmap.items():
        h = _img_hash(k)
        if h:
            imgmap_by_hash[h] = v
    soup = BeautifulSoup(raw_html, "lxml")
    # lxml wraps in <html><body>; operate on body
    root = soup.body or soup
    def alive(el):  # decomposed nodes lose attrs / parent
        return el is not None and el.attrs is not None and (el is root or el.parent is not None)
    # 1. drop whole subtrees (editor chrome, icons)
    #    table-* data-testid = DingTalk table drag/resize/toolbar chrome whose text
    #    ("正在移动0列表格") leaks after every table; cangjie-selection-layer /
    #    drag-hander = selection + drag handles. Verified no data-testid wraps a real
    #    <table>, so dropping these never removes table content. MUST run before step 2
    #    strips data-testid.
    for el in list(root.find_all(True)):
        if not alive(el):
            continue
        if el.name in DROP_TREE:
            el.decompose(); continue
        tid = el.attrs.get("data-testid", "")
        if tid.startswith("table-") or tid in ("cangjie-selection-layer", "drag-hander"):
            el.decompose(); continue
        # code block (CodeMirror widget): extract .cm-line code BEFORE the
        # not-editable branch decomposes it (code-editor IS not-editable).
        if tid == "code-editor" or el.attrs.get("data-type") == "code":
            repl = extract_code_editor(el, soup)
            if repl is not None:
                el.replace_with(repl)
            else:
                el.decompose()
            continue
        if el.attrs.get("data-cangjie-not-editable") == "true":
            # 河图 (hetu) reference chip: its visible title lives INSIDE a
            # not-editable container (data-hetu-id). Dropping the not-editable
            # subtree wholesale also drops the title, so the reader loses the
            # "there's a referenced table named X here" signal. Preserve the
            # title as an inline chip; the table's actual data lives in a
            # separate embedded 河图 (mirrored elsewhere).
            is_hetu = (el.get("data-hetu-id") or el.get("data-hetu-type")
                       or "hetu-container" in (el.get("class") or []))
            if is_hetu:
                title = el.get_text(strip=True)
                if title:
                    chip = soup.new_tag("mark")
                    chip["class"] = ["hetu-ref"]
                    chip.string = "\U0001F4CA " + title
                    el.replace_with(chip)
                    continue
            el.decompose(); continue
        if el.attrs.get("id") == "doc-title-area":
            el.decompose()
    # 1.5 reconstruct block structure: each DingTalk cangjie leaf-block is one
    #     logical line. Real headings already carry h1-h6 tags. Other text lines
    #     become <p class="lvlN"> where N = group-block nesting depth, so nested
    #     outlines render as indented, bulleted blocks instead of one text wall.
    for blk in list(root.find_all(attrs={"data-cangjie-leaf-block": "true"})):
        if not alive(blk):
            continue
        if blk.name in ("h1","h2","h3","h4","h5","h6") or blk.find("table"):
            continue
        depth = 0
        for anc in blk.parents:
            if anc is root:
                break
            if getattr(anc, "attrs", None) and anc.attrs.get("data-cangjie-group-block") == "true":
                depth += 1
        blk.name = "p"
        blk.attrs = {"class": ["lvl%d" % min(depth, 5)]}
    # 1.7 recover lazy-image boxes that never got an <img>. DingTalk renders
    #     above-fold images with a real <img src>; below-fold ones stay as an
    #     editor-image-real-box whose only URL is an inner <div data-src=...>
    #     (full 192-char hash, exact-matches an imgmap entry). Without this every
    #     lazy image past the fold is silently dropped (e.g. TPU-GKE-指标 10/12).
    for box in list(root.find_all(attrs={"data-testid": "editor-image-real-box"})):
        if not alive(box) or box.find("img"):
            continue
        dsrc = ""
        for d in box.find_all(True):
            if d.get("data-src"):
                dsrc = d["data-src"]; break
        h = _img_hash(dsrc)
        local = imgmap_by_hash.get(h) if h else None
        if not local:
            continue
        nimg = soup.new_tag("img")
        nimg["src"] = local
        nimg["loading"] = "lazy"
        box.replace_with(nimg)
    # 2. strip attributes (keep whitelisted per tag) + localize images
    for el in list(root.find_all(True)):
        if not alive(el):
            continue
        keep = ATTR_KEEP.get(el.name, set())
        for a in list(el.attrs):
            if a not in keep:
                del el[a]
        if el.name == "img":
            # lazy images carry the real URL in data-src; only loaded ones have a
            # usable src. resolve hash from either, rewrite to the local asset.
            src = el.get("src","") or ""
            dsrc = el.get("data-src","") or ""
            h = _img_hash(src) or _img_hash(dsrc)
            local = None
            if src in imgmap:
                local = imgmap[src]
            elif dsrc in imgmap:
                local = imgmap[dsrc]
            elif h and h in imgmap_by_hash:
                local = imgmap_by_hash[h]
            if local:
                el["src"] = local
            elif dsrc and not _img_hash(src):
                el["src"] = dsrc  # fallback to remote lazy URL if unmapped
            if el.has_attr("data-src"):
                del el["data-src"]
            el["loading"] = "lazy"
    # 3. unwrap non-kept tags
    for el in root.find_all(True):
        if el.name not in KEEP:
            el.unwrap()
    # 4. drop empty block elements (no text, no media)
    for _ in range(3):  # iterate: emptying children can empty parents
        for el in root.find_all(["div","p","span","li"]):
            if not el.get_text(strip=True) and not el.find(["img","table","hr","br"]):
                el.decompose()
    # 5. collapse spans (inline) into text where harmless
    for el in root.find_all("span"):
        el.unwrap()
    # 6. strip zero-width / BOM chars + drop bullet-glyph-only nodes (DingTalk
    #    fold-button markers ●○■▪ that survived as bare text after span unwrap)
    bullet_only = re.compile(r"^[\s·•‣⁃■-◿]+$")
    for t in root.find_all(string=True):
        if t.find_parent(["pre", "code"]):
            continue  # never touch code content (blank lines, glyph-like chars)
        cleaned = t.replace("﻿","").replace("​","").replace("⁠","")
        if cleaned and bullet_only.match(cleaned):
            cleaned = ""
        if cleaned != t:
            t.replace_with(cleaned)
    # 7. drop now-empty blocks again
    for el in root.find_all(["div","p","li"]):
        if not el.get_text(strip=True) and not el.find(["img","table","hr","br"]):
            el.decompose()
    # 8. merge lvlN paragraph runs into native nested <ul><li>
    group_lvl_lists(soup, root)
    # 9. re-attach orphan ordered-list numbers ("1." before <h2>) to their block
    merge_orphan_numbers(root)
    # 10. empty-doc guard: DingTalk renders blank docs as the editor placeholder
    #     '输入"/"插入或帮我写关于"X"的文档'. Surface a clear empty note instead.
    txt = root.get_text(strip=True)
    if root.find(["img", "table"]) is None and (
        ("插入或帮我写关于" in txt and "的文档" in txt) or not txt
    ):
        return '<p class="empty-doc">（此文档在钉钉原文中为空）</p>'
    body = root.decode_contents() if hasattr(root, "decode_contents") else str(root)
    return body


def render_code(src, lang):
    head = f'<div class="hd"><span>{C.esc(lang or "text")}</span></div>'
    return f'<div class="codewrap">{head}<pre><code>{C.esc(src)}</code></pre></div>'


def render_md(text):
    return md_lib.markdown(text, extensions=["tables","fenced_code","toc",
                                             "sane_lists","nl2br"])


def meta_bar(meta):
    cu = C.hkt(meta.get("dingtalk_content_updated") or meta.get("dingtalk_updated"))
    synced = meta.get("local_synced_at") or C.now_hkt()
    status = meta.get("status","synced")
    pill = '<span class="pill stale">待更新</span>' if status == "stale" else '<span class="pill">已同步</span>'
    parts = [pill]
    if meta.get("updator"):
        parts.append(f'最后编辑 <b>{C.esc(meta["updator"])}</b>')
    parts.append(f'钉钉编辑时间 {cu}')
    parts.append(f'本地同步 {synced}')
    if meta.get("path"):
        parts.append(f'<span style="opacity:.7">{C.esc(meta["path"])}</span>')
    return '<div class="meta">' + " ".join(f"<span>{p}</span>" for p in parts) + "</div>"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["adoc","md","code"])
    ap.add_argument("--in", dest="infile", help="input file (default stdin)")
    ap.add_argument("--meta", required=True, help="metadata JSON file")
    ap.add_argument("--imgmap", help="image map JSON {origSrc: localUrl}")
    ap.add_argument("--lang", default="", help="code language (code mode)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    raw = open(args.infile, encoding="utf-8").read() if args.infile else sys.stdin.read()
    meta = json.load(open(args.meta, encoding="utf-8"))
    imgmap = json.load(open(args.imgmap, encoding="utf-8")) if args.imgmap else {}

    if args.mode == "adoc":
        body_inner = clean_adoc(raw, imgmap)
    elif args.mode == "md":
        body_inner = render_md(raw)
    else:
        body_inner = render_code(raw, args.lang)

    title = meta.get("name","").rsplit(".",1)[0]
    crumb = C.esc(meta.get("path","")) if meta.get("path") else ""
    body = (f'<div class="wrap"><h1 class="doc-title">{C.esc(title)}</h1>'
            f'{meta_bar(meta)}<div class="doc">{body_inner}</div></div>')
    htmlout = C.page_shell(title, body, crumb=crumb, extra_css=LVL_CSS)
    open(args.out, "w", encoding="utf-8").write(htmlout)
    print(f"wrote {args.out} ({len(htmlout)} bytes)")


if __name__ == "__main__":
    main()
