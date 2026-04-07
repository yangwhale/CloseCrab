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
│   ├── overview.html            # 全局综述
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

# 3. 创建 symlink（如果没有）
ln -sfn ~/my-wiki/wiki $CC_PAGES_WEB_ROOT/wiki 2>/dev/null || rsync
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
7. **追加日志**：在 `log.html` 追加一条记录
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
5. **如果回答有持久价值**（对比分析、综合研究等）：
   - 生成新的 analysis 页面存入 `wiki/analyses/`
   - 更新索引和图谱
   - 告知用户"这个分析已保存为 {url}"

### /wiki lint — 健康检查

**检查项：**

1. **矛盾检测**：扫描页面间的冲突观点
2. **孤儿页面**：`graph.json` 中入链数为 0 的页面
3. **过时信息**：新来源是否推翻了旧结论
4. **缺失页面**：多个页面提到但没有独立页面的概念
5. **断链**：HTML 中的 `wiki-link` 指向不存在的页面
6. **Backlinks 一致性**：实际引用关系 vs backlinks 段
7. **建议**：推荐去找什么新资料

**输出**：生成 lint 报告，列出问题和建议修复方案。确认后执行修复。

### /wiki status — 统计信息

显示：页面总数（按类型）、来源数、最近 5 次操作、图谱节点/边数、上次 lint 时间。

## 页面 HTML 规范

### Meta 标签（必须）

每个 Wiki 页面的 `<head>` 必须包含：

```html
<meta name="wiki-type" content="concept">           <!-- source|entity|concept|analysis -->
<meta name="wiki-tags" content="ai,knowledge,llm">  <!-- 逗号分隔标签 -->
<meta name="wiki-created" content="2026-04-07">
<meta name="wiki-updated" content="2026-04-07">
<meta name="wiki-sources" content="3">               <!-- 引用来源数 -->
<meta name="wiki-links-to" content="rag,memex">      <!-- 出链页面 slug -->
```

### HTML 结构

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{页面标题} — CC Wiki</title>
  <!-- wiki-* meta tags -->
  <!-- OG tags for sharing -->
  <link rel="stylesheet" href="../style.css">
</head>
<body>
  <nav class="wiki-nav">
    <a href="../index.html">Index</a>
    <a href="../graph.html">Graph</a>
    <a href="../log.html">Log</a>
  </nav>

  <article class="wiki-content">
    <header>
      <div class="wiki-meta">
        <span class="wiki-type">{type}</span>
        <span class="wiki-date">Created: {date} · Updated: {date}</span>
      </div>
      <h1>{标题}</h1>
      <p class="wiki-summary">{一行摘要}</p>
      <div class="wiki-tags">{tags}</div>
    </header>

    <main>
      <!-- 页面主体内容 -->
    </main>

    <section class="wiki-backlinks">
      <h3>引用了此页面的页面</h3>
      <ul>
        <li><a href="...">{title}</a></li>
      </ul>
    </section>

    <section class="wiki-sources-list">
      <h3>参考来源</h3>
      <ul>
        <li><a href="...">{source title}</a></li>
      </ul>
    </section>
  </article>

  <footer class="wiki-footer">
    CC Wiki · Maintained by CloseCrab Bot
  </footer>
</body>
</html>
```

### 交叉引用

```html
<!-- Wiki 内部链接 -->
<a href="../concepts/rag.html" class="wiki-link">RAG</a>

<!-- 原始资料引用 -->
<a href="../../raw/articles/xxx.html" class="source-ref">[来源]</a>

<!-- 外部链接 -->
<a href="https://..." target="_blank" rel="noopener">外部链接</a>
```

### 特殊标注

```html
<!-- 矛盾标注 -->
<div class="wiki-callout wiki-warning">
  <strong>矛盾</strong>：此观点与 <a href="...">xxx</a> 中的结论冲突。
</div>

<!-- 不确定标注 -->
<div class="wiki-callout wiki-question">
  <strong>待验证</strong>：此数据来源单一，需要更多佐证。
</div>

<!-- 过时标注 -->
<div class="wiki-callout wiki-outdated">
  <strong>可能过时</strong>：较新的来源 <a href="...">xxx</a> 提供了更新数据。
</div>
```

## graph.json 格式

```json
{
  "meta": {
    "updated": "2026-04-07T10:30:00Z",
    "node_count": 42,
    "link_count": 128
  },
  "nodes": [
    {
      "id": "rag",
      "title": "RAG (Retrieval-Augmented Generation)",
      "type": "concept",
      "path": "concepts/rag.html",
      "tags": ["ai", "retrieval", "llm"],
      "summary": "基于检索增强的生成方法...",
      "created": "2026-04-07",
      "updated": "2026-04-07",
      "source_count": 3
    }
  ],
  "links": [
    {
      "source": "karpathy-llm-wiki",
      "target": "rag",
      "type": "mentions"
    }
  ]
}
```

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
