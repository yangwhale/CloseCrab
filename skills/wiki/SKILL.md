---
name: wiki
description: 个人知识 Wiki 管理（Quartz v2）。录入资料、查询知识、健康检查。当用户说"录入wiki"、"wiki ingest"、"加到wiki"、"wiki查一下"、"wiki lint"、"/wiki"等关键词时触发。
---

# CC Wiki v2 — 个人知识 Wiki 管理

基于 Quartz (Static Site Generator) + Markdown + BM25 搜索引擎 + MCP Server 的 LLM 维护知识 Wiki。灵感来自 Karpathy 的 LLM Wiki 模式。

**站点**: https://cc.higcp.com/wiki-v2/
**完整技术文档**: `~/CloseCrab/docs/wiki-v2.md`

## 触发条件

- `/wiki ingest <url|文件|文本>` — 录入新资料
- `/wiki query <问题>` — 基于 Wiki 回答问题
- `/wiki lint` — 健康检查
- `/wiki status` — 显示 Wiki 统计 + 知识覆盖度
- 自然语言："帮我录入到 wiki"、"加到知识库"、"wiki 里有没有..."

## 第一性原理（来自 Karpathy LLM Wiki）

### 角色分工

**人类的职责**：策展来源、引导分析方向、提出好问题、思考这一切意味着什么。
**LLM 的职责**：其他所有事——总结、交叉引用、归档、簿记。

### 知识编译，而非检索

不要每次从原始文档重新检索（RAG 模式）。而是将知识**编译一次，持续更新**。Wiki 是编译后的产物——交叉引用已经建好，矛盾已经标记，综合分析已经反映了所有已读内容。

### Schema 共同进化

SKILL.md 是**活文档**，随使用不断迭代。Bot 在操作中发现规则不够用时，应主动建议修改。

### 参与度由用户决定

- **深度参与**（默认）：逐个录入，边录边讨论要点
- **批量模式**：一次性投放多个来源，LLM 自主处理

## 路径约定

```
WIKI_REPO=~/my-wiki-v2                         # Quartz repo
WIKI_CONTENT=~/my-wiki-v2/content              # Markdown 源文件
WIKI_RAW=~/my-wiki-v2/raw                      # 原始资料（不可变）
WIKI_URL=https://cc.higcp.com/wiki-v2
```

## 目录结构

```
~/my-wiki-v2/
├── content/                    # Markdown 源文件（Bot 维护）
│   ├── index.md                # 首页
│   ├── sources/                # 来源摘要
│   ├── entities/               # 实体页面（人/产品/项目/硬件）
│   ├── concepts/               # 概念页面（技术/方法/理论）
│   └── analyses/               # 分析对比
├── raw/                        # 原始资料（不可变）
│   ├── articles/               # 网页文章
│   ├── papers/                 # 论文 PDF
│   └── notes/                  # 碎片笔记
├── scripts/                    # 工具脚本
│   ├── wiki_utils.py           # 常量、frontmatter 解析、wikilink 提取
│   ├── query.py                # BM25 搜索引擎（倒排索引 + LRU 缓存）
│   ├── wiki-mcp-server.py      # MCP Server（9 个 tools）
│   ├── ingest.py               # 录入管道（保存 raw + 创建骨架）
│   ├── lint.py                 # 健康检查（断链/孤儿/frontmatter）
│   ├── status.py               # 统计信息 + 知识覆盖度评分
│   ├── benchmark.py            # 搜索质量基准测试（P@1, P@3, MRR）
│   ├── build-and-sync.sh       # Quartz 构建 + GCS 同步
│   ├── synonyms.json           # 同义词表（30+ AI/ML 术语）
│   └── test_queries.json       # 30 个标准测试查询
├── quartz.config.ts            # Quartz 配置
└── quartz.layout.ts            # Quartz 布局
```

## Markdown 页面模板

```markdown
---
title: "标题"
description: "一句话摘要（用于搜索结果展示）"
type: source
date: 2026-04-12
tags:
  - tag1
  - tag2
aliases:
  - 别名1
---

## 原文

[原文链接](https://example.com) · 2026-04-12

## 核心要点

1. **要点一**：说明
2. **要点二**：说明

## 详细内容

使用 [[wikilinks]] 链接到其他页面。
表格、列表、代码块都用标准 Markdown。
```

**Frontmatter 必填字段**: title, type, date, tags
**可选字段**: description, aliases, deprecated, lastmod
**页面类型**: source（来源摘要）, entity（实体）, concept（概念）, analysis（分析）
**内部链接**: 用 `[[slug]]` 或 `[[slug|显示文本]]` wikilink 语法

