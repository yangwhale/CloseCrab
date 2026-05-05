#!/bin/bash
# Copyright 2025-2026 Chris Yang (yangwhale)
# Licensed under the Apache License, Version 2.0
#
# voice-healthcheck.sh — 检查本机 voice infra 是否健康
#
# 检查项:
#   1. /usr/local/bin/livekit-server 存在 + version
#   2. systemd: livekit-server.service / livekit-frontend.service / caddy.service 都 active
#   3. ~/livekit-server/config.yaml 存在 + 含 keys
#   4. ~/livekit-frontend/.env.local 存在 + 含 LIVEKIT_API_KEY
#   5. localhost:7880 能 TCP connect (livekit-server 监听)
#   6. localhost:3000 能 HTTP get (frontend 监听)
#   7. ~/.closecrab-voice-hmac-*.key 至少一个 (bot 启动过)
#   8. (可选) Firestore bots/{name}.livekit.enabled=true 的 bot 数量
#
# 用法: ./scripts/voice-healthcheck.sh

set -uo pipefail   # 不用 -e, 让所有检查都跑完

OK=0
FAIL=0

check() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "  [OK]   $label"
        OK=$((OK + 1))
    else
        echo "  [FAIL] $label"
        FAIL=$((FAIL + 1))
    fi
}

check_value() {
    local label="$1" actual="$2" expected_pattern="$3"
    if [[ "$actual" =~ $expected_pattern ]]; then
        echo "  [OK]   $label = $actual"
        OK=$((OK + 1))
    else
        echo "  [FAIL] $label = $actual (expected $expected_pattern)"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== voice infra healthcheck ==="
echo ""

echo "[binary]"
check "livekit-server binary 存在" test -x /usr/local/bin/livekit-server
if [[ -x /usr/local/bin/livekit-server ]]; then
    echo "         version: $(/usr/local/bin/livekit-server --version 2>&1 | head -1)"
fi
check "caddy binary 存在" command -v caddy
check "pnpm binary 存在" command -v pnpm
echo ""

echo "[systemd]"
for svc in livekit-server livekit-frontend caddy; do
    check_value "$svc.service" "$(systemctl is-active "$svc" 2>&1)" "^active$"
done
echo ""

echo "[config files]"
check "~/livekit-server/config.yaml 存在"  test -f "$HOME/livekit-server/config.yaml"
check "~/livekit-server/.api_key 存在"     test -f "$HOME/livekit-server/.api_key"
check "~/livekit-server/.api_secret 存在"  test -f "$HOME/livekit-server/.api_secret"
check "~/livekit-frontend/.env.local 存在" test -f "$HOME/livekit-frontend/.env.local"
check "~/livekit-frontend/.next 存在 (已 build)" test -d "$HOME/livekit-frontend/.next"
echo ""

echo "[network]"
check "localhost:7880 (livekit-server signaling)" timeout 2 bash -c "</dev/tcp/127.0.0.1/7880"
check "localhost:3000 (livekit-frontend)"          timeout 2 bash -c "</dev/tcp/127.0.0.1/3000"
echo ""

echo "[HMAC keys]"
hmac_count=$(ls "$HOME"/.closecrab-voice-hmac-*.key 2>/dev/null | wc -l)
if [[ "$hmac_count" -gt 0 ]]; then
    echo "  [OK]   $hmac_count 个 HMAC key 文件 (bot 启动过):"
    for f in "$HOME"/.closecrab-voice-hmac-*.key; do
        bot="${f##*/.closecrab-voice-hmac-}"
        bot="${bot%.key}"
        echo "         - $bot"
    done
    OK=$((OK + 1))
else
    echo "  [WARN] 没有 ~/.closecrab-voice-hmac-*.key — voice bot 还没启动过"
fi
echo ""

echo "[Firestore voice bots]"
if python3 -c "from google.cloud import firestore" 2>/dev/null; then
    python3 - <<'PYEOF'
import os, sys
try:
    from google.cloud import firestore
    db = firestore.Client(
        project=os.environ.get("FIRESTORE_PROJECT", "closecrab"),
        database=os.environ.get("FIRESTORE_DATABASE", "(default)"),
    )
    count = 0
    for bot in db.collection("bots").stream():
        cfg = bot.to_dict()
        lk = cfg.get("livekit") or {}
        if lk.get("enabled"):
            has_secret = "yes" if lk.get("hmac_secret") else "no (会在首次启动时生成)"
            print(f"  [OK]   {bot.id}: enabled, frontend_url={lk.get('frontend_url')}, hmac_secret={has_secret}")
            count += 1
    if count == 0:
        print("  [WARN] 没有 voice 启用的 bot. 跑 'python3 scripts/config-manage.py set-livekit <bot> --auto-detect --frontend-url ... --enable'")
except Exception as e:
    print(f"  [SKIP] 读 Firestore 失败: {e}")
PYEOF
else
    echo "  [SKIP] google-cloud-firestore 未装"
fi
echo ""

echo "=== 总结: $OK OK / $FAIL FAIL ==="
if [[ "$FAIL" -gt 0 ]]; then
    echo ""
    echo "FAIL 排查:"
    echo "  - service 没起: sudo journalctl -u <service> -n 50"
    echo "  - port 不通: sudo ss -tlnp | grep -E '7880|3000'"
    echo "  - 配置缺失: 重跑 ./scripts/install-livekit.sh --refresh-templates --frontend-domain ... --signaling-domain ... --admin-email ..."
    exit 1
fi
exit 0
