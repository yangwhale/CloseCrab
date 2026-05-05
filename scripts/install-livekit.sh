#!/bin/bash
# Copyright 2025-2026 Chris Yang (yangwhale)
# Licensed under the Apache License, Version 2.0
#
# install-livekit.sh — 安装 / 维护 CloseCrab voice IO 所需的 LiveKit infra
#
# 装四样东西:
#   1. livekit-server 二进制 (curl install, 固定到指定 release)
#   2. Caddy (apt install) + Caddyfile (从 infra/livekit/Caddyfile.tmpl 渲染)
#   3. livekit-frontend (clone yangwhale/agent-starter-react + pnpm install + pnpm build)
#   4. systemd unit 文件 (livekit-server.service / livekit-frontend.service)
#
# 同时生成:
#   - ~/livekit-server/config.yaml (含随机 API key/secret)
#   - ~/livekit-frontend/.env.local (引用同一对 key/secret)
#
# 不做的事:
#   - DNS A 记录 (用户手动配, 脚本只检查解析)
#   - GCP firewall (用户手动开 UDP 50000-60000 + TCP 80/443)
#   - Firestore bots/{name}.livekit 字段写入 (用 scripts/config-manage.py set-livekit)
#   - HMAC key 文件 (~/.closecrab-voice-hmac-{bot}.key) — bot 启动时自动生成
#
# 用法:
#   ./scripts/install-livekit.sh \
#       --frontend-domain live.example.com \
#       --signaling-domain livekit.example.com \
#       --admin-email admin@example.com
#
#   ./scripts/install-livekit.sh --refresh-templates  # 重新渲染模板, 不动 key/secret 也不重装二进制
#   ./scripts/install-livekit.sh --rotate-keys        # 生成新 API key/secret (要重启所有 bot)
#   ./scripts/install-livekit.sh --uninstall          # 反向卸载

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INFRA_DIR="$SCRIPT_DIR/infra/livekit"

# ----- 默认参数 -----
FRONTEND_DOMAIN=""
SIGNALING_DOMAIN=""
ADMIN_EMAIL=""
LIVEKIT_VERSION="${LIVEKIT_VERSION:-v1.11.0}"
FRONTEND_REPO="${FRONTEND_REPO:-https://github.com/yangwhale/agent-starter-react.git}"
FRONTEND_BRANCH="${FRONTEND_BRANCH:-main}"
DEFAULT_AGENT_NAME="${DEFAULT_AGENT_NAME:-closecrab-voice-default}"
ACTION="install"

usage() {
    cat <<EOF
用法: $0 [OPTIONS]

操作 (互斥, 默认 install):
  --refresh-templates  重新渲染模板到目标位置, 不动 key/secret 也不重装二进制
  --rotate-keys        生成新 API key/secret (frontend + server 都更新, 要重启所有 voice bot)
  --uninstall          停掉 service, 删模板, 删二进制 (保留 ~/livekit-server/.api_secret 备份)

参数 (install / refresh-templates 用):
  --frontend-domain DOMAIN   前端域名, 例如 live.higcp.com (必填)
  --signaling-domain DOMAIN  Signaling 域名, 例如 livekit.higcp.com (必填)
  --admin-email EMAIL        Let's Encrypt 邮箱 (必填)

环境变量覆盖:
  LIVEKIT_VERSION       livekit-server 版本, 默认 v1.11.0
  FRONTEND_REPO         frontend git URL, 默认 yangwhale/agent-starter-react
  FRONTEND_BRANCH       frontend 分支, 默认 main
  DEFAULT_AGENT_NAME    URL 没带 ?bot= 时的 fallback agent name

示例:
  $0 --frontend-domain live.higcp.com \\
     --signaling-domain livekit.higcp.com \\
     --admin-email chris@higcp.com
EOF
}

# ----- 解析参数 -----
while [[ $# -gt 0 ]]; do
    case "$1" in
        --frontend-domain)  FRONTEND_DOMAIN="$2"; shift 2 ;;
        --signaling-domain) SIGNALING_DOMAIN="$2"; shift 2 ;;
        --admin-email)      ADMIN_EMAIL="$2"; shift 2 ;;
        --refresh-templates) ACTION="refresh"; shift ;;
        --rotate-keys)      ACTION="rotate"; shift ;;
        --uninstall)        ACTION="uninstall"; shift ;;
        --help|-h)          usage; exit 0 ;;
        *) echo "未知参数: $1"; usage; exit 1 ;;
    esac
done

