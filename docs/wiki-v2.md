# CC Wiki v2 — AI/ML 知识 Wiki 完整指南

LLM 维护的个人知识 Wiki，基于 Quartz + Markdown + BM25 搜索引擎 + MCP Server。

**站点**: `$WIKI_URL`（由环境变量配置）

---

## 1. 设计理念

### 知识编译，不是检索

传统 RAG（Retrieval-Augmented Generation）每次从原始文档检索。CC Wiki 的理念不同：**编译一次，持续更新**。Wiki 是编译后的产物——交叉引用已建好，矛盾已标记，综合分析已反映所有已读内容。

灵感来自 Karpathy 的 LLM Wiki 模式。

### 角色分工

| 角色 | 职责 |
|------|------|
| **人类** | 策展来源、引导分析方向、提出好问题、思考意义 |
| **LLM** | 总结、交叉引用、归档、簿记、健康检查 |

### Schema 共同进化

SKILL.md 和 Wiki 规则是活文档。Bot 在操作中发现不够用时，主动建议迭代。

### 自主使用（第二大脑）

Bot 不需要用户显式触发 `/wiki` 就能利用 Wiki：
- 回答知识性问题前，自动搜索 Wiki
- 发现有价值的对话内容，主动建议录入
- 在 system prompt 中注入 Wiki 存在感

---

## 2. 系统架构

```
用户                     Bot (Claude Code)              Wiki v2
 │                          │                            │
 │── "TPU v7 性能?" ──────►│                            │
 │                          │── wiki_query("TPU v7") ──►│ MCP Server
 │                          │◄── BM25 结果 + snippets ──│   (fastmcp)
 │                          │                            │
 │                          │── wiki_page("tpu-v7") ───►│ 读 Markdown
 │                          │◄── 全文 ─────────────────│
 │                          │                            │
 │◄── 综合回答 ──────────│                            │
 │                          │                            │
 │── "录入这篇文章" ──────►│                            │
 │                          │── ingest.py ─────────────►│ 保存 raw/
 │                          │── Edit 工具 ─────────────►│ 写 content/
 │                          │── build-and-sync.sh ─────►│ Quartz 构建
 │                          │                            │── gsutil → GCS
 │◄── 新页面 URL ────────│                            │
```

### 技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| 静态站生成 | Quartz v4 | Hugo-like, 原生支持 wikilinks + 知识图谱 |
| 内容格式 | Markdown + YAML frontmatter | 纯文本, git 友好 |
| 搜索引擎 | 自研 BM25 + 倒排索引 | jieba 中文分词, 同义词扩展, 模糊匹配 |
| MCP Server | fastmcp (Python) | 9 个 tools, 通过 Claude Code MCP 协议暴露 |
| 部署 | GCS + gcsfuse 反代 | `gs://$GCS_BUCKET/cc-pages/wiki-v2/` |
| 版本控制 | Git (`~/my-wiki-v2/`) | 内容和脚本都在同一 repo |

---

## 3. 目录结构

```
~/my-wiki-v2/
├── content/                    # Markdown 源文件（Bot 维护）
│   ├── index.md                # 首页
│   ├── sources/                # 来源摘要（文章/论文/报告总结）
│   ├── entities/               # 实体页面（人/产品/项目/硬件）
│   ├── concepts/               # 概念页面（技术/方法/理论）
│   └── analyses/               # 分析对比
├── raw/                        # 原始资料（只增不改）
│   ├── articles/               # 网页文章原文
│   ├── papers/                 # 论文 PDF
│   └── notes/                  # 碎片笔记
├── scripts/                    # 工具脚本（搜索引擎 + MCP + 构建）
│   ├── wiki_utils.py           # 常量、frontmatter 解析、wikilink 提取
│   ├── query.py                # BM25 搜索引擎（倒排索引 + LRU 缓存）
│   ├── wiki-mcp-server.py      # MCP Server（9 个 tools）
│   ├── ingest.py               # 录入管道（保存 raw + 创建骨架）
│   ├── lint.py                 # 健康检查（断链/孤儿/frontmatter）
│   ├── status.py               # 统计信息 + 知识覆盖度评分
│   ├── benchmark.py            # 搜索质量基准测试
│   ├── convert-html-to-md.py   # HTML → Markdown 批量转换
│   ├── build-and-sync.sh       # Quartz 构建 + GCS 同步
│   ├── synonyms.json           # 同义词表（30+ AI/ML 术语）
│   └── test_queries.json       # 30 个标准测试查询
├── quartz.config.ts            # Quartz 配置（zh-CN, KaTeX, wikilinks）
└── quartz.layout.ts            # 布局（图谱、搜索、目录、反向链接）
```

### 页面类型

