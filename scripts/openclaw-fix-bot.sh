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

    # 2a. Symlinks at the two non-indexed scope paths (for grep/read).
    for parent in \
        "${HOME}/.openclaw/workspace" \
        "${HOME}/.openclaw/workspace-${bot}"
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

    # 2b. Hardlinks at the per-bot workspace (OpenClaw memory indexer
    #     does NOT follow symlinks, so we need real files here).
    if [[ -L "${ws}/memory" ]]; then
        rm -f "${ws}/memory"
    fi
    mkdir -p "${ws}/memory"
    python3 - "$ws" "$MEMORY_TARGET" << 'PYEOF'
import os, sys, pathlib, shutil
ws = pathlib.Path(sys.argv[1])
shared = pathlib.Path(sys.argv[2])

def relink(src, dst):
    if dst.exists():
        if dst.is_symlink():
            dst.unlink()
        elif dst.stat().st_ino == src.stat().st_ino:
            return False  # already linked
        else:
            dst.unlink()
    os.link(src, dst)
    return True

if (shared / 'MEMORY.md').is_file():
    if relink(shared / 'MEMORY.md', ws / 'MEMORY.md'):
        print(f'  + hardlinked {ws}/MEMORY.md')
    else:
        print(f'  \u2713 hardlink ok: {ws}/MEMORY.md')

count_new = count_ok = 0
for src in shared.glob('*.md'):
    if not src.is_file():
        continue
    if relink(src, ws / 'memory' / src.name):
        count_new += 1
    else:
        count_ok += 1
print(f'  hardlinks in {ws}/memory/: +{count_new} new, {count_ok} already ok')

# Team shared infra docs (GCS-mounted, different FS, can't hardlink).
shared_src = shared / 'shared'
if shared_src.is_dir():
    shared_dst = ws / 'memory' / 'shared'
    if shared_dst.is_symlink():
        shared_dst.unlink()
    shared_dst.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0
    for src in shared_src.glob('*.md'):
        if not src.is_file():
            continue
        dst = shared_dst / src.name
        try:
            s = src.stat()
            if dst.exists():
                d = dst.stat()
                if d.st_size == s.st_size and d.st_mtime >= s.st_mtime:
                    skipped += 1
                    continue
            shutil.copyfile(src, dst)
            copied += 1
        except Exception as e:
            print(f'  WARN shared copy {src.name}: {e}')
    print(f'  shared/ infra docs: +{copied} copied, {skipped} unchanged')
PYEOF

    # 3. Verify + reindex.
    python3 - "$bot" "$ws" "$OPENCLAW_CFG" << 'VERIFYEOF'
import json, pathlib, os, sys
bot, ws_str, cfg_path = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = json.load(open(cfg_path))
entry = next((a for a in cfg['agents']['list'] if a.get('id') == bot), None)
print('  -- verify --')
print('  agents.list entry:', json.dumps(entry, ensure_ascii=False) if entry else 'MISSING')
home = os.environ['HOME']
for p in [f'{home}/.openclaw/workspace/memory', f'{home}/.openclaw/workspace-{bot}/memory']:
    pp = pathlib.Path(p)
    if pp.is_symlink():
        print(f'  symlink {p} -> {pp.resolve()}')
    elif pp.exists():
        print(f'  WARNING_NOT_SYMLINK {p}')
    else:
        print(f'  MISSING {p}')
ws_mem = pathlib.Path(f'{ws_str}/MEMORY.md')
if ws_mem.is_file():
    st = ws_mem.stat()
    print(f'  hardlink {ws_mem} inode={st.st_ino} nlinks={st.st_nlink}')
ws_dir = pathlib.Path(f'{ws_str}/memory')
if ws_dir.is_dir() and not ws_dir.is_symlink():
    n = len(list(ws_dir.glob('*.md')))
    print(f'  hardlink dir {ws_dir} ({n} *.md files)')
VERIFYEOF
    # Trigger reindex so memory_search works immediately.
    if command -v openclaw &>/dev/null; then
        openclaw memory index --agent "$bot" --force 2>&1 | tail -1
    fi
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
