#!/usr/bin/env python3
"""render_files.py — render downloaded md/code file nodes to CC Pages HTML.

adoc nodes are handled by process_batch.py; this covers the file-type nodes that
download_files.py pulled (md → markdown render, py/json/yaml/sh → code render).

Inputs:
  STAGE/files-raw/<slug>.<ext>     raw downloaded files
  /tmp/ant-files-jobs.json         enum file nodes (name/uuid/path/updated/updator)
Output:
  STAGE/docs/<slug>.html
  STAGE/meta/<slug>.json
"""
import sys, json, subprocess, pathlib, argparse
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import common as C

STAGE = C.STAGE
S = pathlib.Path(__file__).parent
LANG = {"py": "python", "json": "json", "yaml": "yaml", "yml": "yaml",
        "sh": "bash", "md": "markdown"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", default="/tmp/ant-files-jobs.json")
    args = ap.parse_args()
    jobs = json.load(open(args.jobs))
    by_uuid = {j["uuid"]: j for j in jobs}
    rawdir = STAGE / "files-raw"
    for d in ("meta", "docs"):
        (STAGE / d).mkdir(parents=True, exist_ok=True)

    done, err = [], []
    for j in jobs:
        ext = j.get("ext", "")
        if ext == "adoc":
            continue
        if ext not in ("md", "py", "json", "yaml", "yml", "sh"):
            continue  # binaries handled elsewhere
        slug = C.slugify(j["name"], j["uuid"])
        raw = rawdir / f"{slug}.{ext}"
        if not raw.exists():
            err.append((slug, "no raw file")); continue
        meta = {
            "name": j["name"], "path": j.get("path", ""),
            "updator": j.get("updator", ""),
            "dingtalk_updated": j.get("updated"),
            "dingtalk_content_updated": j.get("updated"),  # files have no contentUpdated
            "local_synced_at": C.now_hkt(), "status": "synced",
        }
        (STAGE / "meta" / f"{slug}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2))
        out = STAGE / "docs" / f"{slug}.html"
        mode = "md" if ext == "md" else "code"
        cmd = [sys.executable, str(S / "render_doc.py"), "--mode", mode,
               "--in", str(raw), "--meta", str(STAGE / "meta" / f"{slug}.json"),
               "--out", str(out)]
        if mode == "code":
            cmd += ["--lang", LANG.get(ext, ext)]
        rc = subprocess.run(cmd, capture_output=True, text=True)
        if rc.returncode == 0:
            done.append(slug)
            print(f"  OK  {slug} ({ext})")
        else:
            err.append((slug, (rc.stderr or rc.stdout).strip()[-120:]))
            print(f"  ERR {slug}: {err[-1][1]}")
    print(f"rendered {len(done)} file-docs, {len(err)} errors")
    for s, e in err:
        print(f"    ! {s}: {e}")

if __name__ == "__main__":
    main()
