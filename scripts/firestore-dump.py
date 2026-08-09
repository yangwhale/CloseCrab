#!/usr/bin/env python3
"""把 Firestore 导成人类可读、git 友好的 JSON —— 凭据脱敏。

跟 `gcloud firestore export` 的分工：
  export   二进制、全保真、能直接 import 回去，但不可读不可 diff → 留在 GCS
  本脚本   JSON、可读可 diff、能进 git，但**不含凭据所以不能直接还原**

为什么脱敏而不是原样导出：bots/{name} 里存着 channel token / app_secret，
一旦进 git 就永久留在版本历史里，删都删不干净。凭据的权威源是 Firestore
本身（以及现在配好的托管备份），这份 JSON 的用途是「看得见变化」，不是「能还原」。

脱敏规则：字段名含 token/secret/key/password/cred/auth 的，值换成
  <redacted:sha256前12位:长度>
指纹保留，所以「值有没有变过」仍然看得出来 —— 排查「是不是 token 被改了」够用。

用法：
  python3 scripts/firestore-dump.py --out ~/my-private/firestore
  python3 scripts/firestore-dump.py --db closecrab --out /tmp/x --no-logs
"""
import argparse
import datetime
import hashlib
import json
import os
import sys

from google.cloud import firestore

SECRET_HINTS = ("token", "secret", "key", "password", "passwd", "cred", "auth")
# 白名单：名字里带 key 但不是凭据的，别误伤
NOT_SECRET = ("key_findings", "keywords", "api_key_name", "public_key_id")


def is_secret(field: str) -> bool:
    f = field.lower()
    if any(w in f for w in NOT_SECRET):
        return False
    return any(w in f for w in SECRET_HINTS)


def redact(value) -> str:
    s = str(value)
    h = hashlib.sha256(s.encode()).hexdigest()[:12]
    return f"<redacted:{h}:{len(s)}>"


def clean(obj, field_name: str = ""):
    """递归脱敏 + 把 Firestore 特有类型转成 JSON 可序列化的东西。"""
    if isinstance(obj, dict):
        return {k: clean(v, k) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [clean(v, field_name) for v in obj]
    if is_secret(field_name) and obj not in (None, "", 0, False):
        return redact(obj)
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return f"<bytes:{len(obj)}>"
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def dump_collection(coll, with_subs=True, log_limit=None):
    out = {}
    for doc in coll.list_documents():
        snap = doc.get()
        entry = {"_data": clean(snap.to_dict() or {})}
        if with_subs:
            for sub in doc.collections():
                # logs 可能上万条，按需截断并记录真实总数
                docs = list(sub.list_documents())
                total = len(docs)
                if log_limit is not None and total > log_limit:
                    docs = docs[-log_limit:]
                subout = {}
                for sd in docs:
                    subout[sd.id] = clean(sd.get().to_dict() or {})
                entry.setdefault("_sub", {})[sub.id] = {
                    "_total": total,
                    "_dumped": len(docs),
                    "_docs": subout,
                }
        out[doc.id] = entry
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.environ.get("FIRESTORE_PROJECT", "chris-pgp-host"))
    ap.add_argument("--db", action="append", default=None, help="可重复；默认 closecrab + closecrab-public")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--log-limit", type=int, default=200, help="每个 bot 保留最近 N 条 log（默认 200，0=全部）")
    ap.add_argument("--no-logs", action="store_true", help="完全不导子集合")
    args = ap.parse_args()

    dbs = args.db or ["closecrab", "closecrab-public"]
    limit = None if args.log_limit == 0 else args.log_limit
    os.makedirs(args.out, exist_ok=True)

    for dbname in dbs:
        client = firestore.Client(project=args.project, database=dbname)
        payload = {
            "_meta": {
                "project": args.project,
                "database": dbname,
                # 时间由调用方注入更好，但这里是一次性脚本，直接取本地时间并标注时区
                "dumped_at": datetime.datetime.now(
                    datetime.timezone(datetime.timedelta(hours=8))
                ).isoformat(),
                "redacted": "字段名含 token/secret/key/password/cred/auth 的值已换成 sha256 指纹",
                "note": "本文件不含凭据，不能用于还原。全保真备份见 GCS firestore-backups/ 与托管 backup schedule。",
                "log_limit": args.log_limit,
            }
        }
        for coll in client.collections():
            payload[coll.id] = dump_collection(
                coll, with_subs=not args.no_logs, log_limit=limit
            )
        path = os.path.join(args.out, f"{dbname}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=False)
        size = os.path.getsize(path)
        print(f"  {dbname:<18} {size/1024:>8.1f} KB  → {path}")


if __name__ == "__main__":
    sys.exit(main())
