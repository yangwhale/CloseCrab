#!/bin/bash
# Copyright 2025-2026 Chris Yang (yangwhale)
# Licensed under the Apache License, Version 2.0
#
# install-livekit.sh — 装 / 维护 CloseCrab 语音栈的 LiveKit infra
#
# ════════════════════════════════════════════════════════════════════
# 跟 2026-09-12 之前那版的两个根本差别，先读完再改
# ════════════════════════════════════════════════════════════════════
#
# 1. **四个组件可以分别装在不同机器上。** 老版本假设一台机器装全套，
#    而现行生产从来不是这样：SFU 要离用户近（媒体延迟），agent 必须跑在
#    Gemini Developer API 支持的地区（香港会被拒，见 agent/env.tmpl），
#    这俩天然分居。老版本在非 SFU 机器上直接跑不起来 —— 它读本机
#    ~/livekit-server/.api_key，那台机器上根本没有这个文件。
#
# 2. **key/secret 的唯一真相是 Firestore `config/livekit`**，不是某台机器上
#    的文件。sfu 组件生成后发布上去，frontend / agent 从那里拉。bot 侧
#    (closecrab/voice/livekit_out.py) 本来就读这里，现在部署侧跟它对齐了。
#
# ── 四个组件 ────────────────────────────────────────────────────────
#   sfu       livekit-server 二进制 + /etc/livekit/config.yaml + systemd unit
#   frontend  Next.js 前端：clone 上游 + 盖上 infra/livekit/frontend/ 的补丁
#             + .env.local + systemd unit
#   agent     Gemini Live agent：venv + agent.py + personas/ + .env + unit
#   caddy     反代。**drop-in 写法** —— 只写 /etc/caddy/sites/ 下的片段，
#             绝不整份覆盖 /etc/caddy/Caddyfile（那份通常还挂着别的站点）
#
# ── 不做的事 ────────────────────────────────────────────────────────
#   - DNS A 记录（手动配，脚本只在 direct 模式下检查解析）
#   - GCP firewall / GCLB / IAP（手动，注意事项写在 Caddyfile-gclb-iap.tmpl 里）
#   - Firestore bots/{name}.livekit（用 scripts/config-manage.py set-livekit）
#   - HMAC key 文件（~/.closecrab-voice-hmac-{bot}.key）—— bot 启动时自动生成
#   - GEMINI_API_KEY —— 只从环境变量或已有 .env 里取，绝不生成也绝不打印
#
# ── 典型用法 ────────────────────────────────────────────────────────
#   # SFU + 反代那台（媒体落地的机器）
#   ./scripts/install-livekit.sh --component sfu,caddy \
#       --frontend-domain live.example.com \
#       --frontend-upstream 10.0.0.2:3000
#
#   # 前端 + agent 那台
#   GEMINI_API_KEY=... ./scripts/install-livekit.sh --component frontend,agent \
#       --sfu-url ws://10.0.0.1:7880 \
#       --public-wss-url wss://live.example.com/lk \
#       --allowed-rooms bunny,jarvis
#
#   ./scripts/install-livekit.sh --check                  # 只体检，不改任何东西
#   ./scripts/install-livekit.sh --component frontend --refresh-templates
#   ./scripts/install-livekit.sh --component sfu --rotate-keys
#   ./scripts/install-livekit.sh --component sfu,caddy --uninstall

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INFRA_DIR="$SCRIPT_DIR/infra/livekit"

# ----- 默认参数 -----
COMPONENTS=""
CADDY_MODE="gclb-iap"
FRONTEND_DOMAIN=""
SIGNALING_DOMAIN=""
ADMIN_EMAIL=""
FRONTEND_UPSTREAM=""
SFU_UPSTREAM="127.0.0.1:7880"
SFU_URL=""
PUBLIC_WSS_URL=""
AGENT_NAME="${AGENT_NAME:-}"          # 留空 = 匿名派发（Gemini Live agent 用这个）
ALLOWED_ROOMS=""
ADMIN_URL=""
ALLOW_INSECURE_TOKEN="false"
FORCE_CADDY="false"
ACTION="install"
AGENT_GEMINI_KEY=""

# 这些可以用环境变量覆盖 —— 主要为了能在 scratch 目录里测脚本，正常部署别动。
LIVEKIT_VERSION="${LIVEKIT_VERSION:-v1.13.6}"
FRONTEND_REPO="${FRONTEND_REPO:-https://github.com/livekit-examples/agent-starter-react.git}"
FRONTEND_REF="${FRONTEND_REF:-44b8a0c}"
FRONTEND_DIR="${FRONTEND_DIR:-$HOME/livekit-frontend}"
AGENT_DIR="${AGENT_DIR:-$HOME/lk-gemini-agent}"
SFU_CONFIG="${SFU_CONFIG:-/etc/livekit/config.yaml}"
CADDY_SITES_DIR="${CADDY_SITES_DIR:-/etc/caddy/sites}"
CADDY_MAIN="${CADDY_MAIN:-/etc/caddy/Caddyfile}"
CADDY_DROPIN_NAME="closecrab-livekit.caddy"

