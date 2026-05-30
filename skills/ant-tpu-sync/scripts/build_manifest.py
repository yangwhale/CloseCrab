#!/usr/bin/env python3
"""build_manifest.py — assemble manifest.json from a fresh enumerate + local renders.

Source of truth for the tree = the enumerate JSON (name/type/ext/path/depth/
updated/contentUpdated/updator/size/docKey/dentryKey/uuid). For each node we
resolve its local artifact:
  - doc  (adoc/md/py/json/yaml/sh) → docs/<slug>.html   → local_html
  - bin  (pdf/mp4/webm/zip/adraw/axls) → files/<slug>.<ext> → local_asset
and stamp content_sha256 + local_synced_at + status (synced|error|folder).

Usage:
  build_manifest.py --enum /tmp/ant-tpu-enum-fresh.json \
                    --files-dir /tmp/ant-files-local \
                    --out manifest.json
"""
import json, argparse, hashlib, pathlib
import common as C

DOC_EXT = ("adoc", "md", "py", "json", "yaml", "yml", "sh")
BIN_EXT = ("pdf", "mp4", "webm", "zip", "adraw", "axls")


def sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enum", default="/tmp/ant-tpu-enum-fresh.json")
    ap.add_argument("--files-dir", default="/tmp/ant-files-local")
    ap.add_argument("--out", default=str(C.STAGE / "manifest.json"))
    args = ap.parse_args()

    enum = json.load(open(args.enum, encoding="utf-8"))
    nodes_in = enum["nodes"]
    docs_dir = C.STAGE / "docs"
    files_dir = pathlib.Path(args.files_dir)
    now = C.now_hkt()

    out_nodes = {}
    n_doc = n_bin = n_err = n_fld = 0
    for n in nodes_in:
        uuid = n["uuid"]
        ext = n.get("ext", "")
        slug = C.slugify(n["name"], uuid)
        rec = {
            "name": n["name"], "type": n["type"], "ext": ext,
            "path": n["path"], "depth": n.get("depth", 0),
            "dingtalk_updated": n.get("updated"),
            "dingtalk_content_updated": n.get("contentUpdated") or n.get("updated"),
            "updator": n.get("updator", ""), "size": n.get("size", 0),
            "docKey": n.get("docKey", ""), "dentryKey": n.get("dentryKey", ""),
            "dingtalk_url": n.get("url", ""),
        }
        if n["type"] == "folder":
            rec["status"] = "folder"
            n_fld += 1
        elif ext in DOC_EXT:
            html = docs_dir / f"{slug}.html"
            if html.exists():
                rec["local_html"] = f"docs/{slug}.html"
                rec["content_sha256"] = sha256(html)
                rec["local_synced_at"] = now
                rec["status"] = "synced"
                n_doc += 1
            else:
                rec["status"] = "error"
                rec["error"] = "no rendered html"
                n_err += 1
        elif ext in BIN_EXT:
            binf = files_dir / f"{slug}.{ext}"
            if binf.exists():
                rec["local_asset"] = f"files/{slug}.{ext}"
                rec["content_sha256"] = sha256(binf)
                rec["local_synced_at"] = now
                rec["status"] = "synced"
                n_bin += 1
            else:
                rec["status"] = "error"
                rec["error"] = "download failed (e.g. adraw 403)"
                n_err += 1
        else:
            rec["status"] = "error"
            rec["error"] = f"unknown ext {ext}"
            n_err += 1
        out_nodes[uuid] = rec

    man = {
        "space_id": C.SPACE_ID,
        "root_uuid": getattr(C, "ROOT_UUID", "pGBa2Lm8aGLllAvacMD4Q7v9VgN7R35y"),
        "last_full_sync": now,
        "nodes": out_nodes,
    }
    json.dump(man, open(args.out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"manifest → {args.out}")
    print(f"  {len(out_nodes)} nodes: {n_doc} docs + {n_bin} bins + "
          f"{n_fld} folders + {n_err} errors")


if __name__ == "__main__":
    main()
