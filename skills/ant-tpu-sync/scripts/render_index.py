#!/usr/bin/env python3
"""Render the master 总纲 index.html from manifest.json.

The root document has two jobs at once:
  1. A grand, hand-authored narrative (references/epic_narrative.html) — timeline,
     relationship graph, a 7-stage methodology, insights, multi-path indexes.
  2. A live auto-generated catalog tree of every node (the index property).

This script reads the hand-authored fragment and injects live data into its
placeholders ({{STATS}} {{LAST_SYNC}} {{CATALOG}} {{TOTALDOCS}} {{TOTALNODES}}
{{SPACEID}}), so every incremental sync re-embeds the prose + a fresh tree
without ever overwriting the creative narrative. If the fragment is missing it
falls back to a plain hero + tree.

Usage:
  render_index.py --manifest manifest.json --out index.html
"""
import json, argparse, pathlib, re
import common as C

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

FRAGMENT = pathlib.Path(__file__).resolve().parent.parent / "references" / "epic_narrative.html"

EXT_ICON = {
    "adoc":"📄","md":"📝","pdf":"📕","mp4":"🎬","webm":"🎬","zip":"🗜️",
    "adraw":"🗺️","axls":"📊","py":"🐍","json":"⚙️","yaml":"⚙️","sh":"💻",
    "folder":"📁",
}

# ── catalog tree CSS (shared by narrative + fallback) ──────────────────────
TREE_CSS = """
.hero{background:linear-gradient(120deg,#1A73E8,#1557b0);color:#fff;border-radius:16px;
  padding:32px 36px;margin-bottom:28px}
.hero h1{margin:0 0 8px;font-size:26px;font-weight:500}
.hero p{margin:4px 0;opacity:.92;font-size:14px}
.stats{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0 28px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 20px;flex:1;min-width:120px}
.stat b{display:block;font-size:26px;color:var(--primary);font-weight:500}
.stat span{color:var(--ink-2);font-size:13px}
.section-card{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:8px 8px 12px;margin:18px 0;box-shadow:0 1px 3px rgba(60,64,67,.08)}
.tree{list-style:none;margin:0;padding:0}
.tree li{list-style:none}
.node{display:flex;align-items:center;gap:10px;padding:7px 12px;border-radius:8px;
  text-decoration:none;color:var(--ink);transition:background .12s}
.node:hover{background:var(--accent)}
.node .ic{width:22px;text-align:center;flex:0 0 22px}
.node .nm{flex:1;font-size:14px}
.node .nm.folder{font-weight:500}
.node .tm{color:var(--ink-2);font-size:12px;white-space:nowrap}
.node .by{color:var(--ink-2);font-size:12px;white-space:nowrap;opacity:.8}
.node .badge{font-size:10px;padding:1px 7px;border-radius:10px;background:var(--accent);color:var(--primary-dark)}
.node .badge.stale{background:#fce8e6;color:#c5221f}
.node .badge.new{background:#e6f4ea;color:#188038}
.lvl-1{margin-left:0}.lvl-2{margin-left:22px}.lvl-3{margin-left:44px}
.lvl-4{margin-left:66px}.lvl-5{margin-left:88px}
.fld-row{border-left:2px solid var(--accent)}
.legend{color:var(--ink-2);font-size:12px;margin:8px 4px 14px;display:flex;gap:16px;flex-wrap:wrap}
"""