usage() {
    cat <<EOF
用法: $0 --component <list> [OPTIONS]

组件 (逗号分隔; install/refresh/uninstall 必填, --check 不填则全查):
  sfu        livekit-server + $SFU_CONFIG + systemd unit
  frontend   Next.js 前端 ($FRONTEND_DIR) + systemd unit
  agent      Gemini Live agent ($AGENT_DIR) + systemd unit
  caddy      反代站点片段 ($CADDY_SITES_DIR/$CADDY_DROPIN_NAME)

操作 (互斥, 默认 install):
  --refresh-templates  只重渲染配置/unit, 不重装二进制、不动 key
  --rotate-keys        生成新 API key/secret 并发布到 Firestore (要重启所有 voice bot)
  --check              只体检, 不改任何东西 (退出码 0=全绿, 1=有问题)
  --uninstall          停服务 + 删 unit + 删本组件装的二进制

拓扑参数:
  --caddy-mode gclb-iap|direct   反代形态, 默认 gclb-iap (现行生产)
  --frontend-domain DOMAIN       对外域名 (caddy 必填)
  --signaling-domain DOMAIN      独立 signaling 域名 (只有 direct 模式要)
  --admin-email EMAIL            Let's Encrypt 邮箱 (只有 direct 模式要)
  --frontend-upstream HOST:PORT  Caddy 回源前端的地址, 默认 127.0.0.1:3000
  --sfu-upstream HOST:PORT       Caddy 回源 SFU 的地址, 默认 127.0.0.1:7880
  --sfu-url ws://HOST:PORT       frontend/agent 连 SFU 的**内网** ws 地址
                                 (不填则用 Firestore config/livekit.url)
  --public-wss-url wss://...     浏览器侧 signaling URL
                                 (gclb-iap 形态下是 wss://<域名>/lk)

前端可选项:
  --agent-name NAME        显式派发的 agent 名。**Gemini Live agent 要留空**
                           (它匿名注册) —— 名字对不上时两边都不报错, 谁也等不到谁
  --allowed-rooms a,b,c    允许用 ?room= 进的房间白名单 (== bot 名 == persona 文件名)
  --admin-url http://...   /admin 调 RoomService 用的内网地址
  --allow-insecure-token   打开无鉴权的 token 端点 (前面必须有 IAP/basicauth 挡着)

其他:
  --force-caddy            主 Caddyfile 里已有同名站点时仍然装 drop-in
                           (默认拒绝 —— 两个同地址站点块会让 Caddy 拒绝加载)

环境变量:
  LIVEKIT_VERSION   默认 $LIVEKIT_VERSION
  FRONTEND_REPO     默认上游 livekit-examples/agent-starter-react
  FRONTEND_REF      钉住的 commit, 默认 $FRONTEND_REF
  GEMINI_API_KEY    agent 组件用。不设则沿用 $AGENT_DIR/.env 里已有的值
EOF
}

# ----- 解析参数 -----
while [[ $# -gt 0 ]]; do
    case "$1" in
        --component|--components) COMPONENTS="$2"; shift 2 ;;
        --caddy-mode)         CADDY_MODE="$2"; shift 2 ;;
        --frontend-domain)    FRONTEND_DOMAIN="$2"; shift 2 ;;
        --signaling-domain)   SIGNALING_DOMAIN="$2"; shift 2 ;;
        --admin-email)        ADMIN_EMAIL="$2"; shift 2 ;;
        --frontend-upstream)  FRONTEND_UPSTREAM="$2"; shift 2 ;;
        --sfu-upstream)       SFU_UPSTREAM="$2"; shift 2 ;;
        --sfu-url)            SFU_URL="$2"; shift 2 ;;
        --public-wss-url)     PUBLIC_WSS_URL="$2"; shift 2 ;;
        --agent-name)         AGENT_NAME="$2"; shift 2 ;;
        --allowed-rooms)      ALLOWED_ROOMS="$2"; shift 2 ;;
        --admin-url)          ADMIN_URL="$2"; shift 2 ;;
        --allow-insecure-token) ALLOW_INSECURE_TOKEN="true"; shift ;;
        --force-caddy)        FORCE_CADDY="true"; shift ;;
        --refresh-templates)  ACTION="refresh"; shift ;;
        --rotate-keys)        ACTION="rotate"; shift ;;
        --check)              ACTION="check"; shift ;;
        --uninstall)          ACTION="uninstall"; shift ;;
        --help|-h)            usage; exit 0 ;;
        *) echo "未知参数: $1"; usage; exit 1 ;;
    esac
done

# ====================================================================
# 工具函数
# ====================================================================

log()  { echo "[install-livekit] $*"; }
warn() { echo "[install-livekit] WARN: $*" >&2; }
die()  { echo "[install-livekit] ERROR: $*" >&2; exit 1; }

WANT_SFU=false; WANT_FRONTEND=false; WANT_AGENT=false; WANT_CADDY=false

parse_components() {
    # --check 不填组件 = 全查; 其余操作必须显式写.
    if [[ -z "$COMPONENTS" ]]; then
        if [[ "$ACTION" == "check" ]]; then
            COMPONENTS="sfu,frontend,agent,caddy"
        else
            die "必须指定 --component。这台机器该装哪几个只有你知道 ——
       猜一个默认值等于把老版本「一台机器装全套」的错误假设又写回来。
       例: --component sfu,caddy   或   --component frontend,agent"
        fi
    fi
    local c
    local -a comps
    IFS=',' read -ra comps <<< "$COMPONENTS"
    for c in "${comps[@]}"; do
        case "$(echo "$c" | tr -d ' ')" in
            sfu)      WANT_SFU=true ;;
            frontend) WANT_FRONTEND=true ;;
            agent)    WANT_AGENT=true ;;
            caddy)    WANT_CADDY=true ;;
            "")       ;;
            *) die "未知组件: $c (可选: sfu / frontend / agent / caddy)" ;;
        esac
    done
}

require_caddy_args() {
    # 必须写成 if, 不能用 `[[ -z X ]] && die`. 后者作为函数最后一条语句时,
    # 参数齐全 → 条件为假 → 整个 && 复合命令返回 1 → 函数返回 1 →
    # set -e 把脚本干掉, 而且一个字都不打. 2026-09-12 踩到: 参数明明全对,
    # 脚本静默 exit 1, 日志空白.
    if [[ -z "$FRONTEND_DOMAIN" ]]; then die "caddy 组件缺 --frontend-domain"; fi
    if [[ "$CADDY_MODE" == "direct" ]]; then
        if [[ -z "$SIGNALING_DOMAIN" ]]; then die "--caddy-mode direct 缺 --signaling-domain"; fi
        if [[ -z "$ADMIN_EMAIL"      ]]; then die "--caddy-mode direct 缺 --admin-email"; fi
    elif [[ "$CADDY_MODE" != "gclb-iap" ]]; then
        die "--caddy-mode 只能是 gclb-iap 或 direct, 收到: $CADDY_MODE"
    fi
    if [[ -z "$FRONTEND_UPSTREAM" ]]; then FRONTEND_UPSTREAM="127.0.0.1:3000"; fi
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
    local domain="$1" resolved
    resolved="$(getent hosts "$domain" | awk '{print $1}' | head -1 || true)"
    if [[ -z "$resolved" ]]; then
        warn "$domain 解析失败, Caddy 申请 LE 证书会失败. 请先配 DNS A 记录指向本机."
    else
        log "  $domain → $resolved"
    fi
}

unit_state() { systemctl is-active "$1" 2>/dev/null || echo "absent"; }

# ── Firestore config/livekit 是 key/secret 的唯一真相 ─────────────────
#
# 为什么不放本机文件: frontend 和 agent 常常跟 SFU 不在一台机器上, 它们需要
# 同一对 key 才能签 JWT / 注册 worker. 靠人肉 scp 那对值, 迟早出现「某台机器
# 上还是旧 key」—— 表现是那一路静默连不上, 两边日志全绿.
#
# 三个字段: url (SFU 内网 ws 地址) / api_key / api_secret.
# closecrab/voice/livekit_out.py 读的就是这个文档, 部署侧现在跟它同源.