# ====================================================================
# 工具函数
# ====================================================================

log()  { echo "[install-livekit] $*"; }
die()  { echo "[install-livekit] ERROR: $*" >&2; exit 1; }

require_args() {
    [[ -z "$FRONTEND_DOMAIN"  ]] && die "缺 --frontend-domain"
    [[ -z "$SIGNALING_DOMAIN" ]] && die "缺 --signaling-domain"
    [[ -z "$ADMIN_EMAIL"      ]] && die "缺 --admin-email"
}

render_template() {
    # 用法: render_template <src.tmpl> <dst> <KEY1=VAL1> <KEY2=VAL2> ...
    # 把 __KEY__ 替换成 VAL, 输出到 dst. 用 python 一次完成所有替换避免 bash $() 吃掉末尾换行.
    local src="$1" dst="$2"; shift 2
    python3 - "$src" "$dst" "$@" <<'PYEOF'
import sys, pathlib
src, dst, *pairs = sys.argv[1:]
text = pathlib.Path(src).read_text()
for pair in pairs:
    k, _, v = pair.partition("=")
    text = text.replace(f"__{k}__", v)
pathlib.Path(dst).write_text(text)
PYEOF
}

dns_check() {
    local domain="$1"
    local resolved
    resolved="$(getent hosts "$domain" | awk '{print $1}' | head -1 || true)"
    if [[ -z "$resolved" ]]; then
        log "  WARN: $domain 解析失败, Caddy 申请 LE 证书会失败. 请先配 DNS A 记录指向本机."
    else
        log "  $domain → $resolved"
    fi
}

# ====================================================================
# 安装步骤
# ====================================================================

install_livekit_server() {
    log "[1/4] LiveKit Server ($LIVEKIT_VERSION)..."
    if [[ -x /usr/local/bin/livekit-server ]]; then
        local current
        current="$(/usr/local/bin/livekit-server --version 2>&1 | awk '{print "v"$NF}' || echo "?")"
        if [[ "$current" == "$LIVEKIT_VERSION" ]]; then
            log "  已装 $current, 跳过"
            return
        fi
        log "  当前 $current, 升级到 $LIVEKIT_VERSION"
    fi
    # 官方 install.sh 拉最新; 我们要固定版本, 直接下载 tar
    local arch tar_url tmp
    case "$(uname -m)" in
        x86_64) arch="amd64" ;;
        aarch64) arch="arm64" ;;
        *) die "不支持的 CPU 架构: $(uname -m)" ;;
    esac
    tar_url="https://github.com/livekit/livekit/releases/download/${LIVEKIT_VERSION}/livekit_${LIVEKIT_VERSION#v}_linux_${arch}.tar.gz"
    tmp="$(mktemp -d)"
    log "  下载 $tar_url"
    curl -fsSL "$tar_url" -o "$tmp/lk.tar.gz" || die "下载失败: $tar_url"
    tar -xzf "$tmp/lk.tar.gz" -C "$tmp"
    sudo install -m 0755 "$tmp/livekit-server" /usr/local/bin/livekit-server
    rm -rf "$tmp"
    log "  装到 /usr/local/bin/livekit-server"
}

ensure_keys() {
    # 写 ~/livekit-server/.api_key 和 .api_secret (mode 0600)
    # 已有就复用, 没有就生成. --rotate-keys 时强制重生成.
    local kdir="$HOME/livekit-server"
    mkdir -p "$kdir"
    chmod 0700 "$kdir"
    if [[ "$ACTION" == "rotate" ]] || [[ ! -f "$kdir/.api_key" ]]; then
        # API key 格式: API + 16 hex
        local k="API$(openssl rand -hex 8)"
        # API secret 格式: 32 byte base64 (LiveKit 推荐 ≥32 bytes)
        local s
        s="$(openssl rand -base64 32 | tr -d '=' | tr '/+' '_-')"
        umask 0077
        echo -n "$k" > "$kdir/.api_key"
        echo -n "$s" > "$kdir/.api_secret"
        umask 0022
        log "  生成新 API key (rotate=$([[ $ACTION == rotate ]] && echo yes || echo no))"
    else
        log "  复用已有 API key/secret"
    fi
}

install_livekit_server_config() {
    log "[2/4] livekit-server config.yaml..."
    local kdir="$HOME/livekit-server"
    local k s
    k="$(cat "$kdir/.api_key")"
    s="$(cat "$kdir/.api_secret")"
    render_template "$INFRA_DIR/livekit-server-config.yaml.tmpl" "$kdir/config.yaml" \
        "API_KEY=$k" "API_SECRET=$s"
    chmod 0600 "$kdir/config.yaml"
    log "  写入 $kdir/config.yaml"
}