# ── narrative-specific CSS for epic_narrative.html ─────────────────────────
NARRATIVE_CSS = """
.epic{max-width:920px}
.epic a{color:var(--primary);text-decoration:none}
.epic a:hover{text-decoration:underline}
.ehero{background:radial-gradient(120% 140% at 0% 0%,#1f6feb 0%,#1557b0 55%,#0b3b86 100%);
  color:#fff;border-radius:20px;padding:44px 44px 34px;margin-bottom:30px;
  box-shadow:0 8px 30px rgba(21,87,176,.28)}
.ehero-kicker{font-size:12px;letter-spacing:.12em;text-transform:uppercase;opacity:.82;font-weight:600}
.ehero h1{margin:12px 0 14px;font-size:38px;line-height:1.18;font-weight:600;letter-spacing:-.5px}
.ehero-sub{margin:0;font-size:16px;line-height:1.7;opacity:.94;max-width:680px}
.ehero .stats{margin:26px 0 22px}
.ehero .stat{background:rgba(255,255,255,.13);border:1px solid rgba(255,255,255,.22);backdrop-filter:blur(2px)}
.ehero .stat b{color:#fff}
.ehero .stat span{color:rgba(255,255,255,.85)}
.ehero-nav{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}
.ehero-nav a{background:rgba(255,255,255,.16);color:#fff;border:1px solid rgba(255,255,255,.25);
  border-radius:20px;padding:7px 15px;font-size:13px;font-weight:500;transition:background .15s}
.ehero-nav a:hover{background:rgba(255,255,255,.28);text-decoration:none}
.prose{font-size:15.5px;line-height:1.85;color:var(--ink);margin:14px 0 8px}
.prose p{margin:10px 0}
.prose strong{color:var(--ink);font-weight:600}
.prose em{color:var(--ink-2);font-style:normal;background:var(--accent);padding:1px 6px;border-radius:5px}
.prose blockquote{margin:18px 0;padding:16px 20px;background:var(--accent);
  border-left:4px solid var(--primary);border-radius:0 10px 10px 0;font-size:15px;line-height:1.8;color:var(--ink)}
.sec{font-size:24px;font-weight:600;margin:46px 0 6px;letter-spacing:-.3px;
  scroll-margin-top:20px;display:flex;align-items:center;gap:12px}
.sec .num{display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;
  background:var(--primary);color:#fff;border-radius:11px;font-size:18px;flex:0 0 38px}
/* ① timeline */
.tl{position:relative;margin:26px 0 8px;padding-left:34px}
.tl::before{content:"";position:absolute;left:11px;top:8px;bottom:8px;width:2px;
  background:linear-gradient(#1A73E8,#34a853,#fbbc04,#ea4335)}
.tl-item{position:relative;margin-bottom:24px}
.tl-dot{position:absolute;left:-29px;top:4px;width:16px;height:16px;border-radius:50%;
  border:3px solid #fff;box-shadow:0 0 0 2px var(--line)}
.tl-dot.a{background:#1A73E8}.tl-dot.b{background:#34a853}
.tl-dot.c{background:#fbbc04}.tl-dot.d{background:#ea4335}
.tl-when{font-size:13px;font-weight:700;color:var(--ink-2);letter-spacing:.04em;margin-bottom:6px}
.tl-card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 22px;
  box-shadow:0 1px 3px rgba(60,64,67,.08)}
.tl-phase{display:inline-block;font-size:11.5px;font-weight:700;color:var(--primary-dark);
  background:var(--accent);padding:3px 10px;border-radius:20px;margin-bottom:8px}
.tl-card h3{margin:4px 0 8px;font-size:18px;font-weight:600}
.tl-card p{margin:0;font-size:14.5px;line-height:1.8;color:var(--ink)}
.tl-refs{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;padding-top:12px;border-top:1px dashed var(--line)}
.xref{display:inline-block;font-size:12.5px;background:var(--accent);color:var(--primary-dark)!important;
  padding:4px 10px;border-radius:7px;font-weight:500;transition:background .12s}
.xref:hover{background:#d2e3fc;text-decoration:none!important}
/* ② graph */
.graph{display:flex;flex-direction:column;gap:16px;margin:24px 0 8px}
.branch{border:1px solid var(--line);border-radius:14px;overflow:hidden;background:var(--card);
  box-shadow:0 1px 3px rgba(60,64,67,.08)}
.branch-head{display:flex;align-items:center;gap:14px;padding:16px 20px;
  background:linear-gradient(90deg,var(--accent),transparent)}
.branch-head .bi{font-size:24px}
.branch-head b{display:block;font-size:16px;font-weight:600}
.branch-head i{font-style:normal;font-size:12.5px;color:var(--ink-2)}
.b-origin .branch-head{border-left:4px solid #1A73E8}
.b-model .branch-head{border-left:4px solid #34a853}
.b-perf .branch-head{border-left:4px solid #fbbc04}
.b-eng .branch-head{border-left:4px solid #ea4335}
.b-collab .branch-head{border-left:4px solid #9334e6}
.branch-body{padding:16px 20px}
.bdesc{margin:0 0 12px;font-size:14px;line-height:1.75;color:var(--ink-2)}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{display:inline-block;font-size:13px;background:var(--bg);border:1px solid var(--line);
  color:var(--ink)!important;padding:6px 12px;border-radius:8px;transition:all .12s}
.chip:hover{border-color:var(--primary);background:var(--accent);text-decoration:none!important}
.subgraph{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.sg{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.sg-t{font-size:12.5px;font-weight:700;color:var(--ink-2);margin-bottom:10px;
  text-transform:uppercase;letter-spacing:.04em}
.sg .chip{display:inline-block;margin:0 6px 6px 0;background:var(--card)}
/* ③ methodology */
.stages{display:flex;flex-direction:column;gap:14px;margin:24px 0 8px}
.stage{display:flex;gap:18px;background:var(--card);border:1px solid var(--line);
  border-radius:14px;padding:20px 22px;box-shadow:0 1px 3px rgba(60,64,67,.08)}
.stage-no{flex:0 0 44px;width:44px;height:44px;border-radius:12px;background:var(--primary);
  color:#fff;font-size:22px;font-weight:700;display:flex;align-items:center;justify-content:center}
.stage-main{flex:1}
.stage-main h3{margin:2px 0 8px;font-size:18px;font-weight:600}
.stage-main p{margin:0 0 10px;font-size:14.5px;line-height:1.8;color:var(--ink)}
.stage-main ul{margin:8px 0 0;padding-left:20px}
.stage-main li{font-size:14px;line-height:1.75;margin:5px 0;color:var(--ink)}
.stage-main b{font-weight:600}
.stage-main code{background:var(--accent);padding:1px 6px;border-radius:5px;font-size:13px}
.ev{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;padding-top:12px;border-top:1px dashed var(--line)}
.kv{display:flex;flex-direction:column;gap:10px;margin:6px 0 4px}
.kv>div{display:grid;grid-template-columns:140px 1fr;gap:14px;align-items:start;
  padding:10px 0;border-bottom:1px solid var(--line)}
.kv>div:last-child{border-bottom:none}
.kv b{font-size:14px;color:var(--primary-dark)}
.kv span{font-size:14px;line-height:1.7;color:var(--ink)}
/* ④ insights */
.insights{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin:24px 0 8px}
.ins{display:flex;gap:14px;background:var(--card);border:1px solid var(--line);
  border-radius:14px;padding:18px 20px;box-shadow:0 1px 3px rgba(60,64,67,.08)}
.ins-i{font-size:26px;flex:0 0 30px}
.ins b{display:block;font-size:15.5px;font-weight:600;margin-bottom:6px}
.ins p{margin:0;font-size:13.5px;line-height:1.7;color:var(--ink-2)}
/* ⑤ multi-index */
.midx{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin:22px 0 8px}
.midx-col{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px}
.midx-col h4{margin:0 0 12px;font-size:15px;font-weight:600;color:var(--primary-dark);
  padding-bottom:8px;border-bottom:2px solid var(--accent)}
.midx-g{font-size:13.5px;line-height:1.9;margin:6px 0}
.tag{display:inline-block;font-size:11px;font-weight:700;padding:1px 8px;border-radius:10px;
  background:var(--accent);color:var(--primary-dark);margin-right:4px}
.tag.a{background:#e8f0fe;color:#1A73E8}.tag.b{background:#e6f4ea;color:#188038}
.tag.c{background:#fef7e0;color:#b06000}.tag.d{background:#fce8e6;color:#c5221f}
.muted{color:var(--ink-2);font-size:13px}
@media(max-width:640px){
  .ehero{padding:30px 24px}.ehero h1{font-size:28px}
  .kv>div{grid-template-columns:1fr;gap:2px}
  .stage{flex-direction:column;gap:10px}
}
"""


