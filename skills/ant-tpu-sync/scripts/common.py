"""Shared helpers for ant-tpu-sync: Material CSS shell, slug, paths, manifest IO.

The whole skill mirrors a DingTalk knowledge base (space 15eGe6boVpjOez3N,
"TPU项目资料库") into CC Pages as static HTML. This module holds the bits both
render_doc.py and render_index.py need so styling/paths stay consistent.
"""
import json, re, hashlib, unicodedata, datetime, pathlib

# ── Constants ──────────────────────────────────────────────────────────────
SPACE_ID  = "15eGe6boVpjOez3N"
ROOT_UUID = "pGBa2Lm8aGLllAvacMD4Q7v9VgN7R35y"
HKT       = datetime.timezone(datetime.timedelta(hours=8))

# GCS-backed CC Pages local mount (gcsfuse) — but we WRITE via publish-cc-page.sh,
# staging files here first.
STAGE      = pathlib.Path.home() / ".cache" / "ant-tpu-sync"
PAGES_BASE = "ant-tpu"                      # pages/ant-tpu/...
URL_PREFIX = "https://cc.higcp.com/pages/ant-tpu"
ASSET_URL  = "https://cc.higcp.com/assets/ant-tpu"

# ── Time ───────────────────────────────────────────────────────────────────
def hkt(ms):
    if not ms:
        return "—"
    return datetime.datetime.fromtimestamp(ms / 1000, HKT).strftime("%Y-%m-%d %H:%M")

def now_hkt():
    return datetime.datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT")

# ── Slug ───────────────────────────────────────────────────────────────────
def slugify(name, uuid=""):
    """Stable, filesystem + URL safe slug. CJK kept (URL-encoded by browser),
    spaces/punct collapsed to '-'. UUID suffix guarantees uniqueness."""
    base = name.rsplit(".", 1)[0]  # drop extension
    base = unicodedata.normalize("NFKC", base).strip()
    base = re.sub(r"[\s/\\]+", "-", base)
    base = re.sub(r"[^\w一-鿿\-]", "", base)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    suffix = (uuid or hashlib.md5(name.encode()).hexdigest())[:6]
    return f"{base}-{suffix}" if base else suffix

def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

# ── Manifest IO ────────────────────────────────────────────────────────────
def load_manifest(path):
    p = pathlib.Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return {"space_id": SPACE_ID, "root_uuid": ROOT_UUID,
            "last_full_sync": None, "nodes": {}}

def save_manifest(path, m):
    pathlib.Path(path).write_text(json.dumps(m, ensure_ascii=False, indent=2))

