#!/usr/bin/env python3
"""cdp_extract.py — extract DingTalk adoc bodies + images via raw CDP.

Runs ON gLinux (where the logged-in Chrome lives, debug port 9222). Bypasses
chrome-devtools-mcp (whose SSE session is flaky for long batch jobs). Auto-finds
the open docs.dingtalk.com tab, then per doc:
  1. injects a same-origin iframe loading alidocs preview, waits for body,
     scrolls through to trigger lazy images, returns innerHTML + img srcs.
  2. fetches each real /resources/img/ image via fetch(credentials:include) in
     the page context → base64 (only Chrome holds the auth cookie).

Emits the two files process_batch.py consumes:
  /tmp/ant-extract-batch{N}.json  {results:[{slug,html,imgCount,imgs,err}]}
  /tmp/ant-imgs-batch{N}.json     {results:[{slug,images:[{idx,b64,mime,url}]}]}

Usage:  python3 cdp_extract.py --batch N --jobs /tmp/ant-batch-N.json
  jobs file: [{slug,docKey,dentryKey}]
"""
import sys, json, time, re, argparse, urllib.request
import websocket  # websocket-client 1.x

WORKSPACE = "15eGe6boVpjOez3N"
CDP_HTTP = "http://127.0.0.1:9222/json"

EXTRACT_JS = r"""
(async () => {
  const DOCKEY="%DOCKEY%", DENTRYKEY="%DENTRYKEY%", WS="%WS%";
  const previewUrl = `https://alidocs.dingtalk.com/note/preview?biz_ver=10&docId=${DOCKEY}`
    + `&docType=doc&dontjump=true&utm_scene=team_space&platform=pc&mainsiteOrigin=mainsite`
    + `&from=dingnote&workspaceId=${WS}&docKey=${DOCKEY}&dentryKey=${DENTRYKEY}#preview=true`;
  document.getElementById('__extr')?.remove();
  const f=document.createElement('iframe');
  f.id='__extr'; f.style.cssText='position:fixed;left:-9999px;top:0;width:1280px;height:2600px;';
  f.src=previewUrl; document.body.appendChild(f);
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  let root=null, err='';
  for(let i=0;i<60;i++){
    await sleep(500);
    try{
      const doc=f.contentDocument;
      if(!doc){err='cross-origin';continue;}
      root=doc.querySelector('.body-editor-content');
      if(root && (root.innerText||'').length>150){await sleep(700);break;}
    }catch(e){err='EX:'+String(e).slice(0,80);}
  }
  if(!root){f.remove();return{err:err||'no body-editor-content'};}
  const doc=f.contentDocument, win=f.contentWindow;
  // scroll-through to trigger loading="lazy" images
  const H=root.scrollHeight||doc.body.scrollHeight||2600;
  for(let y=0;y<H+1200;y+=600){win.scrollTo(0,y);await sleep(140);}
  win.scrollTo(0,0); await sleep(1200);
  const imgs=[...root.querySelectorAll('img')].map(i=>i.currentSrc||i.src)
    .filter(s=>s && s.includes('/resources/img/'));
  const res={html:root.innerHTML, imgCount:imgs.length, imgs};
  f.remove();
  return res;
})()
"""

FETCH_JS = r"""
(async () => {
  const URL="%URL%";
  try{
    const r=await fetch(URL,{credentials:'include'});
    if(!r.ok) return {err:'http '+r.status};
    const b=await r.blob();
    const b64=await new Promise((res,rej)=>{const fr=new FileReader();
      fr.onloadend=()=>res(fr.result);fr.onerror=rej;fr.readAsDataURL(b);});
    return {mime:b.type, size:b.size, dataurl:b64};
  }catch(e){return {err:String(e).slice(0,120)};}
})()
"""

def ws_url():
    d = json.load(urllib.request.urlopen(CDP_HTTP, timeout=5))
    for t in d:
        if t.get("type") == "page" and "docs.dingtalk.com" in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    raise SystemExit("no docs.dingtalk.com tab open in Chrome")

class CDP:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, max_size=200*1024*1024, suppress_origin=True)
        self.id = 0
    def call(self, method, params=None, timeout=90):
        self.id += 1
        mid = self.id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        self.ws.settimeout(timeout)
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                return msg
    def eval(self, expr, timeout=90):
        r = self.call("Runtime.evaluate", {
            "expression": expr, "awaitPromise": True, "returnByValue": True,
            "userGesture": True}, timeout=timeout)
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return {"err": "JS-EXC:" + json.dumps(res["exceptionDetails"])[:160]}
        return res.get("result", {}).get("value")

IMG_BASE = "https://alidocs.dingtalk.com"
OSS = "?x-oss-process=image/format,webp/ignore-error,1"

def imgs_from_html(html):
    # Capture EVERY image, including lazy ones whose URL lives in data-src (only
    # the 1-2 above-the-fold images get a real <img src>; the rest stay data-src
    # until scrolled into view). Regex on the resource hash catches both, then we
    # build one canonical webp URL per unique hash.
    seen, out = set(), []
    for h in re.findall(r"/resources/img/([0-9a-f]+)", html):
        if h in seen: continue
        seen.add(h)
        out.append(f"{IMG_BASE}/core/api/resources/img/{h}{OSS}")
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, required=True)
    ap.add_argument("--jobs")
    args = ap.parse_args()
    n = args.batch
    jobs = json.load(open(args.jobs or f"/tmp/ant-batch-{n}.json"))
    cdp = CDP(ws_url())
    cdp.call("Runtime.enable")

    extract_results, imgs_results = [], []
    for j in jobs:
        slug, dk, dek = j["slug"], j["docKey"], j["dentryKey"]
        js = EXTRACT_JS.replace("%DOCKEY%", dk).replace("%DENTRYKEY%", dek).replace("%WS%", WORKSPACE)
        r = cdp.eval(js, timeout=120)
        if not isinstance(r, dict) or r.get("err"):
            extract_results.append({"slug": slug, "err": (r or {}).get("err", "null") if isinstance(r, dict) else str(r)})
            imgs_results.append({"slug": slug, "images": []})
            print(f"  ERR {slug}: {extract_results[-1]['err']}"); continue
        uniq = imgs_from_html(r["html"])
        extract_results.append({"slug": slug, "title": "", "html": r["html"],
                                "imgCount": len(uniq), "imgs": uniq, "err": None})
        # fetch images
        images = []
        for idx, u in enumerate(uniq):
            fr = cdp.eval(FETCH_JS.replace("%URL%", u), timeout=60)
            if isinstance(fr, dict) and fr.get("dataurl"):
                b64 = fr["dataurl"].split(",", 1)[1]
                images.append({"idx": idx, "mime": fr.get("mime", ""),
                               "size": fr.get("size", 0), "b64": b64, "url": u})
            else:
                print(f"     img{idx} fail: {(fr or {}).get('err') if isinstance(fr,dict) else fr}")
        imgs_results.append({"slug": slug, "images": images})
        print(f"  OK  {slug}  html={len(r['html'])}  imgs={len(images)}/{len(uniq)}")

    json.dump({"n": len(extract_results), "results": extract_results},
              open(f"/tmp/ant-extract-batch{n}.json", "w"), ensure_ascii=False)
    json.dump({"n": len(imgs_results), "results": imgs_results},
              open(f"/tmp/ant-imgs-batch{n}.json", "w"), ensure_ascii=False)
    okc = sum(1 for r in extract_results if not r.get("err"))
    print(f"batch {n}: extracted {okc}/{len(jobs)} → /tmp/ant-extract-batch{n}.json + ant-imgs-batch{n}.json")

if __name__ == "__main__":
    main()
