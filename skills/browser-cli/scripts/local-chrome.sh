#!/usr/bin/env bash
# local-chrome.sh — 在 cc-tw 自己的桌面上拉起一个挂了 CDP 的 Chrome，给 `ab --use local` 用。
#
# 为什么需要它：远端那两台机器上的 Chrome 是**登录态**的，专供需要 SSO 的站点。
# 我们自己写的 HTML、GitHub Pages、公开网站根本不需要登录，走远端等于白付一次
# ssh 往返（实测每轮 60s 起步）。本机直连是亚秒级，而且窗口真的显示在
# Chrome Remote Desktop 那块桌面上 —— Chris 能看见我在看什么。
#
# 幂等：已经通了就直接返回，不会起第二个。
set -euo pipefail

PORT="${AB_LOCAL_PORT:-9222}"
PROFILE="$HOME/.cache/ab-local-chrome"

alive() { curl -s --max-time 3 "http://127.0.0.1:$PORT/json/version" | grep -q Browser; }

if alive; then
  echo "✓ 已经在跑: $(curl -s "http://127.0.0.1:$PORT/json/version" | python3 -c 'import sys,json;print(json.load(sys.stdin)["Browser"])')"
  exit 0
fi

# 桌面在哪块 X display 上 —— Chrome Remote Desktop 每次会话的编号可能变，
# 所以从 Xorg 进程现查，不要写死 :20。查不到就退回 headless。
DISP=$(ps -eo args | grep -oP '(?<=Xorg )(:[0-9]+)' | head -1 || true)
if [ -z "$DISP" ]; then
  echo "⚠ 没找到 X display，用 headless 起（截图仍可用，但你在桌面上看不见）" >&2
  HEADLESS="--headless=new"
else
  echo "桌面 display: $DISP"
  HEADLESS=""
fi

# TMPDIR 必须拨回 /tmp。bot 进程里的 TMPDIR 是层层嵌套的
# /tmp/claude-1005/claude-1005/... ，Chrome 的 SingletonSocket 建在它下面会
# 超过 unix socket 路径 108 字节上限，直接 FATAL 退出，报的却是
# "Socket path too long" 这种一眼看不出跟 TMPDIR 有关的话。
env -u TMPDIR TMPDIR=/tmp DISPLAY="${DISP:-}" setsid google-chrome \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --no-first-run --no-default-browser-check \
  --window-size=1440,900 \
  $HEADLESS about:blank \
  >/tmp/ab-local-chrome.log 2>&1 < /dev/null &

for _ in $(seq 1 15); do
  sleep 1
  if alive; then
    echo "✓ 起来了: $(curl -s "http://127.0.0.1:$PORT/json/version" | python3 -c 'import sys,json;print(json.load(sys.stdin)["Browser"])')"
    exit 0
  fi
done

echo "✗ 15 秒内没起来，看 /tmp/ab-local-chrome.log" >&2
tail -5 /tmp/ab-local-chrome.log >&2
exit 1