## Quartz 内建功能（不需要脚本）

Quartz 已内建以下功能，**不需要手动维护**：
- **Graph 知识图谱**: 自动从 `[[wikilinks]]` 生成
- **Backlinks 反向链接**: 每页底部自动显示
- **FlexSearch 全文搜索**: 构建时自动生成索引
- **目录索引 (FolderPage)**: 自动生成目录页
- **Tag 页面**: 自动聚合同标签页面
- **ToC 目录**: 每页自动生成
- **KaTeX 数学**: 支持 `$inline$` 和 `$$block$$`

## 搜索引擎能力（query.py）

query.py 是一个高性能搜索引擎，核心特性：

| 特性 | 说明 |
|------|------|
| **倒排索引** | `{term → [(slug, field, count)]}` 预构建，O(1) 查找 |
| **LRU 缓存** | 热查询 <0.01ms（128 条缓存） |
| **BM25 评分** | k1=1.5, b=0.75, IDF 加权，field 权重（title×10, tags×5, desc×3, body×1） |
| **中文分词** | jieba 精确模式，CJK/ASCII 自动切换 |
| **同义词扩展** | `synonyms.json` 双向映射（tpu↔ironwood/trillium 等） |
| **模糊匹配** | Levenshtein distance ≤ 2 自动纠错 |
| **Tag 共现扩展** | 搜 `tpu` 自动带 `tpu-v7`（基于共现矩阵） |
| **意图分类** | slug/title 精确查找直接返回（0.05ms） |
| **时间衰减** | 180 天半衰期，新页面优先 |
| **图谱加权** | wikilink hub 页面加权 |
| **Entity bonus** | entity/concept 精确匹配 +25 |
| **多 Snippet** | 最多 3 个不重叠片段, ±80 chars, 高亮匹配 |
| **摘要预生成** | description 或正文前 300 字 |

**性能**: 冷查询 1-67ms, 热查询 <0.01ms, P@1=88.9%, P@3=94.7%, MRR=0.851

## 操作流程

### /wiki ingest — 录入新资料

**步骤：**

1. **获取内容**: URL → WebFetch 抓取；PDF → 读取；文本 → 直接使用
2. **创建骨架页面**:
   ```bash
   python3 ~/my-wiki-v2/scripts/ingest.py url \
     --slug article-name --title "Title" --tags "tag1,tag2" \
     --source-url "https://..." --text "fetched content..."
   ```
3. **与用户讨论**: 展示 3-5 个关键要点，确认重点方向
4. **填充详细内容**: 用 Edit 工具填充骨架页面（见下方"内容要求"）
5. **创建/更新 entity 和 concept 页面**（Bot LLM 判断需要时）
6. **构建部署**:
   ```bash
   bash ~/my-wiki-v2/scripts/build-and-sync.sh
   ```
7. **回复用户**: 附上新页面 URL `https://cc.higcp.com/wiki-v2/sources/slug`

**Slug 命名**: kebab-case + 日期后缀，如 `tpu-v7-specs-20260412`

### Source 页面内容要求

Source 页面是**编译后的知识页面**，用户打开就能获取核心知识，不需要跳到外部原文。

**必须包含**：
1. **原文链接**: 保留指向原始资料的链接
2. **核心要点** (Key Takeaways): 3-7 个最重要的结论，用编号列表
3. **详细内容**: 根据原文类型选择合适的结构化内容
4. **数据和表格**: 关键数据以表格形式保留
5. **Wiki 关联**: 通过 `[[wikilinks]]` 链接到相关页面

### /wiki query — 基于 Wiki 提问

**步骤：**

1. **搜索**（优先用 MCP tools）:
   ```bash
   # MCP（推荐）
   wiki_query("搜索关键词")      # BM25 全文搜索
   wiki_ask("具体问题")          # RAG 式问答，直接提取答案段落
   wiki_search("type:source tag:tpu")  # 结构化过滤
   
   # CLI 回退
   python3 ~/my-wiki-v2/scripts/query.py "搜索关键词" --top-k 5
   ```
2. 深入阅读返回的相关页面（用 `wiki_page(slug)` 或 Read 工具）
3. 发现相关页面：`wiki_related(slug)` 获取推荐
4. 综合回答，引用具体页面 URL
5. **判断是否回存 Wiki**:
   - **应该回存**: 对比分析、综合研究、新发现的关联
   - **不需要**: 简单事实查询、单页面信息复述
   - 回存时创建 `analyses/{slug}.md`，然后运行 `build-and-sync.sh`

### /wiki lint — 健康检查

```bash
python3 ~/my-wiki-v2/scripts/lint.py
```

