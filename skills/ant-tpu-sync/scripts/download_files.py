#!/usr/bin/env python3
"""download_files.py — pull DingTalk file-type nodes (md/code/binary) to disk.

Runs ON gLinux. adoc nodes use docKey+iframe extraction (cdp_extract.py) instead
and are SKIPPED here. For every other file node:
  1. Chrome page context calls /box/api/v2/file/download?dentryUuid=<uuid>
     (only Chrome holds the auth cookie + XSRF) → returns a URL_PRE_SIGNATURE
     payload whose ossUrlPreSignatureInfo.preSignUrls[] are plain pre-signed OSS
     links on *.trans.dingtalk.com.
  2. curl each pre-signed URL with NO cookie (the signature is self-contained),
     concatenating multipart segments in array order → /tmp/ant-files/<name>.

Emits /tmp/ant-files-result.json {results:[{uuid,name,ext,bytes,parts,err}]}.

Usage:  python3 download_files.py [--jobs /tmp/ant-files-jobs.json] [--only-ext pdf,zip]
  jobs file: [{uuid,name,ext,path,size,updated,updator,dentryKey}]
"""
import sys, json, subprocess, argparse, pathlib, urllib.request, websocket
import re, unicodedata, hashlib

CDP = "http://127.0.0.1:9222/json"
OUT = pathlib.Path("/tmp/ant-files")

def slugify(name, uuid=""):
    base = name.rsplit(".", 1)[0]
    base = unicodedata.normalize("NFKC", base).strip()
    base = re.sub(r"[\s/\\]+", "-", base)
    base = re.sub(r"[^\w一-鿿\-]", "", base)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    suffix = (uuid or hashlib.md5(name.encode()).hexdigest())[:6]
    return f"{base}-{suffix}" if base else suffix

DL_JS = r"""
(async () => {
  const UUID="%UUID%";
  const xsrf=decodeURIComponent((document.cookie.match(/XSRF-TOKEN=([^;]+)/)||[])[1]||'');
  if(!xsrf) return {err:'NO_XSRF'};
  try{
    const r=await fetch('https://docs.dingtalk.com/box/api/v2/file/download?dentryUuid='+UUID,
      {headers:{'accept':'application/json','x-xsrf-token':xsrf}, credentials:'include'});
    if(!r.ok) return {err:'http '+r.status};
    const j=await r.json();
    const info=j.data&&j.data.ossUrlPreSignatureInfo;
    if(!info||!info.preSignUrls) return {err:'no preSignUrls; type='+(j.data&&j.data.downloadType)};
    return {urls:info.preSignUrls, partSize:info.partSize};
  }catch(e){return {err:String(e).slice(0,140)};}
})()
"""

def ws_url():
    d = json.load(urllib.request.urlopen(CDP, timeout=5))
    for t in d:
        if t.get("type") == "page" and "docs.dingtalk.com" in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    raise SystemExit("no docs.dingtalk.com tab open in Chrome")

class CDP_:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, max_size=200*1024*1024, suppress_origin=True)
        self.id = 0
    def eval(self, expr, timeout=90):
        self.id += 1; mid = self.id
        self.ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate", "params": {
            "expression": expr, "awaitPromise": True, "returnByValue": True}}))
        self.ws.settimeout(timeout)
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == mid:
                r = m.get("result", {})
                if "exceptionDetails" in r:
                    return {"err": "JS-EXC:" + json.dumps(r["exceptionDetails"])[:160]}
                return r.get("result", {}).get("value")

def curl(url, dest):
    rc = subprocess.run(["curl", "-sS", "-L", url, "-o", str(dest),
                         "-w", "%{http_code}"], capture_output=True, text=True)
    return rc.stdout.strip(), rc.stderr.strip()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", default="/tmp/ant-files-jobs.json")
    ap.add_argument("--only-ext", default="")  # comma list; empty = all non-adoc
    args = ap.parse_args()
    jobs = json.load(open(args.jobs))
    only = set(e.strip() for e in args.only_ext.split(",") if e.strip())
    OUT.mkdir(parents=True, exist_ok=True)
    cdp = CDP_(ws_url())

    results = []
    for j in jobs:
        ext = j.get("ext", "")
        if ext == "adoc":
            continue
        if only and ext not in only:
            continue
        name, uuid = j["name"], j["uuid"]
        slug = slugify(name, uuid)
        destname = f"{slug}.{ext}" if ext else slug
        r = cdp.eval(DL_JS.replace("%UUID%", uuid), timeout=90)
        if not isinstance(r, dict) or r.get("err"):
            err = (r or {}).get("err", "null") if isinstance(r, dict) else str(r)
            results.append({"uuid": uuid, "name": name, "slug": slug, "ext": ext, "err": err})
            print(f"  ERR {name}: {err}"); continue
        urls = r["urls"]
        dest = OUT / destname
        if len(urls) == 1:
            code, errtxt = curl(urls[0], dest)
            if code != "200":
                results.append({"uuid": uuid, "name": name, "ext": ext, "err": f"curl {code} {errtxt[:80]}"})
                print(f"  ERR {name}: curl {code}"); continue
        else:
            # multipart: download each part then concat in order
            parts = []
            ok = True
            for i, u in enumerate(urls):
                p = OUT / f"{destname}.part{i}"
                code, errtxt = curl(u, p)
                if code != "200":
                    ok = False; print(f"  ERR {name} part{i}: curl {code}"); break
                parts.append(p)
            if not ok:
                results.append({"uuid": uuid, "name": name, "ext": ext, "err": "multipart curl fail"}); continue
            with open(dest, "wb") as out:
                for p in parts:
                    out.write(p.read_bytes()); p.unlink()
        sz = dest.stat().st_size
        results.append({"uuid": uuid, "name": name, "slug": slug, "ext": ext,
                        "destname": destname, "bytes": sz, "parts": len(urls), "err": None})
        print(f"  OK  {name}  {sz} bytes  parts={len(urls)}")

    json.dump({"n": len(results), "results": results},
              open("/tmp/ant-files-result.json", "w"), ensure_ascii=False, indent=1)
    okc = sum(1 for r in results if not r.get("err"))
    tot = sum(r.get("bytes", 0) for r in results if not r.get("err"))
    print(f"downloaded {okc}/{len(results)} files, {round(tot/1e6,1)} MB → {OUT}")

if __name__ == "__main__":
    main()
