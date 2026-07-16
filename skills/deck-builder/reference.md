# Deck-builder reference

Copy-paste recipes for the parts that aren't in `deck_helpers.py`.

---

## 1. Render-verify loop (do this constantly)
```bash
python my_gen.py                       # writes deck.pptx
bash scripts/render.sh deck.pptx 3 5   # render pages 3-5 -> /tmp/render_out/p-*.png
# -> Read the PNGs, find overflow/misalignment, edit my_gen.py, repeat
```
Then upload and render the *uploaded* copy once (Google's conversion can differ):
```bash
python scripts/gslides.py patch-slides deck.pptx $FILEID
python scripts/gslides.py export /tmp/up.pdf $FILEID
bash scripts/render.sh /tmp/up.pdf
```

---

## 2. Link-stable Drive iteration
First time → create, **save the id**:
```bash
python scripts/gslides.py up-slides deck.pptx $FOLDER "My Deck"   # prints URL + ID
echo "<FILEID>" > .deck_id
```
Every later change → patch the same id (URL never changes):
```bash
python scripts/gslides.py patch-slides deck.pptx "$(cat .deck_id)"
```

---

## 3. Accurate charts with matplotlib (square, CJK-safe)
Draw data charts in matplotlib so numbers are exact; save **square + transparent**
so `pic()` crop-to-fill won't clip outer labels. Make chart totals == table totals.
```python
import matplotlib, math; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
F  = fm.FontProperties(fname="/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
FB = fm.FontProperties(fname="/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")
labels=["AWS","GCP","Azure","其他"]; vals=[15.5,8.6,7.1,2.8]
colors=["#FBBC04","#4285F4","#EA4335","#9AA0A6"]; tot=sum(vals)
fig,ax=plt.subplots(figsize=(5.4,5.4),dpi=200)
w,_=ax.pie(vals,colors=colors,startangle=90,counterclock=False,
           wedgeprops=dict(width=0.42,edgecolor="white",linewidth=3))
for wd,v in zip(w,vals):
    a=(wd.theta1+wd.theta2)/2
    ax.text(.79*math.cos(math.radians(a)),.79*math.sin(math.radians(a)),
            f"{v/tot*100:.0f}%",ha="center",va="center",color="white",fontproperties=FB,fontsize=15)
for wd,l,v in zip(w,labels,vals):
    a=(wd.theta1+wd.theta2)/2; x,y=math.cos(math.radians(a)),math.sin(math.radians(a))
    ax.annotate(f"{l}\n~${v}M",(x,y),(x*1.2,y*1.3),ha="left" if x>=0 else "right",
                va="center",fontproperties=FB,fontsize=12,
                arrowprops=dict(arrowstyle="-",color="#BDC1C6"))
ax.text(0,0,f"~${tot:.0f}M",ha="center",va="center",fontproperties=FB,fontsize=22)
ax.set_xlim(-1.55,1.55); ax.set_ylim(-1.55,1.55); ax.axis("off")
plt.subplots_adjust(left=.01,right=.99,top=.99,bottom=.01)
plt.savefig("chart.png",transparent=True)
```

---

## 4. Generate imagery with Nano Banana 2 (Vertex)
Model `gemini-3.1-flash-image` (global). Prompt for clean, **no text, no real
logos**, brand-neutral. Great for hero strips & concept diagrams. Don't generate
fake photos of real, identifiable people — source those instead.
```python
import os,json,base64,subprocess,time,urllib.request
PROJECT="cloud-llm-preview1"   # same project as Drive calls; change only if you lack access
TOKEN=subprocess.check_output(["gcloud","auth","print-access-token"]).decode().strip()
def gen(name,prompt,ar="16:9"):
    url=f"https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/global/publishers/google/models/gemini-3.1-flash-image:generateContent"
    body={"contents":[{"role":"user","parts":[{"text":prompt}]}],
          "generationConfig":{"responseModalities":["IMAGE"],"imageConfig":{"aspectRatio":ar}}}
    req=urllib.request.Request(url,data=json.dumps(body).encode(),
        headers={"Authorization":f"Bearer {TOKEN}","Content-Type":"application/json"})
    resp=json.loads(urllib.request.urlopen(req,timeout=180).read().decode())
    for p in resp["candidates"][0]["content"]["parts"]:
        if "inlineData" in p:
            open(f"{name}.png","wb").write(base64.b64decode(p["inlineData"]["data"])); return name+".png"
gen("hero_concept","Clean minimal isometric illustration of ..., soft blue/green, white space, NO text, NO logos")
```

---

## 5. Get user-PASTED images as files (they're in the session JSONL)
Screenshots a user drops into chat are **not** on disk — they're base64 in the
current session transcript. Extract them, then match by the `WxH` shown on the
attachment chip. (Don't claim you can't access a pasted image.)
```python
import json,base64,io,os; from PIL import Image
JL="<session>.jsonl"   # newest *.jsonl in ~/.claude/projects/<proj>/ (ls -t)
os.makedirs("/tmp/pasted",exist_ok=True); seen={}; i=0
def walk(o,out):
    if isinstance(o,dict):
        if o.get("type")=="image" and isinstance(o.get("source"),dict) and o["source"].get("data"):
            out.append(o["source"]["data"])
        for v in o.values(): walk(v,out)
    elif isinstance(o,list):
        for v in o: walk(v,out)
for line in open(JL,encoding="utf-8",errors="ignore"):
    if '"image"' not in line: continue
    try: obj=json.loads(line)
    except: continue
    res=[]; walk(obj,res)
    for b in res:
        try:
            raw=base64.b64decode(b); im=Image.open(io.BytesIO(raw)); k=(im.size,len(raw))
            if k in seen: continue
            seen[k]=1; i+=1; im.convert("RGB").save(f"/tmp/pasted/p{i:02d}_{im.size[0]}x{im.size[1]}.png")
        except: pass
print("extracted to /tmp/pasted")  # pick by the WxH on the attachment chip
```

---

## 6. Pretty Google Docs via HTML import
Build rich HTML, upload as a Doc (`up-doc` / `patch-doc`). Google Docs import
honors: colored `<h1/h2/h3>`, bold, `bgcolor` on `<td>`, single-cell tables as
**callout boxes**, and **base64 `data:` `<img>`** (verified). It mostly ignores
`line-height`/`border`/`font-family`, so lean on these primitives:
- Section head: `<h2 style="color:#1a73e8">…</h2><hr style="border:none;border-top:2px solid #4285F4">`
- Data table: header `<tr bgcolor="#202124"><td style="color:#fff">…`, zebra rows
  `<tr bgcolor="#F8F9FA">`, cells `style="border:1px solid #DADCE0;padding:7px"`.
- Callout: `<table><tr><td bgcolor="#E8F0FE" style="padding:10px;border-left:5px solid #4285F4">…</td></tr></table>`
- Image: embed as data URI, downscale first so the file isn't huge:
```python
import base64,io; from PIL import Image
def datauri(p,maxw=900,q=82):
    im=Image.open(p).convert("RGB")
    if im.width>maxw: im=im.resize((maxw,int(im.height*maxw/im.width)))
    b=io.BytesIO(); im.save(b,"JPEG",quality=q)
    return "data:image/jpeg;base64,"+base64.b64encode(b.getvalue()).decode()
# <img src="{datauri('photo.png')}" width="600">
```
Verify by exporting the Doc to PDF and reading it (Google's import ≠ your CSS).

---

## 7. Gotchas
- **`pic()` crops to fill** — give it a box with the right aspect, or it trims
  edges. For charts/screenshots that must show fully, match the box aspect or
  pre-pad the image to that ratio.
- **CJK in matplotlib** needs an explicit font (`Noto Sans CJK`), else tofu.
- **Drive needs `drive`+`presentations` scope** and the `X-Goog-User-Project`
  header on every call, or you get quota/permission errors.
- **Don't double-upload**: one PATCH per iteration; keep the id in a dotfile.
- **Brand/content rules differ** internal vs customer-facing (sources, pricing,
  roadmap). Ask which audience; don't over-commit on the customer's behalf.
- **Verify before "done"**: a slide/doc is only correct once you've rendered it
  and looked. Report honestly if something didn't fit.
