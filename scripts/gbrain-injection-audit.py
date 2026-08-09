#!/usr/bin/env python3
import json, sys, hashlib
from collections import defaultdict
TOK = 3  # 中英混合粗估 char/token

def scan(path):
    names, seen = {}, defaultdict(lambda: {"n":0,"sz":0,"slug":"","q":[]})
    calls = total = 0
    qs = []
    for line in open(path, errors='replace'):
        if 'gbrain' not in line: continue
        try: rec = json.loads(line)
        except Exception: continue
        msg = rec.get('message') or {}
        content = msg.get('content')
        if not isinstance(content, list): continue
        for blk in content:
            if not isinstance(blk, dict): continue
            t = blk.get('type')
            if t == 'tool_use' and str(blk.get('name','')).startswith('mcp__gbrain__'):
                names[blk.get('id')] = blk['name']
                calls += 1
                qs.append((blk['name'].split('__')[-1], str((blk.get('input') or {}).get('query',''))[:28]))
            elif t == 'tool_result' and blk.get('tool_use_id') in names:
                c = blk.get('content')
                txt = c if isinstance(c,str) else (''.join(x.get('text','') for x in c if isinstance(x,dict)) if isinstance(c,list) else '')
                if not txt: continue
                total += len(txt)
                try:
                    arr = json.loads(txt); items = arr if isinstance(arr,list) else [arr]
                except Exception:
                    items = [{'chunk_text': txt, 'slug':'(raw)'}]
                for it in items:
                    if not isinstance(it, dict): continue
                    body = it.get('chunk_text') or it.get('compiled_truth') or json.dumps(it,ensure_ascii=False)
                    fp = hashlib.sha1(body.encode()).hexdigest()[:12]
                    e = seen[fp]; e["n"] += 1; e["sz"] = len(body); e["slug"] = it.get('slug','?')
    return calls, total, seen, qs

for p in sys.argv[1:]:
    calls, total, seen, qs = scan(p)
    if not calls:
        print(f"\n=== {p.split('/')[-1][:12]} === 无 gbrain 调用"); continue
    uniq = sum(v["sz"] for v in seen.values())
    dup  = sum(v["sz"]*(v["n"]-1) for v in seen.values())
    rep  = {k:v for k,v in seen.items() if v["n"]>1}
    print(f"\n=== {p.split('/')[-1][:12]} ===")
    print(f"  gbrain 调用      : {calls} 次")
    print(f"  返回内容总量     : {total:,} 字符 ≈ {total//TOK:,} tok")
    print(f"  去重后唯一内容   : {uniq:,} 字符 ≈ {uniq//TOK:,} tok")
    pct = dup/total*100 if total else 0
    print(f"  ** 重复浪费      : {dup:,} 字符 ≈ {dup//TOK:,} tok  ({pct:.1f}%) **")
    print(f"  片段: {len(seen)} 个唯一 / 其中 {len(rep)} 个被重复返回")
    for fp,v in sorted(rep.items(), key=lambda kv:-kv[1]["sz"]*(kv[1]["n"]-1))[:5]:
        print(f"    · {v['slug'][:44]:44} ×{v['n']}  {v['sz']:,}字符  浪费 {v['sz']*(v['n']-1):,}")
    print(f"  查询序列: {', '.join(f'{a}({b})' for a,b in qs[:8])}")