检查项：
- **断链**: `[[slug]]` 引用的 slug 没有对应 .md 文件
- **孤儿页面**: 没有任何页面 `[[引用]]` 的页面
- **缺失 frontmatter**: title/type/date/tags 缺失
- **内容过短**: 正文 < 100 字
- **标签不一致**: 相似标签未统一

**知识发现**（Lint 时顺带思考）：
- 哪些领域来源太少？建议用户补充
- 哪些页面相关但没有链接？
- 是否可以生成新的对比分析？

### /wiki status — 统计信息 + 知识覆盖度

```bash
python3 ~/my-wiki-v2/scripts/status.py
```

显示：页面总数（按类型）、标签分布、最近变更、知识覆盖度评分（connectivity × 40 + freshness × 30 + tag diversity × 30）、orphan 数量、平均 wikilinks 数。

## 构建部署

```bash
# 一键构建 + 同步
bash ~/my-wiki-v2/scripts/build-and-sync.sh
```

等价于：
```bash
cd ~/my-wiki-v2
npx quartz build
gsutil -m rsync -r -d public/ gs://chris-pgp-host-asia/cc-pages/wiki-v2/
```

**注意**: 构建前会自动删除 `~/package.json`（如果是空文件），避免干扰 Quartz。

## MCP Server（9 个 Tools）

MCP Server 路径：`~/my-wiki-v2/scripts/wiki-mcp-server.py`
配置在 `~/.claude.json` 的 `mcpServers.wiki` 中。基于 fastmcp 框架，所有 tools 用 `@_safe_tool` 包裹防崩溃。

| Tool | 用途 | 适用场景 |
|------|------|---------|
| `wiki_query(question, top_k=5)` | BM25 全文搜索，返回排名结果 + snippets | 主要搜索入口 |
| `wiki_page(slug)` | 读取页面全文 | snippet 不够时深入阅读 |
| `wiki_ask(question)` | RAG 式问答，提取最相关段落 | 直接问题（"TPU v7 HBM 多大？"） |
| `wiki_related(slug, top_k=5)` | 图+tag+类型推荐相关页面 | "看完这个还应该看什么？" |
| `wiki_search(keyword)` | 快速关键词/结构化搜索 | `type:source tag:tpu /regex/` |
| `wiki_list(type="", tag="")` | 按类型和/或标签列出页面 | 浏览类操作 |
| `wiki_graph_neighbors(slug, depth=1)` | N-hop wikilink 邻居 | 知识图谱探索 |
| `wiki_graph_path(source, target)` | 两页面最短路径（BFS） | 发现知识关联 |
| `wiki_status()` | 统计 + 知识覆盖度报告 | 健康总览 |

**MCP tools 可用时优先用 MCP，否则回退到脚本调用。**

### wiki_search 结构化语法

```
type:source tag:tpu           # 按类型+标签过滤
/v[67]/                       # 正则匹配标题
type:entity /karp/            # 组合查询
```

## 质量规则

1. **一个概念一个页面**: 不要在一个页面里混合多个无关概念
2. **优先更新已有页面**: 先用 wiki_query 确认不存在再新建
3. **矛盾必须标注**: 发现矛盾用 `> [!warning]` callout
4. **来源必须引用**: 每个事实性陈述都应标注来源
5. **Slug 命名**: kebab-case，简短有意义，如 `tpu-v7`、`knowledge-compounding`
6. **不要修改 raw/**: 原始资料只增不改
7. **Wikilinks 链接**: 提到已有页面的概念/实体时，用 `[[slug]]` 链接

## 脚本清单

| 脚本 | 用途 | 调用时机 |
|------|------|---------|
| `wiki_utils.py` | 常量、frontmatter 解析、slug 查找、wikilink 提取 | 被其他脚本 import |
| `query.py` | BM25 搜索引擎 + 倒排索引 + LRU 缓存 | MCP server / CLI |
| `wiki-mcp-server.py` | MCP Server, 9 个 tools | Claude Code MCP |
| `ingest.py` | 录入管道: 保存 raw + 创建骨架 | `/wiki ingest` |
| `lint.py` | 健康检查: 断链、孤儿、frontmatter | `/wiki lint` |
| `status.py` | 统计 + 知识覆盖度评分 | `/wiki status` |
| `benchmark.py` | 搜索质量基准测试（P@1, P@3, MRR） | 调参验证 |
| `build-and-sync.sh` | Quartz 构建 + GCS 同步 | 每次内容变更 |
| `synonyms.json` | 同义词表（30+ AI/ML 术语） | query.py 加载 |
| `test_queries.json` | 30 条标准测试查询 | benchmark.py |
