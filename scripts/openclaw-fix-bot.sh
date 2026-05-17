#!/usr/bin/env bash
# openclaw-fix-bot.sh — Self-heal OpenClaw worker bot setup.
#
# What it does (idempotent, safe to re-run):
#   1. Ensures ~/.openclaw/openclaw.json agents.list[<bot>] has correct
#      `workspace` field pointing at ~/.closecrab/openclaw-workspace/<bot>/
#   2. Ensures the per-bot workspace dir exists.
#   3. Creates `memory/` symlinks in all three paths OpenClaw might resolve:
#        ~/.openclaw/workspace/memory          (legacy global)
#        ~/.openclaw/workspace-<bot>/memory    (per-agent default)
#        ~/.closecrab/openclaw-workspace/<bot>/memory (ACP cwd)
#   4. Verifies the result.
#
# Usage:
#   scripts/openclaw-fix-bot.sh <bot_name>
#   scripts/openclaw-fix-bot.sh --all     # all bots currently in agents.list
#
# Triggered automatically on bot start via closecrab/workers/openclaw_acp.py
# (_ensure_openclaw_agent_config + _ensure_memory_symlinks). This script is
# the manual / bulk equivalent for one-shot fixes without restarting bots.

set -euo pipefail

OPENCLAW_CFG="${HOME}/.openclaw/openclaw.json"
MEMORY_TARGET="${HOME}/.claude/projects/-home-chrisya/memory"

if [[ ! -f "$OPENCLAW_CFG" ]]; then
    echo "ERROR: OpenClaw config not found at $OPENCLAW_CFG" >&2
    exit 1
fi
if [[ ! -d "$MEMORY_TARGET" ]]; then
    echo "WARN: memory target $MEMORY_TARGET does not exist; symlinks will still be created"
fi

fix_one() {
    local bot="$1"
    echo "=== fixing $bot ==="
    local ws="${HOME}/.closecrab/openclaw-workspace/${bot}"
    mkdir -p "$ws"

    # 1. Upsert agents.list[<bot>].workspace via Python (atomic rename).
    python3 - "$bot" "$ws" "$OPENCLAW_CFG" << 'PYEOF'
import json, sys, pathlib
bot, ws, cfg_path = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
agents = cfg.setdefault("agents", {})
agent_list = agents.setdefault("list", [])
entry = next((a for a in agent_list if isinstance(a, dict) and a.get("id") == bot), None)
changed = False
if entry is None:
    agent_list.append({"id": bot, "name": bot, "workspace": ws})
    changed = True
    print(f"  + inserted agents.list[{bot}] (workspace={ws})")
elif entry.get("workspace") != ws:
    entry["workspace"] = ws
    changed = True
    print(f"  + set agents.list[{bot}].workspace = {ws}")
else:
    print(f"  ✓ agents.list[{bot}].workspace already correct")
if changed:
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp-fix")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cfg_path)
    print(f"  + wrote {cfg_path}")
PYEOF

    # 2. Memory symlinks (3 paths).
    for parent in \
        "${HOME}/.openclaw/workspace" \
        "${HOME}/.openclaw/workspace-${bot}" \
        "${ws}"
    do
        mkdir -p "$parent"
        local link="${parent}/memory"
        if [[ -L "$link" ]] && [[ "$(readlink -f "$link")" == "$(readlink -f "$MEMORY_TARGET" 2>/dev/null || echo "$MEMORY_TARGET")" ]]; then
            echo "  ✓ symlink ok: $link"
        else
            rm -f "$link"
            ln -s "$MEMORY_TARGET" "$link"
            echo "  + symlinked $link → $MEMORY_TARGET"
        fi
    done

    # 3. Verify.
    python3 -c "
import json, pathlib
cfg = json.load(open('$OPENCLAW_CFG'))
entry = next((a for a in cfg['agents']['list'] if a.get('id') == '$bot'), None)
print('  -- verify --')
print('  agents.list entry:', json.dumps(entry, ensure_ascii=False) if entry else 'MISSING')
for p in ['${HOME}/.openclaw/workspace/memory', '${HOME}/.openclaw/workspace-${bot}/memory', '${ws}/memory']:
    pp = pathlib.Path(p)
    if pp.is_symlink():
        print(f'  symlink {p} → {pp.resolve()}')
    elif pp.exists():
        print(f'  WARNING {p} exists but is not a symlink')
    else:
        print(f'  MISSING {p}')
"
}

case "${1:-}" in
    --all)
        bots=$(python3 -c "
import json
c = json.load(open('$OPENCLAW_CFG'))
for a in c.get('agents', {}).get('list', []):
    if isinstance(a, dict) and 'id' in a:
        print(a['id'])
")
        if [[ -z "$bots" ]]; then
            echo "No bots found in agents.list" >&2
            exit 1
        fi
        for b in $bots; do
            fix_one "$b"
        done
        ;;
    "")
        echo "Usage: $0 <bot_name> | --all" >&2
        exit 1
        ;;
    *)
        fix_one "$1"
        ;;
esac

echo ""
echo "Done. Note: memory sqlite index may need a gateway hot-reload or restart"
echo "to pick up new workspace scope. memory_search returns 0 hits until then."