install_pnpm() {
    if command -v pnpm &>/dev/null; then
        log "  pnpm $(pnpm --version) 已装"
        return
    fi
    if ! command -v node &>/dev/null; then
        die "Node.js 未装. 先装 Node 22+ (deploy.sh --cc-only 会装)."
    fi
    log "  安装 pnpm (npm i -g pnpm)..."
    sudo npm install -g pnpm 2>&1 | tail -3
}

install_livekit_frontend() {
    log "[3/4] livekit-frontend (Next.js)..."
    install_pnpm
    local fdir="$HOME/livekit-frontend"
    if [[ ! -d "$fdir/.git" ]]; then
        log "  clone $FRONTEND_REPO ($FRONTEND_BRANCH)"
        git clone -b "$FRONTEND_BRANCH" "$FRONTEND_REPO" "$fdir"
    else
        log "  pull $fdir"
        ( cd "$fdir" && git pull --ff-only ) || log "  WARN: pull 失败, 用本地版本"
    fi
    # 生成 .env.local
    local k s pnpm_bin
    k="$(cat "$HOME/livekit-server/.api_key")"
    s="$(cat "$HOME/livekit-server/.api_secret")"
    render_template "$INFRA_DIR/frontend-env.local.tmpl" "$fdir/.env.local" \
        "API_KEY=$k" "API_SECRET=$s" \
        "PUBLIC_WSS_URL=wss://$SIGNALING_DOMAIN" \
        "DEFAULT_AGENT_NAME=$DEFAULT_AGENT_NAME"
    chmod 0600 "$fdir/.env.local"
    log "  写入 $fdir/.env.local"
    # pnpm install + build
    log "  pnpm install (这一步可能要 1-2 分钟)..."
    ( cd "$fdir" && pnpm install --frozen-lockfile 2>&1 | tail -3 )
    log "  pnpm build..."
    ( cd "$fdir" && pnpm build 2>&1 | tail -5 )
    log "  frontend 就绪"
}

install_caddy() {
    log "[4a/4] Caddy..."
    if ! command -v caddy &>/dev/null; then
        log "  apt install caddy"
        # 官方 apt repo
        sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl 2>&1 | tail -1
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
            | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
            | sudo tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null
        sudo apt-get update -qq
        sudo apt-get install -y caddy 2>&1 | tail -1
    else
        log "  caddy $(caddy version | head -1) 已装"
    fi
    # 渲染 Caddyfile
    local tmp
    tmp="$(mktemp)"
    render_template "$INFRA_DIR/Caddyfile.tmpl" "$tmp" \
        "FRONTEND_DOMAIN=$FRONTEND_DOMAIN" \
        "SIGNALING_DOMAIN=$SIGNALING_DOMAIN" \
        "ADMIN_EMAIL=$ADMIN_EMAIL"
    sudo install -m 0644 "$tmp" /etc/caddy/Caddyfile
    rm -f "$tmp"
    log "  写入 /etc/caddy/Caddyfile"
    sudo systemctl reload caddy 2>/dev/null || sudo systemctl restart caddy
}

install_systemd_units() {
    log "[4b/4] systemd unit 文件..."
    local pnpm_bin
    pnpm_bin="$(command -v pnpm)"
    [[ -n "$pnpm_bin" ]] || die "pnpm 不在 PATH"
    # server unit
    local tmp
    tmp="$(mktemp)"
    render_template "$INFRA_DIR/livekit-server.service.tmpl" "$tmp" \
        "USER=$USER" "HOME=$HOME"
    sudo install -m 0644 "$tmp" /etc/systemd/system/livekit-server.service
    # frontend unit
    render_template "$INFRA_DIR/livekit-frontend.service.tmpl" "$tmp" \
        "USER=$USER" "HOME=$HOME" "PNPM_BIN=$pnpm_bin"
    sudo install -m 0644 "$tmp" /etc/systemd/system/livekit-frontend.service
    rm -f "$tmp"
    sudo systemctl daemon-reload
    sudo systemctl enable livekit-server livekit-frontend 2>&1 | tail -1
    log "  unit 文件就绪"
}

start_services() {
    log "启动 services..."
    sudo systemctl restart livekit-server
    sleep 1
    sudo systemctl restart livekit-frontend
    log "  livekit-server  : $(systemctl is-active livekit-server)"
    log "  livekit-frontend: $(systemctl is-active livekit-frontend)"
    log "  caddy           : $(systemctl is-active caddy)"
}

