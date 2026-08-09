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

    # gcsfuse 挂载点：优先检测已存在的目录，fallback 到 /gcs/cc-pages
    if [[ -d "$HOME/gcs-mount/cc-pages" ]]; then
        CC_PAGES_WEB_ROOT="$HOME/gcs-mount/cc-pages"
    elif [[ -d "/gcs/cc-pages" ]]; then
        CC_PAGES_WEB_ROOT="/gcs/cc-pages"
    else
        # 目录尚未存在，默认 /gcs/cc-pages（setup_gcsfuse 会创建）
        CC_PAGES_WEB_ROOT="/gcs/cc-pages"
    fi
    export CC_PAGES_WEB_ROOT

    # WIKI_REPO：这台机器归哪个 Wiki 管。
    # wiki skill 的 16 个脚本都是 os.environ.get("WIKI_REPO", "~/my-wiki")，
    # 即「环境变量优先 + 默认值兜底」。问题在于**没有任何地方记录这台机器该用哪个**，
    # 于是全都回落到默认的 ~/my-wiki —— 而那个目录在多数机器上并不存在，
    # 结果是一整套 rebuild-graph / build-search-index / fix-backlinks 静默失效。
    # 这里按机器探测一次并落盘，就是那份缺失的「归属记录」。
    #
    # 判据必须是「目录下有 content/」而不是「目录存在」。本机 ~/my-wiki-study
    # 就是个空目录，只看 -d 会把它选中并导出 —— 那正是上面这段注释在骂的
    # 「指向一个不是 Wiki 的路径，然后静默查空」。判据比顺序重要。
    if [[ -z "${WIKI_REPO:-}" ]]; then
        for _wiki_cand in "$HOME/my-wiki-v2" "$HOME/my-wiki" "$HOME/my-wiki-study"; do
            [[ -d "$_wiki_cand/content" ]] && WIKI_REPO="$_wiki_cand" && break
        done
    fi
    export WIKI_REPO
}

# 需要 envsubst 替换的所有变量名（secrets + dynamic）
CC_ENVSUBST_VARS='$ANTHROPIC_VERTEX_PROJECT_ID $CC_PAGES_URL_PREFIX $CC_PAGES_WEB_ROOT $CONTEXT7_API_KEY $GCS_BUCKET $GEMINI_API_KEY $GITHUB_PERSONAL_ACCESS_TOKEN $JINA_API_KEY $TAVILY_API_KEY $WIKI_REPO'

# 需要持久化到 ~/.zshenv 的变量
CC_DYNAMIC_PERSIST=(
    CC_PAGES_URL_PREFIX
    CC_PAGES_WEB_ROOT
    WIKI_REPO
)