| 类型 | 目录 | 说明 | 命名规则 |
|------|------|------|---------|
| `source` | `sources/` | 文章/论文的编译摘要 | `slug-YYYYMMDD.md` |
| `entity` | `entities/` | 人、产品、项目、硬件 | `slug.md`（无日期） |
| `concept` | `concepts/` | 技术概念、方法、理论 | `slug.md`（无日期） |
| `analysis` | `analyses/` | 对比分析、综合研究 | `slug-YYYYMMDD.md` |

---

## 4. 搜索引擎（query.py）

### 架构

```
查询 → 分词(jieba) → 同义词扩展 → 模糊匹配
                                      ↓
                              倒排索引查候选
                                      ↓
                              BM25 评分 + field 权重
                                      ↓
                         时间衰减 + 图谱加权 + entity bonus
                                      ↓
                              多 snippet 提取 → 结果
```

### 核心特性

| 特性 | 实现 | 效果 |
|------|------|------|
| **倒排索引** | `{term → [(slug, field, count)]}` 预构建 | O(1) 查找替代 O(n) 全扫描 |
| **LRU 缓存** | `@lru_cache(maxsize=128)` | 热查询 <0.01ms |
| **中文分词** | jieba 精确模式 | "TPU v7 性能" → `['tpu', 'v7', '性能']` |
| **BM25 评分** | k1=1.5, b=0.75, IDF 加权 | 常见词自动降权 |
| **同义词扩展** | `synonyms.json` 双向映射 | tpu ↔ ironwood/trillium |
| **模糊匹配** | Levenshtein distance ≤ 2 | `karpthy` → `karpathy` |
| **Tag 共现** | 共现矩阵自动扩展 | 搜 `tpu` 自动带 `tpu-v7` |
| **意图分类** | slug/title/search 三类 | `tpu-v7` 直接返回，0.05ms |
| **时间衰减** | 半衰期 180 天 | 新页面优先 |
| **图谱加权** | wikilink 连接度 | hub 页面加权 |
| **Entity bonus** | entity/concept 精确匹配 +25 | "TPU v7" → entity 页优先 |
| **摘要预生成** | description 或正文前 300 字 | 无需读全文 |
| **多 Snippet** | 最多 3 个不重叠片段, ±80 chars | 高亮匹配 term |

### 性能指标

| 指标 | 优化前 | 优化后 | 提升 |
|------|--------|--------|------|
| 冷查询 | 163ms | 1-67ms | 2-160x |
| 热查询（LRU） | 163ms | <0.01ms | 16000x |
| Slug 精确查找 | 163ms | 0.05ms | 3000x |
| P@1 | - | 88.9% | - |
| P@3 | - | 94.7% | - |
| MRR | - | 0.851 | - |
| 索引构建 | - | ~3.8s（一次性） | 60s TTL |

### CLI 用法

```bash
# 基本搜索
python3 ~/my-wiki-v2/scripts/query.py "TPU v7 性能" --top-k 5

# 按类型过滤
python3 ~/my-wiki-v2/scripts/query.py "training" --type source

# 按标签过滤
python3 ~/my-wiki-v2/scripts/query.py "benchmark" --tag tpu-v7

# JSON 输出
python3 ~/my-wiki-v2/scripts/query.py "推理优化" --format json
```

---

## 5. MCP Server（wiki-mcp-server.py）

MCP Server 是 Bot 与 Wiki 交互的主要接口。基于 fastmcp 框架，通过 stdio 传输，9 个 tools。

### 配置

`~/.claude.json`:
```json
{
  "mcpServers": {
    "wiki": {
      "type": "stdio",
      "command": "python3",
      "args": ["/path/to/my-wiki-v2/scripts/wiki-mcp-server.py"]
    }
  }
}
```

### 9 个 MCP Tools

#### wiki_query(question, top_k=5)
**用途**: 搜索 Wiki 页面。回答任何知识性问题时首选。

返回 JSON：
```json
{
  "query": "TPU v7",
  "results": [
    {
      "title": "TPU v7 (Ironwood)",
      "type": "entity",
      "score": 45.12,
      "summary": "Google 最新一代 TPU...",
      "snippets": ["...匹配上下文..."],
      "matched_terms": ["tpu", "v7"],
      "related_pages": [{"slug": "tpu-v6e", "title": "..."}],
      "url": "$WIKI_URL/entities/tpu-v7"
    }
  ],
  "query_time_ms": 12.3
}
```

#### wiki_page(slug)
**用途**: 读取页面全文。当 wiki_query 的 snippet 不够时使用。

#### wiki_ask(question)
**用途**: RAG 式问答。搜索 top-3 页面，提取最相关段落，返回带来源的答案。

适用于直接问题："TPU v7 HBM 容量是多少？"

#### wiki_related(slug, top_k=5)
**用途**: 发现相关页面。基于 wikilink 图邻居 + tag 交集 + 类型匹配。

