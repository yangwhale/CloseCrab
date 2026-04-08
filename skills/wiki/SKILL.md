---
name: wiki
description: 个人知识 Wiki 管理。录入资料、查询知识、健康检查、索引重建。当用户说"录入wiki"、"wiki ingest"、"加到wiki"、"wiki查一下"、"wiki lint"、"/wiki"等关键词时触发。
---

# CC Wiki — 个人知识 Wiki 管理

基于 CC Pages + GitHub Private Repo 的 LLM 维护知识 Wiki。灵感来自 Karpathy 的 LLM Wiki 模式。

## 触发条件

- `/wiki ingest <url|文件|文本>` — 录入新资料
- `/wiki query <问题>` — 基于 Wiki 回答问题
- `/wiki lint` — 健康检查
- `/wiki status` — 显示 Wiki 统计
- `/wiki init` — 首次初始化（创建 repo + 目录结构）
- 自然语言："帮我录入到 wiki"、"加到知识库"、"wiki 里有没有..."

## 第一性原理（来自 Karpathy LLM Wiki）

以下原则指导所有 Wiki 操作，是 Bot 的根本行为准则：

### 角色分工

**人类的职责**：策展来源、引导分析方向、提出好问题、思考这一切意味着什么。
**LLM 的职责**：其他所有事——总结、交叉引用、归档、簿记。

人类不需要写 Wiki 页面，就像产品经理不需要写代码。人类负责方向和质量，LLM 负责执行和维护。

### 知识编译，而非检索

不要每次从原始文档重新检索（RAG 模式）。而是将知识**编译一次，持续更新**。Wiki 是编译后的产物——交叉引用已经建好，矛盾已经标记，综合分析已经反映了所有已读内容。

### Schema 共同进化

SKILL.md 不是写好就不动的静态规则。它是一个**活文档**，随着使用不断迭代：
- Bot 在操作中发现规则不够用时，应主动建议修改
- 用户确认后，Bot 更新 SKILL.md 并记录变更原因
- 每次 Lint 时顺带审视规则是否需要调整
- 不同领域的 Wiki 可能需要不同的约定，让实践驱动规则演化

### 参与度由用户决定

录入资料时，用户可以选择参与度：
- **深度参与**（默认）：逐个录入，边录边讨论要点，引导 LLM 关注重点
- **批量模式**：一次性投放多个来源，LLM 自主处理，事后汇报
- 用户偏好应记录在 schema 中，后续 session 自动遵循

### 输出多样性

Wiki 的输出不局限于 HTML 页面。根据问题类型，可以生成：
- HTML 知识页面（默认）
- 对比表格
- 幻灯片（用 frontend-slides skill）
- 图表（SVG / Chart.js）
- Canvas 思维导图
- 任何有助于理解的形式

## 核心概念

### 三层架构

| 层 | 目录 | 谁写 | 谁读 | 说明 |
|----|------|------|------|------|
| Raw Sources | `raw/` | 人 | Bot | 原始资料，不可变 |
| Wiki | `wiki/` | Bot | 人 | HTML 知识页面，Bot 全权维护 |
| Schema | 此 SKILL.md | 人+Bot | Bot | 操作规则和约定 |

### 知识复利

- 每次 Ingest 不只是存档，而是编译进 Wiki（更新实体、概念、交叉引用）
- 每次 Query 的好回答可以回存为新的 Wiki 页面
- 每次 Lint 发现并修复不一致，让 Wiki 越来越健康

## 路径约定

```
WIKI_REPO=~/my-wiki                          # Git repo（GitHub private）
WIKI_GCS=$CC_PAGES_WEB_ROOT/wiki             # GCS serving 目录
WIKI_URL=$CC_PAGES_URL_PREFIX/wiki           # 公网 URL 前缀
```

## 目录结构

```
~/my-wiki/                       # GitHub private repo
├── wiki/                        # Wiki 知识页面（Bot 维护）
│   ├── index.html               # 总索引页（自动生成）
│   ├── search.html              # Pagefind 全文搜索页（自动生成）
│   ├── log.html                 # 操作日志（追加式）
│   ├── graph.html               # D3.js 知识图谱
│   ├── style.css                # 共享样式（GCP Console 调色板）
│   ├── wiki-shell.js            # 共享 nav/footer/快捷键（动态注入）
│   ├── local-graph.js           # 页面级关联图谱
│   ├── _pagefind/               # Pagefind 搜索索引（自动生成）
│   ├── sources/                 # 来源摘要
│   ├── entities/                # 实体页面（人/产品/项目）
│   ├── concepts/                # 概念页面（技术/方法/理论）
│   └── analyses/                # 分析和对比
├── raw/                         # 原始资料（不可变）
│   ├── articles/                # 网页文章
│   ├── papers/                  # 论文 PDF
│   ├── transcripts/             # 转录稿
│   └── notes/                   # 碎片笔记
└── wiki-data/                   # 元数据（非 HTML）
    ├── graph.json               # 页面关系图
    ├── log.json                 # 操作日志（机器可读）
    └── search-index.json        # 搜索索引
```