LK_API_KEY=""; LK_API_SECRET=""; LK_URL=""

# ── 怎么访问 Firestore: REST + gcloud token, **不用 google-cloud-firestore** ──
#
# 这是个 bootstrap 脚本 —— 它要能在一台什么都没装的机器上把 SFU 跑起来.
# 用 `from google.cloud import firestore` 就等于要求目标机先装好 pip 包,
# 而 SFU 那台机器上根本没有 bot 的 Python 环境 (实测 hk-jmp:
# `ModuleNotFoundError: No module named 'google'`), 还要跟 PEP 668 的
# externally-managed 打架. REST 只要 curl + python3 标准库的 json.
#
# project / database 仍然只有**一个来源**: FIRESTORE_PROJECT /
# FIRESTORE_DATABASE 这两个环境变量 —— 跟 closecrab/constants.py 读的是同一对,
# 不是另起炉灶. 有 .env 就 source 它 (deploy.sh 生成的就是这两行), 没有就报错.
FS_TOKEN=""

fs_bootstrap() {
    if [[ -z "${FIRESTORE_PROJECT:-}" || -z "${FIRESTORE_DATABASE:-}" ]]; then
        # shellcheck disable=SC1091
        [[ -f "$SCRIPT_DIR/.env" ]] && source "$SCRIPT_DIR/.env"
    fi
    [[ -n "${FIRESTORE_PROJECT:-}" && -n "${FIRESTORE_DATABASE:-}" ]] || die \
        "FIRESTORE_PROJECT / FIRESTORE_DATABASE 没有值。要么 export 它们,
   要么让 $SCRIPT_DIR/.env 存在 (deploy.sh 生成)。**不猜默认值** ——
   猜错会静默写进另一个库, 那种错最难查。"
    if [[ -z "$FS_TOKEN" ]]; then
        FS_TOKEN="$(gcloud auth print-access-token 2>/dev/null || true)"
        [[ -n "$FS_TOKEN" ]] || die \
            "拿不到 access token。先跑 gcloud auth login (或让本机有 metadata SA)。"
    fi
}

_fs_doc_url() {
    echo "https://firestore.googleapis.com/v1/projects/${FIRESTORE_PROJECT}/databases/${FIRESTORE_DATABASE}/documents/config/livekit"
}

fs_get_keys() {
    fs_bootstrap
    local body
    body="$(curl -s -m 20 -H "Authorization: Bearer $FS_TOKEN" "$(_fs_doc_url)" || true)"
    # 文档不存在时 Firestore 回 404 JSON, 下面解析成三个空串 —— 那是 sfu 首次
    # 安装的正常路径 (随后会生成并 fs_put_keys). 拿不到 key 该不该停由调用方
    # 决定: need_keys 停, sfu 不停.
    local out
    out="$(FS_BODY="$body" python3 -c '
import json, os
try:
    f = (json.loads(os.environ["FS_BODY"]) or {}).get("fields", {})
except Exception:
    f = {}
for k in ("api_key", "api_secret", "url"):
    print(f.get(k, {}).get("stringValue", ""))
' 2>/dev/null || true)"
    LK_API_KEY="$(echo "$out"    | sed -n 1p)"
    LK_API_SECRET="$(echo "$out" | sed -n 2p)"
    LK_URL="$(echo "$out"        | sed -n 3p)"
}

fs_put_keys() {
    fs_bootstrap
    # updateMask 列出这三个字段 == SDK 的 merge=True: 只动这三个,
    # 同一个文档里别人加的字段原样留着.
    local url; url="$(_fs_doc_url)"
    url+="?updateMask.fieldPaths=api_key&updateMask.fieldPaths=api_secret&updateMask.fieldPaths=url"
    local payload
    payload="$(LK_K="$LK_API_KEY" LK_S="$LK_API_SECRET" LK_U="$LK_URL" python3 -c '
import json, os
print(json.dumps({"fields": {
    "api_key":    {"stringValue": os.environ["LK_K"]},
    "api_secret": {"stringValue": os.environ["LK_S"]},
    "url":        {"stringValue": os.environ["LK_U"]},
}}))')"
    local code
    code="$(curl -s -m 20 -o /dev/null -w '%{http_code}' -X PATCH "$url" \
        -H "Authorization: Bearer $FS_TOKEN" -H "Content-Type: application/json" \
        -d "$payload" || true)"
    [[ "$code" == 200 ]] || die "写 Firestore config/livekit 失败 (HTTP $code)"
    log "  已发布到 Firestore config/livekit (key 前缀 ${LK_API_KEY:0:8}…)"
}

need_keys() {
    # frontend / agent 用: 必须拿到 key, 拿不到就停, **不自己生成** ——
    # 自己生成一对等于跟 SFU 那边对不上, 而且两边都不会报错, 只是谁也连不上谁.
    fs_get_keys
    if [[ -z "$LK_API_KEY" || -z "$LK_API_SECRET" ]]; then
        die "Firestore config/livekit 里没有 api_key/api_secret。
       先在 SFU 那台机器上跑: ./scripts/install-livekit.sh --component sfu
       它会生成并发布这对值。"
    fi
    if [[ -n "$SFU_URL" ]]; then
        LK_URL="$SFU_URL"
    elif [[ -z "$LK_URL" ]]; then
        die "不知道 SFU 在哪。给 --sfu-url ws://<SFU内网IP>:7880,
       或先让 sfu 组件把 url 写进 Firestore config/livekit。"
    fi
}

# ====================================================================
# 组件: sfu
# ====================================================================

install_sfu_binary() {
    log "[sfu] livekit-server $LIVEKIT_VERSION..."
    if [[ -x /usr/local/bin/livekit-server ]]; then
        local current
        current="v$(/usr/local/bin/livekit-server --version 2>&1 | awk '{print $NF}')"
        if [[ "$current" == "$LIVEKIT_VERSION" ]]; then
            log "  已装 $current, 跳过"
            return
        fi
        log "  当前 $current, 换成 $LIVEKIT_VERSION"
    fi
    local arch tar_url tmp
    case "$(uname -m)" in
        x86_64)  arch="amd64" ;;
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

