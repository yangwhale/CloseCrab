#!/usr/bin/env python3
"""Diff a fresh enumerate dump against manifest.json → sync worklist.

Compares each live node's DingTalk edit-time to what the manifest recorded:
  new       : live node absent from manifest
  stale     : docs whose dingtalk_content_updated (or files' dingtalk_updated)
              is newer than the manifest record
  deleted   : manifest node no longer live
  unchanged : everything else

Usage:
  diff_manifest.py --enum enum.json --manifest manifest.json [--out worklist.json]
"""
import sys, json, argparse


def edit_ms(node):
    # docs use contentUpdated; files (md/code/binary) use updated
    return node.get("contentUpdated") or node.get("updated") or 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enum", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()

    enum = json.load(open(args.enum, encoding="utf-8"))
    live = {n["uuid"]: n for n in enum["nodes"] if n["type"] == "file"}
    try:
        man = json.load(open(args.manifest, encoding="utf-8")).get("nodes", {})
    except FileNotFoundError:
        man = {}

    new, stale, unchanged, deleted = [], [], [], []
    for uuid, n in live.items():
        rec = man.get(uuid)
        if not rec:
            new.append(uuid); continue
        live_ms = edit_ms(n)
        rec_ms = rec.get("dingtalk_content_updated") or rec.get("dingtalk_updated") or 0
        if live_ms > rec_ms:
            stale.append(uuid)
        else:
            unchanged.append(uuid)
    for uuid in man:
        if uuid not in live:
            deleted.append(uuid)

    def label(uuid):
        n = live.get(uuid) or {"name": man.get(uuid, {}).get("name", uuid)}
        return f"  [{n.get('ext','?')}] {n['name']}"

    print(f"=== sync diff ===  live files: {len(live)}  manifest: {len(man)}")
    print(f"NEW ({len(new)}):");        [print(label(u)) for u in new]
    print(f"STALE ({len(stale)}):");    [print(label(u)) for u in stale]
    print(f"DELETED ({len(deleted)}):");[print(label(u)) for u in deleted]
    print(f"UNCHANGED: {len(unchanged)}")

    worklist = {"new": new, "stale": stale, "deleted": deleted,
                "unchanged": unchanged,
                "to_sync": new + stale,
                "live": {u: live[u] for u in live}}
    if args.out:
        json.dump(worklist, open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"\nworklist → {args.out}  ({len(worklist['to_sync'])} to sync)")


if __name__ == "__main__":
    main()
