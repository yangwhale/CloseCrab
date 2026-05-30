#!/usr/bin/env python3
"""process_batch.py — turn one extracted adoc batch into rendered CC Pages HTML.

Inputs (defaults keyed off --batch N, all local /tmp + the gLinux-pulled files):
  /tmp/ant-batch-N.json        [{slug,docKey,dentryKey}]            (slug→docKey map)
  /tmp/ant-extract-batchN.json {results:[{slug,title,html,imgs}]}   (Chrome extract)
  /tmp/ant-imgs-batchN.json    {results:[{slug,images:[{idx,b64,mime,url}]}]} (Chrome fetch)
  /tmp/ant-tpu-enum-fresh.json  list of 91 enum nodes               (meta source)

For each slug it:
  1. decodes base64 images → STAGE/img/<slug>/N.webp
  2. builds STAGE/meta/<slug>.imgmap.json  {origUrl: ASSET_URL/img/<slug>/N.webp}
  3. writes STAGE/raw/<slug>.html  (extracted innerHTML)
  4. writes STAGE/meta/<slug>.json (name/path/updator/dingtalk timestamps from enum)
  5. shells render_doc.py → STAGE/docs/<slug>.html

Idempotent: re-running overwrites in place. Then upload_batch handles GCS.
"""
import sys, json, base64, argparse, subprocess, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import common as C

STAGE = C.STAGE
S = pathlib.Path(__file__).parent

def load(p):
    return json.loads(pathlib.Path(p).read_text())

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, required=True)
    ap.add_argument("--extract")
    ap.add_argument("--imgs")
    ap.add_argument("--batchmap")
    ap.add_argument("--enum", default="/tmp/ant-tpu-enum-fresh.json")
    args = ap.parse_args()
    n = args.batch
    extract = load(args.extract or f"/tmp/ant-extract-batch{n}.json")
    imgs    = load(args.imgs    or f"/tmp/ant-imgs-batch{n}.json")
    bmap    = load(args.batchmap or f"/tmp/ant-batch-{n}.json")
    enum    = load(args.enum)
    if isinstance(enum, dict):
        enum = enum.get("nodes", [])

    slug2dockey = {r["slug"]: r["docKey"] for r in bmap}
    dk2node = {nd["docKey"]: nd for nd in enum if nd.get("docKey")}
    imgs_by_slug = {r["slug"]: r.get("images", []) for r in imgs.get("results", [])}

    for d in ("img", "meta", "raw", "docs"):
        (STAGE / d).mkdir(parents=True, exist_ok=True)

    done = []
    for r in extract.get("results", []):
        slug = r["slug"]
        if r.get("err"):
            print(f"  SKIP {slug}: extract err {r['err']}"); continue
        dk = slug2dockey.get(slug)
        node = dk2node.get(dk, {})
        # 1+2. images
        imgmap = {}
        imgdir = STAGE / "img" / slug
        ims = imgs_by_slug.get(slug, [])
        if ims:
            imgdir.mkdir(parents=True, exist_ok=True)
        for im in ims:
            idx = im["idx"]
            b = base64.b64decode(im["b64"])
            (imgdir / f"{idx}.webp").write_bytes(b)
            imgmap[im["url"]] = f"{C.ASSET_URL}/img/{slug}/{idx}.webp"
        (STAGE / "meta" / f"{slug}.imgmap.json").write_text(
            json.dumps(imgmap, ensure_ascii=False, indent=2))
        # 3. raw html
        (STAGE / "raw" / f"{slug}.html").write_text(r.get("html", ""))
        # 4. meta
        meta = {
            "name": node.get("name", slug),
            "path": node.get("path", ""),
            "updator": node.get("updator", ""),
            "dingtalk_updated": node.get("updated"),
            "dingtalk_content_updated": node.get("contentUpdated"),
            "local_synced_at": C.now_hkt(),
            "status": "synced",
        }
        (STAGE / "meta" / f"{slug}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2))
        # 5. render
        out = STAGE / "docs" / f"{slug}.html"
        cmd = [sys.executable, str(S / "render_doc.py"), "--mode", "adoc",
               "--in", str(STAGE / "raw" / f"{slug}.html"),
               "--meta", str(STAGE / "meta" / f"{slug}.json"),
               "--imgmap", str(STAGE / "meta" / f"{slug}.imgmap.json"),
               "--out", str(out)]
        rc = subprocess.run(cmd, capture_output=True, text=True)
        ok = rc.returncode == 0
        print(f"  {'OK ' if ok else 'ERR'} {slug}  imgs={len(ims)}  "
              f"{(rc.stdout or rc.stderr).strip().splitlines()[-1] if (rc.stdout or rc.stderr) else ''}")
        if ok:
            done.append(slug)
    print(f"batch {n}: rendered {len(done)}/{len(extract.get('results',[]))} docs")

if __name__ == "__main__":
    main()