sfu_ensure_keys() {
    fs_get_keys
    if [[ "$ACTION" == "rotate" || -z "$LK_API_KEY" || -z "$LK_API_SECRET" ]]; then
        # key 格式 API + 16 hex; secret 是 32 字节 base64 (LiveKit 建议 ≥32 字节),
        # 转成 URL-safe 字母表, 免得它出现在 YAML / .env 里时还要考虑转义.
        LK_API_KEY="API$(openssl rand -hex 8)"
        LK_API_SECRET="$(openssl rand -base64 32 | tr -d '=' | tr '/+' '_-')"
        log "  生成新 API key/secret (rotate=$([[ $ACTION == rotate ]] && echo yes || echo no))"
    else
        log "  复用 Firestore 里已有的 API key/secret"
    fi
    # url 优先用显式给的; 否则保留已有; 再否则拿本机内网 IP 拼一个.
    if [[ -n "$SFU_URL" ]]; then
        LK_URL="$SFU_URL"
    elif [[ -z "$LK_URL" ]]; then
        local ip
        ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
        [[ -n "$ip" ]] || die "推不出本机内网 IP, 显式给 --sfu-url ws://<ip>:7880"
        LK_URL="ws://$ip:7880"
        log "  SFU url 未指定, 用本机内网地址: $LK_URL"
    fi
    fs_put_keys
}

install_sfu_config() {
    log "[sfu] $SFU_CONFIG..."
    local tmp; tmp="$(mktemp)"
    render_template "$INFRA_DIR/livekit-server-config.yaml.tmpl" "$tmp" \
        "API_KEY=$LK_API_KEY" "API_SECRET=$LK_API_SECRET"
    sudo install -D -m 0600 -o root -g root "$tmp" "$SFU_CONFIG"
    rm -f "$tmp"
    log "  写入 $SFU_CONFIG (root:root 0600)"
}

install_sfu_unit() {
    local tmp; tmp="$(mktemp)"
    render_template "$INFRA_DIR/livekit-server.service.tmpl" "$tmp" \
        "CONFIG_PATH=$SFU_CONFIG"
    sudo install -m 0644 "$tmp" /etc/systemd/system/livekit-server.service
    rm -f "$tmp"
    sudo systemctl daemon-reload
    sudo systemctl enable livekit-server 2>&1 | tail -1
    log "  unit 就绪: livekit-server.service"
}

# ====================================================================
# 组件: frontend
# ====================================================================

install_pnpm() {
    if command -v pnpm &>/dev/null; then
        log "  pnpm $(pnpm --version) 已装"
        return
    fi
    command -v node &>/dev/null || die "Node.js 未装. 先跑 deploy.sh --cc-only 装 Node 22+."
    log "  安装 pnpm (npm i -g pnpm)..."
    sudo npm install -g pnpm 2>&1 | tail -3
}

frontend_apply_overlay() {
    # 前端不是 fork, 是**上游 + 我们的覆盖层**.
    #
    # 为什么不 fork: 这几个文件 (token 路由、page.tsx 的 ?room= 处理、/admin)
    # 每次上游更新都要重新合. 放成覆盖层的话, 换上游版本只要改 FRONTEND_REF,
    # 冲突在 build 时暴露, 不用长期维护一条分叉分支.
    #
    # 代价说清楚: 上游要是改了同名文件, 我们的覆盖会**静默盖掉**它的新逻辑.
    # 所以 FRONTEND_REF 钉死 commit —— 升级是一个有意识的动作, 不是 git pull.
    local n=0 f rel
    while IFS= read -r f; do
        rel="${f#"$INFRA_DIR/frontend/"}"
        install -D -m 0644 "$f" "$FRONTEND_DIR/$rel"
        n=$((n + 1))
    done < <(find "$INFRA_DIR/frontend" -type f)
    log "  覆盖 $n 个补丁文件 (infra/livekit/frontend/ → $FRONTEND_DIR)"
}

write_frontend_env() {
    if [[ -z "$PUBLIC_WSS_URL" ]]; then
        die "frontend 组件缺 --public-wss-url (浏览器侧 signaling 地址)"
    fi
    # /admin 那条 RPC 走 http，跟 LIVEKIT_URL 是**同一个地址的两种 scheme**,
    # 所以从它换算而来, 不算「又找了一个配置来源」. 不给 --admin-url 时这么填.
    local admin_url="$ADMIN_URL"
    if [[ -z "$admin_url" ]]; then admin_url="${LK_URL/#ws:\/\//http://}"; fi

    local tmp; tmp="$(mktemp)"
    render_template "$INFRA_DIR/frontend-env.local.tmpl" "$tmp" \
        "API_KEY=$LK_API_KEY" "API_SECRET=$LK_API_SECRET" \
        "SFU_URL=$LK_URL" \
        "PUBLIC_WSS_URL=$PUBLIC_WSS_URL" \
        "DEFAULT_AGENT_NAME=$AGENT_NAME" \
        "ALLOWED_ROOMS=$ALLOWED_ROOMS" \
        "LIVEKIT_ADMIN_URL=$admin_url" \
        "ALLOW_INSECURE_TOKEN=$ALLOW_INSECURE_TOKEN"
    install -m 0600 "$tmp" "$FRONTEND_DIR/.env.local"
    rm -f "$tmp"
    log "  写入 $FRONTEND_DIR/.env.local (0600)"
    if [[ -z "$AGENT_NAME" ]]; then
        log "  AGENT_NAME 留空 = 匿名派发 (Gemini Live agent 用这个)"
    else
        log "  AGENT_NAME=$AGENT_NAME = 显式派发, agent 注册名必须**逐字相同**"
    fi
}

install_frontend() {
    log "[frontend] Next.js ($FRONTEND_DIR)..."
    install_pnpm
    if [[ ! -d "$FRONTEND_DIR/.git" ]]; then
        log "  clone $FRONTEND_REPO @ $FRONTEND_REF"
        git clone "$FRONTEND_REPO" "$FRONTEND_DIR"
    fi
    ( cd "$FRONTEND_DIR" && git fetch --quiet origin && git checkout --quiet "$FRONTEND_REF" ) \
        || warn "checkout $FRONTEND_REF 失败, 用工作树当前版本"
    frontend_apply_overlay
    register_library_patches
    write_frontend_env
    # 不能用 --frozen-lockfile: 覆盖层可能引入上游 lock 里没有的依赖,
    # 那时 frozen 会直接失败而不是解开.
    log "  pnpm install (1-2 分钟)..."
    ( cd "$FRONTEND_DIR" && pnpm install 2>&1 | tail -3 )
    log "  pnpm build..."
    ( cd "$FRONTEND_DIR" && pnpm build 2>&1 | tail -5 )
    install_frontend_unit
}

