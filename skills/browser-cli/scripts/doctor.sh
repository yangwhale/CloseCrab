#!/usr/bin/env bash
# doctor.sh — 检查 browser-cli 依赖是否就绪，不满足时打印修复命令
#
# 这些依赖装在**远端 cloudtop**上，不在仓库里，所以每台新机器都要跑一次。
set -uo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HOME/.config/ab-host"
HOST="${AB_HOST:-$( [ -f "$CONF" ] && cat "$CONF" || echo glinux_bj )}"
FAIL=0

echo "目标机: $HOST"

# 把命令挂到 PATH 上（~/.local/bin 是标准用户 bin，已在 PATH 里）
mkdir -p "$HOME/.local/bin"
for c in ab abref chat-read absend chat-poll; do
  ln -sfn "$SELF_DIR/$c" "$HOME/.local/bin/$c"
done
echo "[0/3] 命令已链到 ~/.local/bin: ab abref chat-read absend chat-poll"

echo -n "[1/3] ssh 可达 ....... "
if ssh -o ConnectTimeout=8 -o BatchMode=yes -o LogLevel=ERROR "$HOST" true 2>/dev/null; then
  echo "OK"
else
  echo "FAIL"
  echo "     修: 检查 ~/.ssh/config 里的 $HOST 别名与反向隧道是否还活着"
  exit 1
fi

echo -n "[2/3] agent-browser .. "
V=$(ssh -o LogLevel=ERROR "$HOST" 'export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh" >/dev/null 2>&1; \
    nvm use 24 >/dev/null 2>&1; agent-browser --version 2>/dev/null' 2>/dev/null | tail -1)
if [ -n "$V" ]; then
  echo "OK ($V)"
else
  echo "FAIL"
  echo "     修: ssh $HOST 后执行"
  echo '         export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm install 24; nvm use 24'
  echo '         npm i -g agent-browser'
  FAIL=1
fi

echo -n "[3/3] Chrome CDP 9222  "
if ssh -o LogLevel=ERROR "$HOST" 'curl -s --max-time 5 http://127.0.0.1:9222/json/version' 2>/dev/null | grep -q Browser; then
  echo "OK"
else
  echo "FAIL"
  echo "     修: cloudtop 上那个**已登录公司账号**的 Chrome 必须带调试端口启动:"
  echo "         google-chrome --remote-debugging-port=9222 &"
  echo "     注意别新开一个没登录的 profile，否则内部系统全要重新 SSO。"
  FAIL=1
fi

[ "$FAIL" = 0 ] && echo "全部就绪，可直接用 ab / absend / chat-poll" || echo "有依赖缺失，见上面修复命令"
exit $FAIL
