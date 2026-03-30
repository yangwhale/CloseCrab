#!/bin/bash
# ====================================================================
# 环境变量声明 — deploy.sh 的 single source of truth
#
# 三类变量:
#   1. secrets:  交互收集，存 ~/.zshenv，模板用 ${VAR} 占位
#   2. dynamic:  运行时计算，存 ~/.zshenv + settings.json
#   3. static:   固定值，直接写死在 settings.json 模板里
#
# Bot secrets (Discord token, 飞书 app_secret 等) 已迁移到 Firestore，
# 不再通过 .env 或环境变量管理。
# ====================================================================

# --- Secrets（需要交互收集，模板中用 ${VAR} 占位）---
CC_SECRETS=(
    ANTHROPIC_VERTEX_PROJECT_ID
    CC_PAGES_URL_PREFIX
    CONTEXT7_API_KEY
    GCS_BUCKET
    GEMINI_API_KEY
    GITHUB_PERSONAL_ACCESS_TOKEN
    JINA_API_KEY
    TAVILY_API_KEY
)

# --- Dynamic（运行时计算）---
compute_dynamic_vars() {
    # CC Pages: 统一 GCS 托管，所有机器用同一个 URL 前缀
    CC_PAGES_URL_PREFIX="${CC_PAGES_URL_PREFIX:-}"
    export CC_PAGES_URL_PREFIX

    # gcsfuse 挂载点：gLinux 用 ~/gcs-mount/cc-pages，VMs 用 /gcs/cc-pages
    if [[ -d "$HOME/gcs-mount/cc-pages" ]]; then
        CC_PAGES_WEB_ROOT="$HOME/gcs-mount/cc-pages"
    else
        CC_PAGES_WEB_ROOT="/gcs/cc-pages"
    fi
    export CC_PAGES_WEB_ROOT
}

# 需要 envsubst 替换的所有变量名（secrets + dynamic）
CC_ENVSUBST_VARS='$ANTHROPIC_VERTEX_PROJECT_ID $CC_PAGES_URL_PREFIX $CC_PAGES_WEB_ROOT $CONTEXT7_API_KEY $GCS_BUCKET $GEMINI_API_KEY $GITHUB_PERSONAL_ACCESS_TOKEN $JINA_API_KEY $TAVILY_API_KEY'

# 需要持久化到 ~/.zshenv 的变量
CC_DYNAMIC_PERSIST=(
    CC_PAGES_URL_PREFIX
    CC_PAGES_WEB_ROOT
)