register_library_patches() {
    # 覆盖层里的 patches/*.patch 只是躺在磁盘上的文件, pnpm 不会自己发现它们 ——
    # 必须在 package.json 的 pnpm.patchedDependencies 里点名, 下一次 pnpm install
    # 才会打. 漏了这步不报错, 只是补丁静默不生效, 表现成「装完还是老 bug」.
    #
    # 不用 `pnpm patch-commit`: 那条命令要先 `pnpm patch` 开一份工作副本,
    # 而我们的补丁是现成的. 直接改 package.json 更直白, 也幂等.
    local dir="$FRONTEND_DIR/patches"
    [[ -d "$dir" ]] || return 0
    local n; n="$(find "$dir" -name '*.patch' | wc -l)"
    [[ "$n" -gt 0 ]] || return 0

    FRONTEND_DIR="$FRONTEND_DIR" python3 - <<'PY'
import json, os, pathlib

root = pathlib.Path(os.environ["FRONTEND_DIR"])
pkg_path = root / "package.json"
pkg = json.loads(pkg_path.read_text())

# 文件名就是 pnpm 的 key: `<包名, / 换成 __>@<版本>.patch`
entries = {
    p.stem.replace("__", "/"): f"patches/{p.name}"
    for p in sorted((root / "patches").glob("*.patch"))
}
pkg.setdefault("pnpm", {}).setdefault("patchedDependencies", {}).update(entries)
pkg_path.write_text(json.dumps(pkg, indent=2) + "\n")
for k in entries:
    print(f"  登记库补丁 {k}")
PY
}

install_frontend_unit() {
    local pnpm_bin node_bin tmp
    pnpm_bin="$(command -v pnpm)" || die "pnpm 不在 PATH"
    node_bin="$(command -v node)" || die "node 不在 PATH"
    tmp="$(mktemp)"
    render_template "$INFRA_DIR/livekit-frontend.service.tmpl" "$tmp" \
        "USER=$USER" "HOME=$HOME" "FRONTEND_DIR=$FRONTEND_DIR" \
        "PNPM_BIN=$pnpm_bin" \
        "NODE_BIN_DIR=$(dirname "$node_bin")" "PNPM_BIN_DIR=$(dirname "$pnpm_bin")"
    sudo install -m 0644 "$tmp" /etc/systemd/system/livekit-frontend.service
    rm -f "$tmp"
    sudo systemctl daemon-reload
    sudo systemctl enable livekit-frontend 2>&1 | tail -1
    log "  unit 就绪: livekit-frontend.service"
}

# ====================================================================
# 组件: agent (Gemini Live)
# ====================================================================

agent_resolve_gemini_key() {
    # 三个来源, 按优先级: 环境变量 → 已有 .env → 报错.
    # **绝不生成、绝不打印、绝不写进仓库。**
    if [[ -n "${GEMINI_API_KEY:-}" ]]; then
        AGENT_GEMINI_KEY="$GEMINI_API_KEY"
        log "  GEMINI_API_KEY 取自环境变量"
        return
    fi
    if [[ -f "$AGENT_DIR/.env" ]]; then
        AGENT_GEMINI_KEY="$(sed -n 's/^GEMINI_API_KEY=//p' "$AGENT_DIR/.env" | head -1)"
        if [[ -n "$AGENT_GEMINI_KEY" ]]; then
            log "  GEMINI_API_KEY 沿用 $AGENT_DIR/.env 里已有的值"
            return
        fi
    fi
    die "agent 组件需要 GEMINI_API_KEY (Gemini **Developer** API, 不是 Vertex)。
       用法: GEMINI_API_KEY=... $0 --component agent ...
       为什么必须 Developer API、以及为什么 agent 不能跟 SFU 同机,
       见 infra/livekit/agent/env.tmpl 里的注释。"
}

install_agent() {
    log "[agent] Gemini Live agent ($AGENT_DIR)..."
    agent_resolve_gemini_key
    mkdir -p "$AGENT_DIR"
    if [[ ! -x "$AGENT_DIR/.venv/bin/python" ]]; then
        log "  建 venv (turn-detector 会拖进 torch + onnxruntime, 数百 MB)"
        python3 -m venv "$AGENT_DIR/.venv"
    fi
    log "  pip install -r agent/requirements.txt (首次 3-5 分钟)..."
    "$AGENT_DIR/.venv/bin/pip" install -q --upgrade pip
    "$AGENT_DIR/.venv/bin/pip" install -q -r "$INFRA_DIR/agent/requirements.txt" 2>&1 | tail -3
    install -m 0644 "$INFRA_DIR/agent/agent.py" "$AGENT_DIR/agent.py"
    install -m 0755 "$INFRA_DIR/agent/speak_into_room.py" "$AGENT_DIR/speak_into_room.py"
    # personas 是**同步**不是叠加: 仓库里删掉一个房间的人格文件, 部署后它也该消失.
    rm -rf "$AGENT_DIR/personas"
    cp -a "$INFRA_DIR/agent/personas" "$AGENT_DIR/personas"
    log "  agent.py + speak_into_room.py + personas/ ($(ls "$AGENT_DIR/personas"/*.md 2>/dev/null | wc -l) 个) 就位"

    local tmp; tmp="$(mktemp)"
    render_template "$INFRA_DIR/agent/env.tmpl" "$tmp" \
        "SFU_URL=$LK_URL" \
        "API_KEY=$LK_API_KEY" "API_SECRET=$LK_API_SECRET"
    # GEMINI_API_KEY 单独塞, 不进 render_template 的参数列表 ——
    # 那些参数会出现在 ps 的命令行里, 同机任何用户都能看见.
    GK="$AGENT_GEMINI_KEY" python3 - "$tmp" <<'PYEOF'
import os, pathlib, re, sys
p = pathlib.Path(sys.argv[1])
p.write_text(re.sub(r"(?m)^GEMINI_API_KEY=.*$",
                    "GEMINI_API_KEY=" + os.environ["GK"], p.read_text()))
PYEOF
    install -m 0600 "$tmp" "$AGENT_DIR/.env"
    rm -f "$tmp"
    log "  写入 $AGENT_DIR/.env (0600)"

    tmp="$(mktemp)"
    render_template "$INFRA_DIR/lk-gemini-agent.service.tmpl" "$tmp" \
        "USER=$USER" "HOME=$HOME" "AGENT_DIR=$AGENT_DIR"
    sudo install -m 0644 "$tmp" /etc/systemd/system/lk-gemini-agent.service
    rm -f "$tmp"
    sudo systemctl daemon-reload
    sudo systemctl enable lk-gemini-agent 2>&1 | tail -1
    log "  unit 就绪: lk-gemini-agent.service"
}

# ====================================================================
# 组件: caddy —— drop-in, 绝不整份覆盖
# ====================================================================

install_caddy_pkg() {
    if command -v caddy &>/dev/null; then
        log "  caddy $(caddy version | head -1) 已装"
        return
    fi
    log "  apt install caddy"
    sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl 2>&1 | tail -1
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | sudo tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null
    sudo apt-get update -qq
    sudo apt-get install -y caddy 2>&1 | tail -1
}