def build_tree(nodes):
    """Sort nodes so folders precede their contents (lexicographic by path)."""
    return sorted(nodes, key=lambda n: tuple(n["path"].split(" / ")))


def node_link(n):
    if n["type"] == "folder":
        return ""
    if n.get("local_html"):
        return f'{C.URL_PREFIX}/{n["local_html"]}'
    if n.get("local_asset"):
        return f'{C.ASSET_URL}/{n["local_asset"]}'
    return n.get("dingtalk_url", "")  # fallback: link to DingTalk original


def build_catalog(nodes):
    rows = []
    for n in build_tree(nodes):
        depth = n.get("depth", 0) + 1
        ic = EXT_ICON.get(n["type"] if n["type"] == "folder" else n.get("ext", ""), "📄")
        nm = C.esc(n["name"])
        href = node_link(n)
        cu = C.hkt(n.get("dingtalk_content_updated") or n.get("dingtalk_updated"))
        by = C.esc(n.get("updator", "") or "")
        status = n.get("status", "synced")
        badge = ""
        if status == "stale":
            badge = '<span class="badge stale">待更新</span>'
        elif status == "new":
            badge = '<span class="badge new">新</span>'
        is_folder = n["type"] == "folder"
        nm_cls = "nm folder" if is_folder else "nm"
        tail = "" if is_folder else (
            f'<span class="by">{by}</span><span class="tm">{cu}</span>{badge}')
        tag = "div" if (is_folder or not href) else "a"
        attr = f' href="{href}"' if tag == "a" else ""
        rows.append(
            f'<{tag} class="node lvl-{min(depth,5)}{" fld-row" if is_folder else ""}"{attr}>'
            f'<span class="ic">{ic}</span>'
            f'<span class="{nm_cls}">{nm}</span>{tail}</{tag}>')
    return '<div class="section-card"><div class="tree">' + "".join(rows) + "</div></div>"


