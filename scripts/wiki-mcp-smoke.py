#!/usr/bin/env python3
"""wiki-mcp-smoke.py — 真调用每个 wiki MCP tool，验证接口对得上。

由 install-wiki-mcp.sh 调用，也可单独跑：
    cd $WIKI_REPO/scripts && python3 ~/CloseCrab/scripts/wiki-mcp-smoke.py

为什么不能只数 tool 个数：2026-08-10 往一台机器拷了 wiki-mcp-server.py，
9 个 tool「全在」，一调用 4 个直接炸 —— 那份 server 是照另一个 Wiki 的
query.py 写的，返回类型（dict vs list）、结果项的键（有没有 "path"）、
以及 import 的辅助符号（_tokenize / get_index）全都不一样。

更阴的是 server 用 @_safe_tool 把异常包成 {"error": ...} 字符串**返回**，
不抛出。所以「调用没报错」也不算通过，必须看返回内容。

输出（供 shell 解析）：
    RESULT <ok>/<total> term=... slug=...
    FAILED <tool>:<原因> ...        # 只在有失败时输出
    LOADFAIL <原因>                 # 模块都加载不了
"""
import importlib.util
import json
import pathlib
import sys


def _corpus_args():
    """从真实语料取查询词和 slug。

    用假词测不出接口不兼容 —— 空结果那条分支太短，碰不到
    r["path"] / for r in results 这些真正会炸的地方。
    """
    term, slug, slug2 = "test", "", ""
    try:
        import wiki_utils as W
        pages = list(W.all_pages())[:2]
        if not pages:
            return term, slug, slug2

        def as_path(x):
            for attr in ("path", "file", "filepath"):
                v = getattr(x, attr, None)
                if v:
                    return pathlib.Path(str(v))
            if isinstance(x, dict):
                for k in ("path", "file"):
                    if x.get(k):
                        return pathlib.Path(str(x[k]))
            return pathlib.Path(str(x))

        p0 = as_path(pages[0])
        slug = p0.stem
        slug2 = as_path(pages[1]).stem if len(pages) > 1 else slug
        for line in p0.read_text(errors="ignore")[:600].splitlines():
            if line.startswith("title:"):
                cand = line.split(":", 1)[1].strip().strip('"').strip("'")
                if cand:
                    term = cand.split()[0]
                break
    except Exception:
        pass
    return term, slug, (slug2 or slug)


def main():
    sys.path.insert(0, ".")
    try:
        spec = importlib.util.spec_from_file_location("wm", "wiki-mcp-server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"LOADFAIL {type(e).__name__}: {e}")
        return 1

    tools = sorted(n for n in dir(mod)
                   if n.startswith("wiki_") and callable(getattr(mod, n)))
    if not tools:
        print("LOADFAIL 模块加载了，但一个 wiki_ tool 都没有")
        return 1

    term, slug, slug2 = _corpus_args()
    args = {
        "wiki_query": (term,), "wiki_ask": (term,), "wiki_search": (term,),
        "wiki_page": (slug,), "wiki_related": (slug,),
        "wiki_graph_neighbors": (slug,), "wiki_graph_path": (slug, slug2),
        "wiki_list": (), "wiki_status": (),
    }

    ok, bad = [], []
    for name in tools:
        fn = getattr(mod, name)
        try:
            out = fn(*args.get(name, ()))
        except TypeError as e:
            # 参数签名对不上也是接口不兼容，照样算失败
            bad.append(f"{name}:签名({e.__class__.__name__})")
            continue
        except Exception as e:
            bad.append(f"{name}:{type(e).__name__}")
            continue
        txt = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        # @_safe_tool 把异常包成 {"error": "xxx failed: ..."} 返回 —— 那是失败
        if '"error"' in txt and "failed" in txt:
            bad.append(f"{name}:内部错误")
            continue
        ok.append(name)

    print(f"RESULT {len(ok)}/{len(tools)} term={term!r} slug={slug!r}")
    if bad:
        print("FAILED " + " ".join(bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