ensure_caddy_import() {
    # 主 Caddyfile 只加一行 import, 其余一个字不动.
    #
    # 老版本这里是 `sudo install -m 0644 "$tmp" /etc/caddy/Caddyfile` —— 整份覆盖。
    # 生产那台机器的 Caddyfile 同时挂着 CC Pages、飞书回调和另外几个站点,
    # 跑一次老脚本 = 那些全没了, 而 Caddy 会照常 reload 成功, 不报任何错。
    sudo mkdir -p "$CADDY_SITES_DIR"
    if [[ -f "$CADDY_MAIN" ]] && sudo grep -qE "^\s*import\s+.*sites/\*\.caddy" "$CADDY_MAIN"; then
        log "  主 Caddyfile 已有 import, 不动"
        return
    fi
    if [[ -f "$CADDY_MAIN" ]]; then
        local bak="$CADDY_MAIN.bak-closecrab-$(date +%Y%m%d-%H%M%S)"
        sudo cp -a "$CADDY_MAIN" "$bak"
        log "  主 Caddyfile 备份到 $bak"
    else
        sudo mkdir -p "$(dirname "$CADDY_MAIN")"
        sudo touch "$CADDY_MAIN"
    fi
    # import 路径相对于主 Caddyfile 所在目录; 追加到文件末尾是安全的 ——
    # 只有全局选项块要求在最前面, 站点块之间的顺序不影响匹配.
    printf '\n# 由 scripts/install-livekit.sh 追加: 加载 %s 下的站点片段。\n# 这一行以外的内容脚本不会碰。\nimport %s/*.caddy\n' \
        "$CADDY_SITES_DIR" "$(basename "$CADDY_SITES_DIR")" \
        | sudo tee -a "$CADDY_MAIN" > /dev/null
    log "  主 Caddyfile 追加了 import $(basename "$CADDY_SITES_DIR")/*.caddy"
}

caddy_conflict_check() {
    # 同一个站点地址在主文件里已经手写过的话, 再装一份 drop-in, Caddy 会报
    # 重复站点定义并**拒绝加载整份配置** —— 连原来能用的站点一起停掉.
    # 这种失败方式比「没装上」严重得多, 所以默认拦下来.
    local addr="$1" esc
    esc="${addr//./\\.}"
    if [[ -f "$CADDY_MAIN" ]] && sudo grep -qE "^\s*(https?://)?${esc}([ :,{]|$)" "$CADDY_MAIN"; then
        if [[ "$FORCE_CADDY" == "true" ]]; then
            warn "$CADDY_MAIN 里已经手写了 $addr 的站点块, --force-caddy 强行继续"
            return
        fi
        die "$CADDY_MAIN 里已经手写了 $addr 的站点块。
       两个同地址站点块会让 Caddy 拒绝加载整份配置, 把这台机器上**所有**站点一起停掉。
       先把那一段从主文件里剪掉 (脚本会把等价内容写进
       $CADDY_SITES_DIR/$CADDY_DROPIN_NAME), 再重跑。
       确认过要并存就加 --force-caddy。"
    fi
}

install_caddy_site() {
    log "[caddy] 站点片段 (mode=$CADDY_MODE)..."
    install_caddy_pkg
    caddy_conflict_check "$FRONTEND_DOMAIN"
    [[ "$CADDY_MODE" != "direct" ]] || caddy_conflict_check "$SIGNALING_DOMAIN"
    ensure_caddy_import

    local tmp; tmp="$(mktemp)"
    if [[ "$CADDY_MODE" == "gclb-iap" ]]; then
        render_template "$INFRA_DIR/Caddyfile-gclb-iap.tmpl" "$tmp" \
            "FRONTEND_DOMAIN=$FRONTEND_DOMAIN" \
            "FRONTEND_UPSTREAM=$FRONTEND_UPSTREAM" \
            "SFU_UPSTREAM=$SFU_UPSTREAM"
    else
        dns_check "$FRONTEND_DOMAIN"
        dns_check "$SIGNALING_DOMAIN"
        render_template "$INFRA_DIR/Caddyfile.tmpl" "$tmp" \
            "FRONTEND_DOMAIN=$FRONTEND_DOMAIN" \
            "SIGNALING_DOMAIN=$SIGNALING_DOMAIN" \
            "ADMIN_EMAIL=$ADMIN_EMAIL" \
            "FRONTEND_UPSTREAM=$FRONTEND_UPSTREAM" \
            "SFU_UPSTREAM=$SFU_UPSTREAM"
    fi

    # 先备份旧片段, 装上, validate; 不过就回滚. 语法错的 Caddyfile 会让 reload
    # 失败并保持旧配置**继续运行**, 所以当场看不出问题 —— 但下一次 restart
    # (比如机器重启) 整个 Caddy 就起不来了, 那时候没人会想到是这次改的.
    local dst="$CADDY_SITES_DIR/$CADDY_DROPIN_NAME" bak=""
    if sudo test -f "$dst"; then bak="$(mktemp)"; sudo cp -a "$dst" "$bak"; fi
    sudo install -m 0644 "$tmp" "$dst"
    rm -f "$tmp"
    if ! sudo caddy validate --adapter caddyfile --config "$CADDY_MAIN" >/dev/null 2>&1; then
        if [[ -n "$bak" ]]; then sudo install -m 0644 "$bak" "$dst"; else sudo rm -f "$dst"; fi
        sudo caddy validate --adapter caddyfile --config "$CADDY_MAIN" 2>&1 | tail -20 >&2 || true
        die "新的 Caddy 配置校验不过, 已回滚 $dst"
    fi
    [[ -z "$bak" ]] || rm -f "$bak"
    log "  写入 $dst, caddy validate 通过"
    sudo systemctl reload caddy 2>/dev/null || sudo systemctl restart caddy
    log "  caddy: $(unit_state caddy)"
}

# ====================================================================
# 体检
# ====================================================================

CHECK_BAD=0
ck() {
    if [[ "$1" == 0 ]]; then printf '  ✅ %s\n' "$2"; else CHECK_BAD=1; printf '  ❌ %s\n' "$2"; fi
}
ck_warn() { printf '  ⚠️  %s\n' "$1"; }
b2rc() { [[ "$1" == true ]] && echo 0 || echo 1; }

port_open() { (exec 3<>"/dev/tcp/${1}/${2}") 2>/dev/null && { exec 3<&-; return 0; } || return 1; }