适用于："看完 tpu-v7 还应该看什么？"

#### wiki_search(keyword)
**用途**: 快速关键词搜索（标题/slug/tag 匹配）。比 wiki_query 快，适合简单查找。

支持结构化查询：
```
type:source tag:tpu           # 按类型+标签过滤
/v[67]/                       # 正则匹配标题
type:entity /karp/            # 组合
```

#### wiki_list(type="", tag="")
**用途**: 按类型和/或标签列出页面。

#### wiki_graph_neighbors(slug, depth=1)
**用途**: 获取 N-hop wikilink 邻居。

#### wiki_graph_path(source, target)
**用途**: 两个页面之间的最短路径（BFS on wikilink graph）。

#### wiki_status()
**用途**: Wiki 统计 + 知识覆盖度报告。

返回：
```json
{
  "total_pages": 221,
  "by_type": {"source": 134, "concept": 49, "entity": 36, "analysis": 1},
  "knowledge_coverage": {
    "score": 95.8,
    "connectivity": 89.6,
    "orphan_count": 23,
    "avg_links_per_page": 4.4,
    "recent_30d_pages": 161,
    "deprecated_count": 1
  }
}
```

### 缓存机制

- **元数据 + 邻接表**: 60s TTL, 增量更新（mtime 检测，无变化 <5ms）
- **查询结果**: LRU cache 128 条，索引更新时自动清空
- **索引**: 倒排索引随 TTL 一起刷新

### 错误处理

所有 tool 函数用 `@_safe_tool` 装饰器包裹，异常返回 JSON error 而不是 crash。输入验证：空查询、超长查询（截断 500 字）、特殊字符都安全处理。

---

## 6. 部署

### 前提条件

```bash
# Python 依赖
pip install jieba pyyaml mcp[server]    # 或 pip install fastmcp

# Node.js（Quartz 构建需要）
node --version  # >= 18

# Quartz 安装（首次）
cd ~/my-wiki-v2
npm install
```

### 首次部署

```bash
# 1. Clone wiki repo
git clone <repo-url> ~/my-wiki-v2

# 2. 配置 MCP Server
# 在 ~/.claude.json 的 mcpServers 中添加：
{
  "wiki": {
    "type": "stdio",
    "command": "python3",
    "args": ["/home/user/my-wiki-v2/scripts/wiki-mcp-server.py"]
  }
}

# 3. 验证
python3 ~/my-wiki-v2/scripts/query.py "test" --format json
python3 ~/my-wiki-v2/scripts/status.py

# 4. 首次构建
bash ~/my-wiki-v2/scripts/build-and-sync.sh
```

### 日常构建

```bash
# 修改内容后
bash ~/my-wiki-v2/scripts/build-and-sync.sh
```

等价于：
```bash
cd ~/my-wiki-v2 && npx quartz build
gsutil -m rsync -r -d public/ gs://$GCS_BUCKET/cc-pages/wiki-v2/
```

---

## 7. 日常使用

### 录入新资料

```
/wiki ingest <URL 或文本>
```

流程：
1. 获取内容（WebFetch / Read）
2. `ingest.py` 保存 raw + 创建骨架
3. 与用户讨论要点
4. 填充详细内容
5. 创建/更新 entity 和 concept 页面
6. `build-and-sync.sh` 部署

### 查询知识

```
/wiki query <问题>
```

或直接提问（Bot 自动查 Wiki）。

### 健康检查

```
/wiki lint
```

检查断链、孤儿页面、缺失 frontmatter、内容过短、标签不一致。

### 统计信息

```
/wiki status
```

### Benchmark

```bash
python3 ~/my-wiki-v2/scripts/benchmark.py --verbose
```

---

## 8. Markdown 页面规范

### Frontmatter 模板

```yaml
---
title: "标题"
description: "一句话摘要（用于搜索结果展示）"
type: source              # source | entity | concept | analysis
date: 2026-04-13
tags:
  - tag1
  - tag2
aliases:
  - 别名1                 # Obsidian 兼容
---
```

**必填**: title, type, date, tags
**可选**: description, aliases, deprecated, lastmod

### 内容结构

```markdown
## 原文

[原文链接](https://example.com) · 2026-04-13

## 核心要点

1. **要点一**：说明
2. **要点二**：说明

## 详细内容

使用 [[wikilinks]] 链接到其他页面。
表格、列表、代码块都用标准 Markdown。

> [!warning]
> Callout 语法（Obsidian 兼容）
```

### 命名规则

- **Slug**: kebab-case, 如 `tpu-v7`, `knowledge-compounding`
- **Source 页面**: 加日期后缀, 如 `karpathy-llm-wiki-20260407`
- **Entity/Concept 页面**: 不加日期, 如 `tpu-v7`, `mixed-precision`

### 质量规则