## 操作流程

### /wiki init — 首次初始化

```bash
# 1. 检查 repo 是否已存在
if [ -d ~/my-wiki ]; then
    cd ~/my-wiki && git pull
else
    # 首次：创建目录结构
    mkdir -p ~/my-wiki/{wiki/{sources,entities,concepts,analyses},raw/{articles,papers,transcripts,notes},wiki-data}
    cd ~/my-wiki && git init
    # 生成初始文件：index.html, log.html, graph.json, style.css
    # push 到 GitHub private repo
fi

# 2. 同步到 GCS
python3 ~/.claude/skills/wiki/scripts/sync-to-gcs.py
```

### /wiki ingest — 录入新资料

**推荐方式（Pipeline 自动化）：**

```bash
# PDF 论文
python3 ingest-pipeline.py pdf /path/to/paper.pdf --slug paper-name --title "Title" --tags "ml,training"

# URL 文章（Bot 先 WebFetch 获取内容）
python3 ingest-pipeline.py url --slug article-name --title "Title" --tags "tag1,tag2" --text "fetched content..."

# 纯文本
python3 ingest-pipeline.py text --slug note-name --title "Title" --tags "misc" --text "内容..."

# Bot 已手动创建页面后，只执行 rebuild+sync
python3 ingest-pipeline.py post-ingest --slug existing-slug --title "Title" --type source
```

Pipeline 自动完成：保存 raw → 生成骨架 source 页面 → rebuild index/graph/search → 追加 log → 同步 GCS。
Bot 只需关注：**填充 source 页面详细内容** + **识别和创建 entity/concept 页面**。

**手动步骤（Pipeline 不覆盖的部分）：**

1. **获取内容**：URL → WebFetch 抓取；PDF → `extract-pdf.py` 提取；文本 → 直接使用
2. Pipeline 自动保存 raw + 创建骨架 source 页面
3. **与用户讨论**：展示 3-5 个关键要点，确认重点方向
4. **填充 source 页面详细内容**：**必须包含详细结构化内容**（见下方 Source 页面内容要求）
5. **创建/更新 entity 和 concept 页面**（Bot LLM 判断）
6. Pipeline 已自动执行 rebuild + sync
7. 如果 Bot 额外修改了页面，再跑一次 `post-ingest`
8. **回复用户**：附上新页面的 URL

### Source 页面内容要求

Source 页面不是书签或链接集合，而是**编译后的知识页面**。用户打开 source 页面就能获取核心知识，不需要跳到外部原文。

**必须包含**（在 `<main>` 标签内）：
1. **原文链接**：保留指向原始资料的链接（CC Pages URL 或外部 URL）
2. **核心要点**（Key Takeaways）：3-7 个最重要的结论或发现，用编号列表
3. **详细内容**：根据原文类型选择合适的结构：
   - 技术文章 → 架构说明、关键参数、对比数据、代码示例
   - 论文 → 研究动机、方法、实验结果、局限性
   - 教程/指南 → 步骤摘要、关键配置、注意事项
   - 对比分析 → 对比表格、各方优劣、适用场景
4. **数据和表格**：原文中的关键数据（benchmark 数据、配置参数、性能指标）应该以表格形式保留
5. **与 Wiki 的关联**：通过 `wiki-link` 链接到相关的 entity/concept 页面

**不需要**：
- 逐字复制原文（这是摘要编译，不是转载）
- 原文的前言/致谢等非核心内容

### /wiki query — 基于 Wiki 提问

**步骤：**

1. **搜索引擎查询**：运行 `python3 ~/.claude/skills/wiki/scripts/wiki-query.py "{问题}" --top-k 5 --format json`
   - 返回 BM25 + 图谱增强的相关页面列表和匹配段落
   - 如果 search-chunks.json 不存在，先运行 `python3 build-search-index.py`
