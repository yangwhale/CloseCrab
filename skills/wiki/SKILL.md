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
│   ├── log.html                 # 操作日志（追加式）
│   ├── graph.html               # D3.js 知识图谱
│   ├── style.css                # 共享样式
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

**步骤：**

1. **获取内容**：URL → WebFetch 抓取；文件 → 直接读取；文本 → 直接使用
2. **保存原始资料**：存到 `raw/` 对应子目录，文件名用 kebab-case + 日期
   - 文章: `raw/articles/karpathy-llm-wiki-20260407.html`
   - 论文: `raw/papers/attention-is-all-you-need.pdf`
3. **与用户讨论**：展示 3-5 个关键要点，确认重点方向
4. **生成/更新 Wiki 页面**：
   - 新建 source 摘要页 `wiki/sources/{slug}.html`
   - 新建或更新相关 entity 页面
   - 新建或更新相关 concept 页面
   - 更新所有受影响页面的 backlinks
5. **更新索引**：运行 `rebuild-index.py` 重建 `index.html`
6. **更新图谱**：运行 `rebuild-graph.py` 重建 `graph.json`
7. **追加日志**：在 `log.html` 追加一条记录，同时追加 `wiki-data/log.json`（格式见 `references/log-json-spec.md`）
8. **同步**：
   - `git add -A && git commit -m "ingest: {title}"` 
   - `python3 sync-to-gcs.py`
   - （定期或手动 `git push`）
9. **回复用户**：附上新页面的 URL

### /wiki query — 基于 Wiki 提问

**步骤：**

1. 读取 `wiki-data/graph.json` 了解 Wiki 全局结构
2. 根据问题定位相关页面
3. 深入阅读相关页面内容
4. 综合回答，引用具体页面 URL
5. **判断是否回存 Wiki**（好回答不应消失在聊天历史里）：
   - **应该回存**：对比分析、综合研究、新发现的关联、跨来源的综合结论、用户引导出的新洞察
   - **不需要回存**：简单事实查询、单页面信息复述、临时计算
   - 回存时生成新的 analysis 页面存入 `wiki/analyses/`
   - 更新索引和图谱
   - 告知用户"这个分析已保存为 {url}"
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

**输出**：生成 lint 报告，分"问题"和"机会"两部分。问题确认后执行修复，机会作为后续 ingest/query 的建议。

### /wiki status — 统计信息

显示：页面总数（按类型）、来源数、最近 5 次操作、图谱节点/边数、上次 lint 时间。

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