# ====================================================================
# Action 派发
# ====================================================================

do_install() {
    require_args
    log "目标: frontend=$FRONTEND_DOMAIN signaling=$SIGNALING_DOMAIN email=$ADMIN_EMAIL"
    log "DNS 检查 (失败不会 abort, 但 Caddy 会拿不到证书):"
    dns_check "$FRONTEND_DOMAIN"
    dns_check "$SIGNALING_DOMAIN"
    install_livekit_server
    ensure_keys
    install_livekit_server_config
    install_livekit_frontend
    install_caddy
    install_systemd_units
    start_services
    cat <<EOF

[install-livekit] === 安装完成 ===

  Frontend:  https://$FRONTEND_DOMAIN
  Signaling: wss://$SIGNALING_DOMAIN
  API key:   $(cat "$HOME/livekit-server/.api_key")
  API secret: (在 ~/livekit-server/.api_secret)

下一步:
  1. 把 voice 凭据写入 Firestore (替换 <bot> 为你的 bot 名):
     python3 scripts/config-manage.py set-livekit <bot> --auto-detect \\
         --frontend-url https://$FRONTEND_DOMAIN --enable

  2. 启动 / 重启 bot, voice 会自动生成 HMAC secret 并回写 Firestore.

  3. 在飞书私聊给 bot 发 /voice 验证.

如果 frontend 起不来检查:  sudo systemctl status livekit-frontend && sudo journalctl -u livekit-frontend -n 50
如果 server 起不来检查:    sudo systemctl status livekit-server   && sudo journalctl -u livekit-server   -n 50
如果证书没签下来检查:      sudo journalctl -u caddy -n 50
EOF
}

do_refresh() {
    require_args
    log "刷新模板 (不动 key/secret, 不重装二进制)..."
    install_livekit_server_config   # 用现有 key/secret 重渲染 config.yaml
    # frontend .env.local
    local fdir="$HOME/livekit-frontend"
    [[ -d "$fdir" ]] || die "$fdir 不存在, 跑 install 而不是 refresh"
    local k s
    k="$(cat "$HOME/livekit-server/.api_key")"
    s="$(cat "$HOME/livekit-server/.api_secret")"
    render_template "$INFRA_DIR/frontend-env.local.tmpl" "$fdir/.env.local" \
        "API_KEY=$k" "API_SECRET=$s" \
        "PUBLIC_WSS_URL=wss://$SIGNALING_DOMAIN" \
        "DEFAULT_AGENT_NAME=$DEFAULT_AGENT_NAME"
    chmod 0600 "$fdir/.env.local"
    install_caddy        # 这一步会 reload caddy
    install_systemd_units
    log "刷新完成. 手动 systemctl restart livekit-server livekit-frontend 应用新配置."
}

do_rotate() {
    require_args
    log "轮换 API key/secret..."
    ensure_keys                       # ACTION=rotate, 强制重生成
    install_livekit_server_config
    do_refresh                        # 顺便把 frontend env 也更新了
    log "WARN: 旧 key 已失效. 必须重启所有用 voice 的 bot 让它从 Firestore 重新拉新 key,"
    log "      并跑 'python3 scripts/config-manage.py set-livekit <bot> --auto-detect' 把新 key 写回 Firestore."
}

do_uninstall() {
    log "卸载 voice infra (保留 ~/livekit-server/.api_secret 备份, 不删 frontend 源码)..."
    sudo systemctl stop livekit-server livekit-frontend 2>/dev/null || true
    sudo systemctl disable livekit-server livekit-frontend 2>/dev/null || true
    sudo rm -f /etc/systemd/system/livekit-server.service /etc/systemd/system/livekit-frontend.service
    sudo systemctl daemon-reload
    sudo rm -f /usr/local/bin/livekit-server
    log "完成. 没动的:"
    log "  ~/livekit-server/         (含 .api_key/.api_secret 备份)"
    log "  ~/livekit-frontend/       (源码 + node_modules)"
    log "  /etc/caddy/Caddyfile      (如要清掉手动 sudo rm + systemctl reload caddy)"
    log "  Firestore bots/<bot>.livekit (用 config-manage.py 改)"
}

case "$ACTION" in
    install)   do_install ;;
    refresh)   do_refresh ;;
    rotate)    do_rotate ;;
    uninstall) do_uninstall ;;
esac
