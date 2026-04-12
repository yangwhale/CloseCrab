#!/bin/bash
# bwrap-restore.sh — 恢复真实 bwrap
# 用法: sudo bash ~/CloseCrab/scripts/bwrap-restore.sh

if [[ ! -f /usr/bin/bwrap.real ]]; then
    echo "ERROR: /usr/bin/bwrap.real 不存在，无需恢复"
    exit 1
fi

mv /usr/bin/bwrap.real /usr/bin/bwrap
echo "OK: bwrap 已恢复为原版"