1. 一个概念一个页面
2. 优先更新已有页面（先查再建）
3. 矛盾用 `> [!warning]` callout 标注
4. 来源必须引用
5. 提到已有概念/实体时用 `[[slug]]` wikilink
6. `raw/` 目录只增不改

---

## 9. Quartz 内建功能

这些由 Quartz 自动提供，**不需要手动维护**：

| 功能 | 说明 |
|------|------|
| **知识图谱** | 从 `[[wikilinks]]` 自动生成交互式图谱 |
| **反向链接** | 每页底部显示"哪些页面引用了本页" |
| **全文搜索** | FlexSearch, 构建时生成索引 |
| **目录页** | 每个文件夹自动生成索引页 |
| **Tag 聚合** | 自动聚合同标签页面 |
| **ToC 目录** | 每页右侧自动生成 |
| **KaTeX 数学** | 支持 `$inline$` 和 `$$block$$` |
| **Dark/Light 模式** | 主题切换 |

---

## 10. 同义词表（synonyms.json）

支持 30+ AI/ML 领域的双向同义词映射：

```json
{
  "tpu": ["tensor processing unit", "ironwood", "trillium"],
  "gpu": ["graphics processing unit", "cuda", "nvidia"],
  "llm": ["large language model", "大语言模型", "大模型"],
  "inference": ["推理", "serving"],
  "training": ["训练", "fine-tuning", "微调"],
  "memory": ["显存", "内存", "hbm"],
  "sharding": ["分片", "shard", "partition"],
  "migration": ["迁移", "porting", "移植"]
}
```

搜索 "tpu" 自动扩展为 "tpu OR tensor processing unit OR ironwood OR trillium"。

---

## 11. 脚本清单

| 脚本 | 行数 | 用途 | 调用时机 |
|------|------|------|---------|
| `wiki_utils.py` | 96 | 常量、frontmatter 解析、slug 查找、wikilink 提取 | 被其他脚本 import |
| `query.py` | ~500 | BM25 搜索引擎 + 倒排索引 + LRU 缓存 | MCP server / CLI |
| `wiki-mcp-server.py` | ~520 | MCP Server, 9 个 tools | Claude Code MCP |
| `ingest.py` | 160 | 录入管道: 保存 raw + 创建骨架 | `/wiki ingest` |
| `lint.py` | 177 | 健康检查: 断链、孤儿、frontmatter | `/wiki lint` |
| `status.py` | 165 | 统计 + 知识覆盖度评分 | `/wiki status` |
| `benchmark.py` | 122 | 搜索质量基准测试（P@1, P@3, MRR） | 调参验证 |
| `convert-html-to-md.py` | 960 | HTML → Markdown 批量转换 | 批量录入 |
| `html2md.py` | 842 | HTML 解析工具（markdownify 扩展） | convert 依赖 |
| `build-and-sync.sh` | ~20 | Quartz 构建 + GCS 同步 | 每次内容变更 |
| `synonyms.json` | 35 | 同义词表 | query.py 加载 |
| `test_queries.json` | 30条 | 标准测试集 | benchmark.py |

---

## 12. Bot 自主行为规则

以下规则写在 CLAUDE.md 中，每次 session 自动生效：

1. **识别知识价值**: 用户分享有长期参考价值的内容时，主动问"要录入 Wiki 吗？"
2. **查 Wiki 再回答**: 知识性问题先用 MCP `wiki_query` 搜索，有则引用
3. **好回答建议回存**: 生成了有持久价值的分析时，建议存到 Wiki
4. **Lint 提醒**: 每 10 次 ingest 或距上次 lint 超一周，提醒跑 `/wiki lint`
5. **对话结束评估**: 技术分析/对比/排查对话结束时，评估是否有值得回存的综合结论

---

## 13. Troubleshooting

### MCP Server 不响应

```bash
# 验证能启动
python3 ~/my-wiki-v2/scripts/wiki-mcp-server.py
# 应该等待 stdin 输入，Ctrl+C 退出

# 检查依赖
python3 -c "import jieba, yaml, mcp; print('OK')"
```

### 搜索结果不准

```bash
# 跑 benchmark 看指标
python3 ~/my-wiki-v2/scripts/benchmark.py --verbose

# 检查分词
python3 -c "from query import _tokenize; print(_tokenize('你的查询'))"

# 检查同义词
python3 -c "from query import _synonym_map; print(_synonym_map.get('tpu'))"
```

### 构建失败

```bash
# 确保 Quartz 依赖安装
cd ~/my-wiki-v2 && npm install

# 清理后重建
rm -rf ~/my-wiki-v2/public/ && npx quartz build
```

### 页面未更新

```bash
# 手动同步到 GCS
gsutil -m rsync -r -d ~/my-wiki-v2/public/ gs://$GCS_BUCKET/cc-pages/wiki-v2/
```