def stats_block(files, folders, docs, bins):
    return (f'<div class="stats">'
            f'<div class="stat"><b>{len(files)}</b><span>文件</span></div>'
            f'<div class="stat"><b>{len(folders)}</b><span>目录</span></div>'
            f'<div class="stat"><b>{len(docs)}</b><span>文档镜像</span></div>'
            f'<div class="stat"><b>{len(bins)}</b><span>媒体/附件</span></div>'
            f'</div>')


def render(manifest):
    nodes = list(manifest["nodes"].values())
    files = [n for n in nodes if n["type"] == "file"]
    folders = [n for n in nodes if n["type"] == "folder"]
    docs = [n for n in files if n.get("ext") in ("adoc", "md", "py", "json", "yaml", "sh")]
    bins = [n for n in files if n.get("ext") in ("pdf", "mp4", "webm", "zip", "adraw", "axls")]

    catalog = build_catalog(nodes)
    stats = stats_block(files, folders, docs, bins)
    last_sync = manifest.get("last_full_sync") or C.now_hkt()

    if FRAGMENT.exists():
        body = _COMMENT_RE.sub("", FRAGMENT.read_text(encoding="utf-8"))  # drop authoring notes
        body = (body
                .replace("{{STATS}}", stats)
                .replace("{{CATALOG}}", catalog)
                .replace("{{LAST_SYNC}}", last_sync)
                .replace("{{TOTALDOCS}}", str(len(docs)))
                .replace("{{TOTALNODES}}", str(len(nodes)))
                .replace("{{SPACEID}}", C.SPACE_ID))
        return C.page_shell("总纲", body, crumb="在 TPU 上重生一个大模型",
                            extra_css=TREE_CSS + NARRATIVE_CSS)

    # fallback: plain hero + tree (fragment missing)
    hero = (f'<div class="hero"><h1>蚂蚁 TPU 项目资料库 · 总纲</h1>'
            f'<p>钉钉知识库全量镜像 · 让整个信息体系一目了然</p>'
            f'<p style="opacity:.8">源空间 {C.SPACE_ID} · 最后全量同步 {last_sync}</p></div>')
    legend = ('<div class="legend"><span>📄 文档</span><span>🐍⚙️💻 代码/配置</span>'
              '<span>📕 PDF</span><span>🎬 视频</span><span>🗜️ 压缩包</span>'
              '<span>点击节点 → 打开镜像 · 时间为钉钉最后编辑</span></div>')
    body = f'<div class="wrap">{hero}{stats}{legend}{catalog}</div>'
    return C.page_shell("总纲", body, crumb="全部内容", extra_css=TREE_CSS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    man = json.load(open(args.manifest, encoding="utf-8"))
    html = render(man)
    open(args.out, "w", encoding="utf-8").write(html)
    print(f"wrote {args.out} ({len(html)} bytes, {len(man['nodes'])} nodes)")


if __name__ == "__main__":
    main()