2. 深入阅读返回的 top-k 相关页面内容（用 Read 工具读取 HTML 文件）
3. 综合回答，引用具体页面 URL
4. **判断是否回存 Wiki**（好回答不应消失在聊天历史里）：
   - **应该回存**：对比分析、综合研究、新发现的关联、跨来源的综合结论、用户引导出的新洞察
   - **不需要回存**：简单事实查询、单页面信息复述、临时计算
   - 回存时：
     1. 生成新的 analysis 页面存入 `wiki/analyses/{slug}.html`（遵循 html-page-spec.md 模板）
     2. 更新相关 entity/concept 页面的 backlinks
     3. 运行 `rebuild-index.py` + `rebuild-graph.py`
     4. 运行 `bash ~/.claude/skills/wiki/scripts/rebuild-search.sh`（更新搜索索引）
     5. 运行 `python3 sync-to-gcs.py`
     6. 告知用户"这个分析已保存为 {url}"
   - **每次探索都在让 Wiki 变得更好，这就是知识复利**

### /wiki lint — 健康检查

Lint 不只是被动的健康检查，更是**主动的知识发现引擎**。

**健康检查（被动）：**

1. **矛盾检测**：扫描页面间的冲突观点
2. **孤儿页面**：`graph.json` 中入链数为 0 的页面
3. **过时信息**：新来源是否推翻了旧结论
4. **缺失页面**：多个页面提到但没有独立页面的概念
5. **断链**：HTML 中的 `wiki-link` 指向不存在的页面
6. **Backlinks 一致性**：实际引用关系 vs backlinks 段

**知识发现（主动）：**

7. **数据空白**：哪些领域的来源太少？建议用户去找什么新资料
8. **新问题**：基于现有知识，有哪些值得深入的问题还没被探索？
9. **潜在关联**：哪些页面看起来相关但还没有链接？
10. **综合机会**：是否可以从已有页面生成新的对比分析或综述？
11. **Schema 审视**：当前规则是否需要调整？有没有反复出现的模式应该变成约定？

**输出**：生成 lint 报告到 `wiki-data/lint-report.json`，分"问题"和"机会"两部分。问题确认后执行修复，机会作为后续 ingest/query 的建议。

**脚本调用**：
```bash
# 体检
python3 ~/.claude/skills/wiki/scripts/lint.py

# 自动修复
python3 ~/.claude/skills/wiki/scripts/fix-backlinks.py
python3 ~/.claude/skills/wiki/scripts/fix-broken-links.py

# 一键体检+修复+重建+同步
bash ~/.claude/skills/wiki/scripts/rebuild-all.sh --fix
```

### /wiki status — 统计信息

```bash
python3 ~/.claude/skills/wiki/scripts/status.py
```

显示：页面总数（按类型）、来源数、最近 5 次操作、图谱节点/边数、上次 lint 时间。

## 脚本清单

| 脚本 | 用途 | 调用时机 |
|------|------|---------|
| `init-wiki.sh` | 首次初始化 repo | `/wiki init` |
| `rebuild-index.py` | 重建 index.html | ingest 后 |
| `rebuild-graph.py` | 重建 graph.json + graph.html + backlinks | ingest 后 |
| `rebuild-search-page.py` | 生成 search.html | 修改搜索页时 |
| `rebuild-log.py` | 重建 log.html | rebuild-all 自动调用 |
| `rebuild-search.sh` | 构建 Pagefind 搜索索引 | ingest 后 |
| `rebuild-all.sh` | 一键 rebuild 全套 + lint + sync（12 步） | 批量操作后 |
| `sync-to-gcs.py` | 同步到 GCS | 每次操作后 |
| `lint.py` | 全量体检 | `/wiki lint` |
| `build-search-index.py` | 构建 BM25 搜索索引 (search-chunks.json) | rebuild-all 自动调用 |
| `wiki-query.py` | BM25+图谱增强查询引擎 | `/wiki query` |
| `update-manifest.py` | 更新编译清单 (compile-manifest.json) | rebuild-all 自动调用 |
| `rebuild-health.py` | 生成 health.html 健康看板 | rebuild-all 自动调用 |
| `graph-query.py` | 图谱遍历（BFS路径/邻居/社区/中心性） | 分析时 |
| `wiki-mcp-server.py` | MCP Server（多 Bot 共享查询） | Claude Code 自动启动 |
| `fix-backlinks.py` | 自动补全缺失 backlinks（legacy，rebuild-graph 已内建） | lint 发现问题后 |
| `fix-broken-links.py` | 修复 HTML wiki-link 断链 | lint 发现问题后 |
| `add-log-entry.py` | 追加 log.json 条目 | ingest/create 后 |
| `scan-uningested.py` | 扫描未录入的 CC Pages | lint 或手动扫描 |
| `status.py` | 显示 Wiki 统计（含健康分/manifest/query 历史） | `/wiki status` |
| `create-page.py` | 创建标准 entity/concept 页面 | ingest 新建页面 |
| `backfill-sources.py` | 批量注入 CC Pages 内容 | 批量录入 |
| `patch-all-pages.py` | 批量补全 pagefind/nav/graph | 升级功能后 |
| `extract-pdf.py` | PDF 文本提取（pymupdf4llm→markitdown→pdfminer→pypdf） | PDF ingest 时 |
| `ingest-pipeline.py` | 自动化 ingest 确定性步骤（保存raw/建骨架/rebuild/sync） | `/wiki ingest` |
| `knowledge-discovery.py` | 知识发现引擎（缺失概念/潜在关联/综合机会） | rebuild-all / lint |
| `wiki_utils.py` | 公共工具函数 | 被其他脚本引用 |

