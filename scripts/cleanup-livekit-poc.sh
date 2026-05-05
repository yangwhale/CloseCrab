#!/bin/bash
# Copyright 2025-2026 Chris Yang (yangwhale)
# Licensed under the Apache License, Version 2.0
#
# cleanup-livekit-poc.sh — 清理 Phase 1 PoC 阶段的独立 livekit-agent 残留
#
# 背景: Phase 1 PoC 时, LiveKit agent (LLM + STT + TTS) 跑在独立 service
#       livekit-agent.service 里, 代码在 ~/livekit-agent/.
#       Phase 2 把 LLM 部分并入 tianmaojingling 进程内 (closecrab/voice/livekit_io.py),
#       PoC service 已不再需要.
#
# 这个脚本: 停 + disable + remove livekit-agent.service. ~/livekit-agent/ 源码可选保留.

set -euo pipefail

KEEP_SOURCE=true
for arg in "$@"; do
    case "$arg" in
        --remove-source) KEEP_SOURCE=false ;;
        --help|-h)
            cat <<'HELP'
用法: ./scripts/cleanup-livekit-poc.sh [--remove-source]

清理 Phase 1 PoC 残留 (livekit-agent.service).

  --remove-source   连 ~/livekit-agent/ 源码也一起删 (默认保留, 仅停 service)
HELP
            exit 0
            ;;
    esac
done

if systemctl list-unit-files livekit-agent.service &>/dev/null; then
    echo "[cleanup] 停止 livekit-agent.service..."
    sudo systemctl stop livekit-agent.service 2>/dev/null || true
    sudo systemctl disable livekit-agent.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/livekit-agent.service
    sudo systemctl daemon-reload
    echo "[cleanup] livekit-agent.service 已清理"
else
    echo "[cleanup] livekit-agent.service 不存在, 跳过"
fi

if [[ "$KEEP_SOURCE" == "false" ]] && [[ -d "$HOME/livekit-agent" ]]; then
    echo "[cleanup] 删除 ~/livekit-agent/ 源码..."
    rm -rf "$HOME/livekit-agent"
    echo "[cleanup] 完成"
elif [[ -d "$HOME/livekit-agent" ]]; then
    echo "[cleanup] ~/livekit-agent/ 保留 (用 --remove-source 删除)"
fi

echo "[cleanup] 全部完成"
