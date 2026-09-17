#!/usr/bin/env bash
# 全栈状态一把过 —— 六个仓库 + 在跑的服务。
# 只读，不改任何东西。用法：scripts/stack-check.sh [--gateway URL]
#
# 设计原则：**查不到就说查不到，不猜**。每一项都标明判据来源，
# 免得「没输出」被当成「没问题」。
set -u

GATEWAY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --gateway) GATEWAY="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

G="\033[32m"; Y="\033[33m"; R="\033[31m"; D="\033[90m"; N="\033[0m"
ok(){ printf "  ${G}✓${N} %s\n" "$*"; }
warn(){ printf "  ${Y}!${N} %s\n" "$*"; }
bad(){ printf "  ${R}✗${N} %s\n" "$*"; }
dim(){ printf "     ${D}%s${N}\n" "$*"; }

echo "━━━ 仓库 ━━━"
check_repo(){ # check_repo <本地路径> <期望 origin 关键字> <标签>
  local p="$1" want="$2" label="$3"
  if [ ! -d "$p/.git" ]; then warn "$label — 本机没有克隆（$p）"; return; fi
  local origin ahead dirty branch
  origin=$(git -C "$p" remote get-url origin 2>/dev/null || echo "")
  branch=$(git -C "$p" rev-parse --abbrev-ref HEAD 2>/dev/null)
  dirty=$(git -C "$p" status --porcelain 2>/dev/null | wc -l)
  if [ -z "$origin" ]; then
    bad "$label — ${R}没有 origin，只存在于本机${N}"; return
  fi
  case "$origin" in *"$want"*) ;; *) warn "$label — origin 不是预期的：$origin"; return ;; esac
  ahead=$(git -C "$p" log --oneline "@{u}..HEAD" 2>/dev/null | wc -l)
  local msg="$label @ $branch"
  [ "$ahead" -gt 0 ] && msg="$msg，${Y}$ahead 个提交没推${N}"
  [ "$dirty" -gt 0 ] && msg="$msg，$dirty 个文件有改动"
  if [ "$ahead" -gt 0 ]; then warn "$msg"; else ok "$msg"; fi
}
check_repo "$HOME/CloseCrab"            CloseCrab             "CloseCrab"
check_repo "$HOME/lk-gemini-agent"      livekit-gemini-agent  "livekit-gemini-agent"
check_repo "$HOME/agent-starter-swift"  agent-starter-swift   "agent-starter-swift（iOS）"
check_repo "$HOME/gpu-tpu-pedia"        gpu-tpu-pedia         "gpu-tpu-pedia"
check_repo "$HOME/liveavatar-gateway"   liveavatar-gateway    "liveavatar-gateway"
check_repo "$HOME/LiveAvatar"           LiveAvatar            "LiveAvatar（fork）"

echo
echo "━━━ 在跑的服务 ━━━"
n=$(pgrep -fc "run.sh" 2>/dev/null || echo 0)
if [ "$n" -gt 0 ]; then ok "CloseCrab bot wrapper：$n 个"; else warn "没看到 run.sh 进程（可能这台不跑 bot）"; fi

if systemctl is-active --quiet lk-gemini-agent 2>/dev/null; then
  ok "lk-gemini-agent：running"
elif systemctl list-unit-files 2>/dev/null | grep -q lk-gemini-agent; then
  bad "lk-gemini-agent：${R}unit 存在但没在跑${N}"
else
  warn "lk-gemini-agent：本机没有这个 unit"
fi

echo
echo "━━━ 数字人控制面 ━━━"
if [ -z "$GATEWAY" ]; then
  dim "未指定 --gateway，跳过（不代表它没问题）"
else
  body=$(curl -s --max-time 5 "$GATEWAY/healthz" 2>/dev/null)
  if [ -z "$body" ]; then
    bad "控制面无响应：$GATEWAY"
  else
    total=$(echo "$body" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("slots_total","?"))' 2>/dev/null)
    used=$(echo "$body" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("slots_used","?"))' 2>/dev/null)
    if [ "$total" = "0" ]; then
      bad "控制面活着，但${R}没有任何 GPU worker 注册${N}（slots_total=0）"
    elif [ "$used" = "$total" ]; then
      warn "控制面：槽位已满 $used/$total —— 新会话会收到 429"
      dim "长期占满不掉 → 查日志里「回收超时会话」「worker 心跳丢失」"
    else
      ok "控制面：槽位 $used/$total 使用中"
    fi
  fi
fi

echo
echo "━━━ 测试怎么跑（各家不一样）━━━"
dim "CloseCrab            scripts/closecrab-smoke-test.sh <bot> --json --actions"
dim "livekit-gemini-agent ./run-tests.sh   ⚠️ 不要用 pytest，会「0 items 然后通过」"
dim "liveavatar-gateway   pytest tests/ -q  且  python smoke.py"
dim "详见 docs/stack-overview.md"
