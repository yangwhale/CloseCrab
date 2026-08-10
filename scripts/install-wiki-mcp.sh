#!/bin/bash
# install-wiki-mcp.sh — 在一台机器上把 Wiki MCP Server 装起来（幂等，可反复跑）
#
# 装完之后 agent 就有 9 个 wiki tool（wiki_query / wiki_ask / wiki_page /
# wiki_related / wiki_search / wiki_list / wiki_graph_neighbors /
# wiki_graph_path / wiki_status），不用再 bash 调 query.py 然后解析 stdout。
#
# 为什么这事值得一个脚本：2026-08-10 给另一台机器装的时候踩了三个坑，
# 每个都够卡住半小时，而且报错都在指向错误的方向。见下面 KNOWN TRAPS。
#
# 用法：
#   ./install-wiki-mcp.sh                      # 自动探测 WIKI_REPO
#   WIKI_REPO=~/my-wiki-study ./install-wiki-mcp.sh
#   ./install-wiki-mcp.sh --from ~/my-wiki-v2/scripts/wiki-mcp-server.py
#   ./install-wiki-mcp.sh --check              # 只体检，不改任何东西
#
# ── KNOWN TRAPS（都实际踩过）─────────────────────────────────────
#
# 1. PyPI 上有**两个**都叫 `mcp` 的项目。
#    我们要的是 modelcontextprotocol 官方 SDK（有 mcp.server.fastmcp），
#    另一个 `mcp==2.x` 的 server/ 下没有 fastmcp。装错了报的是
#    `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` ——
#    这个报错看起来像「没装」，实际是「装了另一个同名项目」。
#    所以下面**钉死版本**。
#
# 2. Ubuntu 24.04+ 的 PEP 668 externally-managed，pip install 直接被拒。
#    必须 --user --break-system-packages。
#
# 3. 装到了错的解释器上等于没装。bot 用哪个 python，就得装到哪个的
#    user-site 里。下面从**正在跑的 bot 进程**反查解释器，不猜。
# ──────────────────────────────────────────────────────────────

set -uo pipefail

MCP_PIN="mcp==1.27.0"          # 见 TRAP 1；升级前先确认 mcp.server.fastmcp 还在
CHECK_ONLY=0
SRC=""

while [ $# -gt 0 ]; do
    case "$1" in
        --from)  SRC="$2"; shift 2 ;;
        --check) CHECK_ONLY=1; shift ;;
        -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

ok()   { echo "  ✅ $*"; }
warn() { echo "  ⚠️  $*"; }
die()  { echo "  ❌ $*" >&2; exit 1; }

# ── 1. 定位 Wiki 仓库 ─────────────────────────────────────────────
if [ -z "${WIKI_REPO:-}" ]; then
    for c in "$HOME/my-wiki-v2" "$HOME/my-wiki" "$HOME/my-wiki-study"; do
        # 判据是「有 content/」而不是「目录存在」—— 空目录也叫存在
        [ -d "$c/content" ] && WIKI_REPO="$c" && break
    done
fi
[ -n "${WIKI_REPO:-}" ] || die "找不到 Wiki 仓库。设 WIKI_REPO=/path/to/wiki 再跑。"
[ -d "$WIKI_REPO/content" ] || die "$WIKI_REPO 下没有 content/，这不是个 Wiki 仓库。"
ok "WIKI_REPO = $WIKI_REPO ($(find "$WIKI_REPO/content" -name '*.md' | wc -l) 页)"

# ── 2. 找出 bot 实际用的 python（见 TRAP 3）──────────────────────
BOT_PID=$(pgrep -f "closecrab --bot" 2>/dev/null | head -1)
if [ -n "$BOT_PID" ] && [ -r "/proc/$BOT_PID/exe" ]; then
    PY=$(readlink -f "/proc/$BOT_PID/exe")
    ok "解释器（取自正在跑的 bot）: $PY"
else
    PY=$(command -v python3) || die "找不到 python3"
    warn "没有正在跑的 bot，退回系统 python3: $PY"
    warn "如果 bot 用的是别的解释器，装了也不生效 —— 起个 bot 再跑一次更稳"
fi

# ── 3. 校验 wiki_utils / query 提供了 MCP server 需要的符号 ───────
# MCP server 只依赖这 7 个符号 + query.query()。缺哪个先补哪个，
# 不要整份 query.py 照搬 —— 各家 Wiki 的 query.py 本来就不一样。
echo "── 依赖符号检查 ──"
MISSING=$("$PY" - "$WIKI_REPO" <<'PYEOF'
import sys, pathlib
repo = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(repo / "scripts"))
need_utils = ["WIKI_CONTENT", "WIKI_URL", "all_pages", "parse_frontmatter",
              "extract_wikilinks", "find_page_by_slug", "page_url"]
missing = []
try:
    import wiki_utils as W
    missing += [n for n in need_utils if not hasattr(W, n)]
except Exception as e:
    missing.append(f"wiki_utils 无法导入: {e}")