do_check() {
    log "体检 (components=$COMPONENTS)"

    echo "── Firestore config/livekit ──"
    fs_get_keys
    ck "$([[ -n $LK_API_KEY    ]] && echo 0 || echo 1)" "api_key 存在${LK_API_KEY:+ (${LK_API_KEY:0:8}…)}"
    ck "$([[ -n $LK_API_SECRET ]] && echo 0 || echo 1)" "api_secret 存在"
    ck "$([[ -n $LK_URL        ]] && echo 0 || echo 1)" "url = ${LK_URL:-<空>}"

    if [[ "$WANT_SFU" == true ]]; then
        echo "── sfu ──"
        if [[ -x /usr/local/bin/livekit-server ]]; then
            local v; v="v$(/usr/local/bin/livekit-server --version 2>&1 | awk '{print $NF}')"
            ck 0 "二进制 $v"
            [[ "$v" == "$LIVEKIT_VERSION" ]] || ck_warn "跟脚本钉的 $LIVEKIT_VERSION 不一致"
        else
            ck 1 "/usr/local/bin/livekit-server 不存在"
        fi
        sudo test -f "$SFU_CONFIG" && ck 0 "$SFU_CONFIG 存在" || ck 1 "$SFU_CONFIG 不存在"
        [[ "$(unit_state livekit-server)" == active ]] \
            && ck 0 "livekit-server.service active" \
            || ck 1 "livekit-server.service $(unit_state livekit-server)"
        port_open 127.0.0.1 7880 && ck 0 "7880 在听" || ck 1 "7880 没在听"
        # 本机 config 里的 key 必须和 Firestore 那份一致, 否则前端签的 JWT 会被
        # SFU 拒 —— 而日志里只有一行 401, 看不出是 key 不一致.
        if sudo test -f "$SFU_CONFIG" && [[ -n "$LK_API_KEY" ]]; then
            sudo grep -q "$LK_API_KEY:" "$SFU_CONFIG" \
                && ck 0 "config 里的 key 和 Firestore 一致" \
                || ck 1 "config 里的 key 跟 Firestore **对不上** (跑 --component sfu --refresh-templates)"
        fi
    fi

    if [[ "$WANT_FRONTEND" == true ]]; then
        echo "── frontend ──"
        [[ -d "$FRONTEND_DIR/.next" ]] && ck 0 "$FRONTEND_DIR/.next 已 build" || ck 1 "没 build 过"
        [[ -f "$FRONTEND_DIR/.env.local" ]] && ck 0 ".env.local 存在" || ck 1 ".env.local 不存在"
        if [[ -f "$FRONTEND_DIR/.env.local" && -n "$LK_API_KEY" ]]; then
            grep -q "^LIVEKIT_API_KEY=$LK_API_KEY$" "$FRONTEND_DIR/.env.local" \
                && ck 0 ".env.local 的 key 和 Firestore 一致" \
                || ck 1 ".env.local 的 key 跟 Firestore **对不上** (--component frontend --refresh-templates)"
        fi
        local drift=0 n=0 f rel
        while IFS= read -r f; do
            rel="${f#"$INFRA_DIR/frontend/"}"; n=$((n + 1))
            cmp -s "$f" "$FRONTEND_DIR/$rel" || { drift=1; ck_warn "补丁漂了: $rel"; }
        done < <(find "$INFRA_DIR/frontend" -type f)
        [[ $drift == 0 ]] && ck 0 "$n 个补丁文件跟仓库一致" || ck 1 "补丁文件跟仓库不一致"
        [[ "$(unit_state livekit-frontend)" == active ]] \
            && ck 0 "livekit-frontend.service active" \
            || ck 1 "livekit-frontend.service $(unit_state livekit-frontend)"
        port_open 127.0.0.1 3000 && ck 0 "3000 在听" || ck 1 "3000 没在听"
    fi

    if [[ "$WANT_AGENT" == true ]]; then
        echo "── agent ──"
        [[ -x "$AGENT_DIR/.venv/bin/python" ]] && ck 0 "venv 存在" || ck 1 "venv 不存在"
        [[ -f "$AGENT_DIR/.env" ]] && ck 0 ".env 存在" || ck 1 ".env 不存在"
        cmp -s "$INFRA_DIR/agent/agent.py" "$AGENT_DIR/agent.py" \
            && ck 0 "agent.py 跟仓库一致" \
            || ck 1 "agent.py 跟仓库不一致 (--component agent --refresh-templates)"
        local repo_n dep_n
        repo_n="$(ls "$INFRA_DIR/agent/personas"/*.md 2>/dev/null | wc -l)"
        dep_n="$(ls "$AGENT_DIR/personas"/*.md 2>/dev/null | wc -l)"
        [[ "$repo_n" == "$dep_n" ]] && ck 0 "personas $dep_n 个, 跟仓库一致" \
            || ck 1 "personas 仓库 $repo_n 个 / 部署 $dep_n 个"
        [[ "$(unit_state lk-gemini-agent)" == active ]] \
            && ck 0 "lk-gemini-agent.service active" \
            || ck 1 "lk-gemini-agent.service $(unit_state lk-gemini-agent)"
        # "registered worker" 绿了不代表 job 起得来 (见 unit 模板里 TMPDIR 那段),
        # 但没有这一行就一定没连上 SFU.
        #
        # 窗口必须是「本次启动至今」, 不能是 `-n 200`. 注册只在启动那一刻打一行,
        # 之后每通一次话都刷几十行 —— 服务稳定跑着反而会把它挤出尾部窗口,
        # 于是**越健康越会误报**. 2026-09-13 实测: 那行在 10732 行之前.
        local since; since="$(systemctl show -p ActiveEnterTimestamp --value lk-gemini-agent 2>/dev/null)"
        local jflag=(-u lk-gemini-agent --no-pager)
        [[ -n "$since" ]] && jflag+=(--since "$since")
        # **不要写成 `journalctl ... | grep -q`。** 本脚本开了 pipefail, 而 grep -q
        # 一命中就退出, 上游 journalctl 随即吃 SIGPIPE 退 141 —— 管道整体判失败,
        # 于是**命中反而走 else**。跟上面那条凑一起, 就是一个「服务越健康越报警」
        # 的双重误报。先落成变量再匹配, 彻底躲开 SIGPIPE。
        local jlog; jlog="$(journalctl "${jflag[@]}" 2>/dev/null || true)"
        if grep -q "registered worker" <<<"$jlog"; then
            ck 0 "已向 SFU 注册 worker"
        else
            ck_warn "本次启动以来没看到 registered worker"
        fi
    fi

    if [[ "$WANT_CADDY" == true ]]; then
        echo "── caddy ──"
        [[ "$(unit_state caddy)" == active ]] && ck 0 "caddy active" || ck 1 "caddy $(unit_state caddy)"
        sudo test -f "$CADDY_SITES_DIR/$CADDY_DROPIN_NAME" \
            && ck 0 "站点片段存在" \
            || ck_warn "$CADDY_SITES_DIR/$CADDY_DROPIN_NAME 不存在 (站点可能还手写在主 Caddyfile 里)"
        sudo caddy validate --adapter caddyfile --config "$CADDY_MAIN" >/dev/null 2>&1 \
            && ck 0 "caddy validate 通过" || ck 1 "caddy validate 不过"
    fi

    echo
    if [[ $CHECK_BAD == 0 ]]; then log "全绿"; else log "有问题, 见上面的 ❌"; fi
    return $CHECK_BAD
}