# ── Material Design shell ──────────────────────────────────────────────────
# Clean Google-Cloud-style Material. Targets semantic tags so cleaned DingTalk
# content (class-stripped) renders well.
CSS = """
:root{
  --primary:#1A73E8;--primary-dark:#1557b0;--ink:#202124;--ink-2:#5f6368;
  --line:#dadce0;--bg:#f8f9fa;--card:#fff;--code-bg:#f1f3f4;--accent:#e8f0fe;
  --ok:#188038;--warn:#e37400;--star:#f9ab00;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;font-family:"Google Sans",Roboto,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  color:var(--ink);background:var(--bg);line-height:1.7;font-size:15px}
.app-bar{position:sticky;top:0;z-index:20;background:var(--card);border-bottom:1px solid var(--line);
  padding:14px 28px;display:flex;align-items:center;gap:14px;box-shadow:0 1px 2px rgba(60,64,67,.1)}
.app-bar .dot{width:10px;height:10px;border-radius:50%;background:var(--primary)}
.app-bar b{font-size:16px;font-weight:500}
.app-bar .crumb{color:var(--ink-2);font-size:13px}
.app-bar a{color:var(--primary);text-decoration:none;font-size:13px}
.wrap{max-width:920px;margin:0 auto;padding:32px 28px 80px}
.meta{display:flex;flex-wrap:wrap;gap:8px 20px;color:var(--ink-2);font-size:13px;
  border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:28px}
.meta .pill{background:var(--accent);color:var(--primary-dark);padding:2px 10px;border-radius:12px;font-size:12px}
.meta .pill.stale{background:#fce8e6;color:#c5221f}
h1.doc-title{font-size:28px;font-weight:500;margin:0 0 6px;line-height:1.3}
.doc h1,.doc h2,.doc h3,.doc h4{font-weight:500;line-height:1.35;margin:1.6em 0 .6em;scroll-margin-top:70px}
.doc h1{font-size:24px;border-bottom:2px solid var(--accent);padding-bottom:.3em}
.doc h2{font-size:20px}.doc h3{font-size:17px}.doc h4{font-size:15px;color:var(--ink-2)}
.doc p{margin:.7em 0}
.doc ul,.doc ol{margin:.6em 0;padding-left:1.6em}
.doc li{margin:.3em 0}
.doc a{color:var(--primary);text-decoration:none}.doc a:hover{text-decoration:underline}
.doc img{max-width:100%;height:auto;border:1px solid var(--line);border-radius:8px;margin:14px 0;display:block;cursor:zoom-in}
.doc td img,.doc th img{margin:6px 0}
.doc mark.hetu-ref{background:var(--accent);color:var(--primary-dark);padding:1px 8px;border-radius:6px;
  font-size:.92em;white-space:nowrap;border:1px dashed var(--primary)}
.doc .mention{background:var(--accent);color:var(--primary-dark);padding:0 4px;border-radius:4px;font-size:.95em}
#lbx{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.85);display:none;
  align-items:center;justify-content:center;cursor:zoom-out;padding:24px}
#lbx.open{display:flex}
#lbx img{max-width:96vw;max-height:96vh;border-radius:8px;box-shadow:0 8px 40px rgba(0,0,0,.5);border:0;cursor:zoom-out}
#lbx .x{position:fixed;top:14px;right:22px;color:#fff;font-size:32px;line-height:1;opacity:.85;cursor:pointer}
.doc table{border-collapse:collapse;width:100%;margin:18px 0;font-size:14px;display:block;overflow-x:auto}
.doc th,.doc td{border:1px solid var(--line);padding:8px 12px;text-align:left;vertical-align:top}
.doc th{background:var(--accent);font-weight:500;color:var(--primary-dark)}
.doc tr:nth-child(even) td{background:#fafbfc}
.doc blockquote{border-left:4px solid var(--primary);background:var(--accent);margin:14px 0;
  padding:10px 16px;color:var(--ink-2);border-radius:0 8px 8px 0}
.doc pre{background:#1e1e1e;color:#d4d4d4;border-radius:10px;padding:16px 18px;overflow-x:auto;
  font:13px/1.6 "JetBrains Mono",Consolas,Menlo,monospace;margin:16px 0}
.doc :not(pre)>code{background:var(--code-bg);padding:2px 6px;border-radius:4px;
  font:13px "JetBrains Mono",Consolas,monospace;color:#c5221f}
.doc hr{border:0;border-top:1px solid var(--line);margin:28px 0}
.codewrap{margin:16px 0}
.codewrap .hd{background:#2d2d2d;color:#9cdcfe;font:12px "JetBrains Mono",monospace;
  padding:8px 16px;border-radius:10px 10px 0 0;display:flex;justify-content:space-between}
.codewrap .hd a{color:#9cdcfe}
.codewrap pre{margin:0;border-radius:0 0 10px 10px}
.bin-embed{margin:18px 0}
.bin-embed video{width:100%;border-radius:10px;border:1px solid var(--line)}
.bin-embed object{width:100%;height:78vh;border:1px solid var(--line);border-radius:10px}
.dl-btn{display:inline-flex;align-items:center;gap:8px;background:var(--primary);color:#fff;
  text-decoration:none;padding:9px 18px;border-radius:8px;font-size:14px;margin:6px 0}
.foot{max-width:920px;margin:40px auto 0;padding:20px 28px;color:var(--ink-2);font-size:12px;
  border-top:1px solid var(--line);text-align:center}
"""

# Self-contained image lightbox (plain string → braces stay literal, NOT an
# f-string). Click any .doc img to open full-size; click overlay / Esc to close.
LIGHTBOX = """<div id="lbx"><span class="x">&times;</span><img alt=""></div>
<script>
(function(){var b=document.getElementById('lbx'),bi=b.querySelector('img');
document.querySelectorAll('.doc img').forEach(function(im){im.addEventListener('click',function(){
  bi.src=this.currentSrc||this.src;b.classList.add('open');document.body.style.overflow='hidden';});});
function close(){b.classList.remove('open');bi.src='';document.body.style.overflow='';}
b.addEventListener('click',close);
document.addEventListener('keydown',function(e){if(e.key==='Escape')close();});
})();
</script>"""

def page_shell(title, body, crumb="", extra_css=""):
    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 蚂蚁 TPU 资料库</title>
<style>{CSS}{extra_css}</style></head><body>
<div class="app-bar"><span class="dot"></span><b>蚂蚁 TPU 资料库</b>
<span class="crumb">{crumb}</span>
<span style="flex:1"></span><a href="{URL_PREFIX}/index.html">← 总纲</a></div>
{body}
<div class="foot">蚂蚁 TPU 资料库镜像 · 源自钉钉空间 {SPACE_ID} · jarvis 自动同步<br>
生成于 {now_hkt()} · 内容保真镜像，以钉钉原文为准</div>
{LIGHTBOX}
</body></html>"""

def esc(s):
    return (str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            .replace('"',"&quot;"))