## Wiki MCP Server（多 Bot 共享查询）

Wiki 提供 MCP Server (`wiki-mcp-server.py`)，让所有 Bot 通过 Claude Code MCP 协议查询 Wiki。

**MCP Tools**：
| Tool | 用途 |
|------|------|
| `wiki_query` | BM25 + 图谱增强搜索 |
| `wiki_page` | 读取页面纯文本 |
| `wiki_graph_neighbors` | N-hop 邻居 |
| `wiki_graph_path` | 两节点最短路径 |
| `wiki_status` | 统计信息 |
| `wiki_search` | 关键词搜索 |
| `wiki_list` | 按类型/标签列表 |

**查询优先级**：MCP tools 可用时优先用 MCP，否则回退到脚本调用。

**配置**（`~/.claude.json`）：
```json
{
  "mcpServers": {
    "wiki": {
      "command": "python3",
      "args": ["~/.claude/skills/wiki/scripts/wiki-mcp-server.py"]
    }
  }
}
```

## 共享组件

Nav、footer、全局快捷键由 `wiki-shell.js` 统一管理，**不要硬编码在页面 HTML 里**。

| 组件 | 管理方式 | 修改位置 |
|------|---------|---------|
| 导航栏（5 个 tab） | `wiki-shell.js` document.write 同步注入 | `wiki/wiki-shell.js` |
| 页脚 | `wiki-shell.js` DOMContentLoaded 注入 | `wiki/wiki-shell.js` |
| Ctrl+K 搜索快捷键 | `wiki-shell.js` 全局键盘监听 | `wiki/wiki-shell.js` |
| 样式 | `wiki/style.css`（GCP Console 调色板） | `wiki/style.css` |
| 页面级关联图谱 | `wiki/local-graph.js` + D3.js | `wiki/local-graph.js` |

**页面模板只需**：`<script src="[prefix]wiki-shell.js"></script>` 放在 `<body>` 开头。
生成器脚本（create-page.py、rebuild-*.py）不输出 nav/footer HTML。

## 页面 HTML 规范

> 📄 详见 `references/html-page-spec.md` — 包含 meta 标签、HTML 结构模板、交叉引用约定、特殊标注（callouts）

**要点速记**（生成页面时查阅完整规范）：
- 每页必须有 `wiki-type`、`wiki-tags`、`wiki-created`、`wiki-updated`、`wiki-links-to` meta 标签
- 使用 `class="wiki-link"` 做内部链接，`class="source-ref"` 引用原始资料
- 矛盾用 `wiki-warning`、待验证用 `wiki-question`、过时用 `wiki-outdated` callout
- 每页底部要有 backlinks 和参考来源两个 section

## graph.json 格式

> 📄 详见 `references/graph-json-spec.md` — 包含完整 JSON schema 和字段说明

**要点速记**：`meta`（统计）+ `nodes`（id/title/type/path/tags/summary）+ `links`（source/target/type）

## 同步机制

### Repo → GCS

每次操作后自动同步：

```bash
python3 ~/.claude/skills/wiki/scripts/sync-to-gcs.py
```

同步范围：`wiki/` 和 `wiki-data/` 目录。`raw/` 不同步到 GCS（原始资料可能很大，且不需要通过 URL 访问）。

### 多机器同步

```bash
# 新机器首次
git clone git@github.com:yangwhale/my-wiki.git ~/my-wiki

# 已有机器拉取
cd ~/my-wiki && git pull
```

## 质量规则

1. **一个概念一个页面**：不要在一个页面里混合多个无关概念
2. **优先更新已有页面**：避免重复创建。先搜索 `graph.json` 确认不存在再新建
3. **矛盾必须标注**：发现矛盾用 `wiki-warning` callout，不要静默覆盖
4. **来源必须引用**：每个事实性陈述都应标注来源
5. **Backlinks 必须同步**：更新页面 A 引用了页面 B 时，同时更新 B 的 backlinks
6. **Slug 命名**：kebab-case，简短有意义。如 `tpu-v7`、`knowledge-compounding`
7. **不要修改 raw/**：原始资料只增不改
8. **每次操作都追加 log**：保持完整的操作时间线