try:
    import query as Q
    if not hasattr(Q, "query"):
        missing.append("query.query()")
except Exception as e:
    missing.append(f"query 无法导入: {e}")
print("|".join(missing))
PYEOF
)
if [ -n "$MISSING" ]; then
    die "缺少符号: ${MISSING//|/, }"
fi
ok "wiki_utils 7 个符号 + query.query() 齐全"

# ── 4. 放置 MCP server 脚本 ──────────────────────────────────────
DST="$WIKI_REPO/scripts/wiki-mcp-server.py"
# --from 先校验，不管后面走哪个分支。首版把校验放在 elif 里，于是目标文件
# 已存在时 `--from /不存在的路径` 一声不吭就过了 —— 用户以为用了自己指定的
# 源，实际用的是旧文件。参数错了就该立刻说，不能等到"恰好用不上"才沉默。
if [ -n "$SRC" ] && [ ! -f "$SRC" ]; then
    die "--from 指定的 $SRC 不存在"
fi

if [ -n "$SRC" ]; then
    # 显式指定来源 = 明确要求覆盖，优先级高于"已有"
    if [ "$CHECK_ONLY" = 1 ]; then
        ok "--check 模式：将从 $SRC 复制（未执行）"
    else
        cp "$SRC" "$DST" && ok "从 $SRC 复制到 $DST"
    fi
elif [ -f "$DST" ]; then
    ok "已有 $DST"
else
    # 不在 CloseCrab 里放一份副本 —— 那会重演「同一份代码两个仓库各自演进」。
    # MCP server 归 Wiki 仓库管，从一个已经有它的 Wiki 仓库拷过来。
    die "$DST 不存在。用 --from <一个已有该文件的 wiki 仓库>/scripts/wiki-mcp-server.py 指定来源"
fi

# ── 5. 装 mcp SDK（见 TRAP 1 / 2）────────────────────────────────
echo "── Python 依赖 ──"
if "$PY" -c "from mcp.server.fastmcp import FastMCP" 2>/dev/null; then
    ok "mcp.server.fastmcp 已可用 ($("$PY" -c 'import importlib.metadata as m;print(m.version("mcp"))' 2>/dev/null || echo '?'))"
else
    if [ "$CHECK_ONLY" = 1 ]; then
        warn "mcp.server.fastmcp 不可用（--check 模式，不安装）"
    else
        echo "     装 $MCP_PIN ..."
        "$PY" -m pip install -q --user --break-system-packages "$MCP_PIN" 2>&1 | grep -viE "warning|hint|note" | head -3
        "$PY" -c "from mcp.server.fastmcp import FastMCP" 2>/dev/null \
            && ok "$MCP_PIN 装好" \
            || die "装完仍然 import 不到 fastmcp。检查是不是装到了别的解释器，或撞上 TRAP 1"
    fi
fi

# ── 6. 冒烟：能不能加载出 9 个 tool ──────────────────────────────
echo "── 加载测试 ──"
TOOLS=$(cd "$WIKI_REPO/scripts" && "$PY" - <<'PYEOF' 2>/dev/null
import sys, importlib.util
sys.path.insert(0, ".")
try:
    s = importlib.util.spec_from_file_location("wm", "wiki-mcp-server.py")
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    print(" ".join(sorted(n for n in dir(m) if n.startswith("wiki_"))))
except Exception as e:
    print(f"ERR {type(e).__name__}: {e}")
PYEOF
)
case "$TOOLS" in
    ERR*) die "加载失败: $TOOLS" ;;
    "")   die "加载没报错但一个 tool 都没有，检查 wiki-mcp-server.py 是否完整" ;;
    *)    ok "$(echo "$TOOLS" | wc -w) 个 tool: $TOOLS" ;;
esac

[ "$CHECK_ONLY" = 1 ] && { echo "  （--check 模式，未修改 ~/.claude.json）"; exit 0; }

# ── 7. 注册到 ~/.claude.json ─────────────────────────────────────
echo "── 注册 MCP ──"
"$PY" - "$DST" <<'PYEOF'
import json, sys, pathlib, shutil
dst = sys.argv[1]
p = pathlib.Path.home() / ".claude.json"
if not p.exists():
    p.write_text("{}")
shutil.copy(p, str(p) + ".bak-wiki-mcp")
d = json.loads(p.read_text())
ms = d.setdefault("mcpServers", {})
before = ms.get("wiki")
ms["wiki"] = {"type": "stdio", "command": "python3", "args": [dst]}
if before == ms["wiki"]:
    print("  ✅ 已注册且指向正确，无需改动")
else:
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    print(f"  ✅ 已注册 wiki → {dst}")
    if before:
        print(f"     （原来指向 {before.get('args', ['?'])[0]}）")
PYEOF

echo
echo "  装好了。**需要重启 bot 才生效** —— MCP 配置是 CLI 启动时读的："
echo "      pkill -f 'closecrab --bot <name>'    # run.sh 会自动拉起"
echo "  重启后验证：让 agent 跑一次 wiki_query(\"随便什么词\")"