# ====================================================================
# Action 派发
# ====================================================================

start_services() {
    local u
    for u in "$@"; do
        sudo systemctl restart "$u"
        log "  $u: $(unit_state "$u")"
    done
}

do_install() {
    local units=()
    if [[ "$WANT_CADDY" == true ]]; then require_caddy_args; fi
    if [[ "$WANT_SFU" == true ]]; then
        install_sfu_binary; sfu_ensure_keys; install_sfu_config; install_sfu_unit
        units+=(livekit-server)
    fi
    if [[ "$WANT_FRONTEND" == true || "$WANT_AGENT" == true ]]; then
        [[ "$WANT_SFU" == true ]] || need_keys
    fi
    if [[ "$WANT_FRONTEND" == true ]]; then install_frontend; units+=(livekit-frontend); fi
    if [[ "$WANT_AGENT" == true ]];    then install_agent;    units+=(lk-gemini-agent); fi
    if [[ "$WANT_CADDY" == true ]];    then install_caddy_site; fi
    [[ ${#units[@]} -eq 0 ]] || start_services "${units[@]}"

    cat <<EOF

[install-livekit] === 完成 (components=$COMPONENTS) ===

下一步:
  1. 每个要用 /voice 的 bot 写一次 Firestore (key 从 config/livekit 自动取):
     python3 scripts/config-manage.py set-livekit <bot> --auto-detect \\
         --frontend-url https://${FRONTEND_DOMAIN:-<域名>} --enable
  2. 重启 bot —— voice 会自动生成 HMAC secret 并回写 Firestore
  3. ./scripts/install-livekit.sh --check     体检
  4. 浏览器开 https://${FRONTEND_DOMAIN:-<域名>}/?room=<bot名> 验证常开入口;
     飞书私聊发 /voice 验证一次性通话

排障: sudo journalctl -u livekit-server -u livekit-frontend -u lk-gemini-agent -n 50
EOF
}

do_refresh() {
    log "重渲染配置/unit (不重装二进制, 不动 key)"
    if [[ "$WANT_SFU" == true ]]; then
        fs_get_keys
        [[ -n "$LK_API_KEY" ]] || die "Firestore config/livekit 里没有 key, 先跑 install"
        install_sfu_config; install_sfu_unit
    fi
    if [[ "$WANT_FRONTEND" == true || "$WANT_AGENT" == true ]]; then
        [[ "$WANT_SFU" == true ]] || need_keys
    fi
    if [[ "$WANT_FRONTEND" == true ]]; then
        [[ -d "$FRONTEND_DIR" ]] || die "$FRONTEND_DIR 不存在, 跑 install 而不是 refresh"
        frontend_apply_overlay; register_library_patches; write_frontend_env; install_frontend_unit
        # 库补丁（patches/*.patch）比应用代码多一步: 它要 pnpm install 才会打进
        # node_modules, 光 build 是不够的.
        log "  注意: refresh **不 install 也不 build**. 改了应用层补丁跑"
        log "        (cd $FRONTEND_DIR && pnpm build); 改了 patches/ 下的库补丁要先 pnpm install"
    fi
    if [[ "$WANT_AGENT" == true ]]; then install_agent; fi
    if [[ "$WANT_CADDY" == true ]]; then require_caddy_args; install_caddy_site; fi
    log "刷新完成. 手动 systemctl restart 相应 unit 生效."
}

do_rotate() {
    [[ "$WANT_SFU" == true ]] || die "--rotate-keys 必须在 SFU 那台机器上跑 (--component sfu[,...])"
    log "轮换 API key/secret..."
    sfu_ensure_keys                 # ACTION=rotate, 强制重生成并发布
    install_sfu_config
    if [[ "$WANT_FRONTEND" == true ]]; then write_frontend_env; fi
    if [[ "$WANT_AGENT" == true ]];    then install_agent; fi
    sudo systemctl restart livekit-server
    log "  livekit-server: $(unit_state livekit-server)"
    warn "旧 key 已失效。还没做的三件事:"
    warn "  1. 在**其他**机器上跑 --component frontend,agent --refresh-templates 拉新 key"
    warn "  2. 重启那些机器上的 livekit-frontend / lk-gemini-agent"
    warn "  3. 重启所有用 voice 的 bot (它们启动时读 Firestore config/livekit)"
}

do_uninstall() {
    log "卸载 (components=$COMPONENTS)"
    if [[ "$WANT_SFU" == true ]]; then
        sudo systemctl disable --now livekit-server 2>/dev/null || true
        sudo rm -f /etc/systemd/system/livekit-server.service /usr/local/bin/livekit-server
        log "  留着没动: $SFU_CONFIG (含 key, 要删自己来)"
    fi
    if [[ "$WANT_FRONTEND" == true ]]; then
        sudo systemctl disable --now livekit-frontend 2>/dev/null || true
        sudo rm -f /etc/systemd/system/livekit-frontend.service
        log "  留着没动: $FRONTEND_DIR (源码 + node_modules)"
    fi
    if [[ "$WANT_AGENT" == true ]]; then
        sudo systemctl disable --now lk-gemini-agent 2>/dev/null || true
        sudo rm -f /etc/systemd/system/lk-gemini-agent.service
        log "  留着没动: $AGENT_DIR (venv + .env)"
    fi
    if [[ "$WANT_CADDY" == true ]]; then
        sudo rm -f "$CADDY_SITES_DIR/$CADDY_DROPIN_NAME"
        sudo systemctl reload caddy 2>/dev/null || true
        log "  删了站点片段; 主 Caddyfile 的 import 行留着 (目录空了也不影响)"
    fi
    sudo systemctl daemon-reload
    log "Firestore config/livekit 和 bots/<bot>.livekit 都没动 (用 config-manage.py 改)"
}

parse_components
case "$ACTION" in
    install)   do_install ;;
    refresh)   do_refresh ;;
    rotate)    do_rotate ;;
    check)     do_check ;;
    uninstall) do_uninstall ;;
esac
