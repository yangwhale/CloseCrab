# graph.json 格式规范

`wiki-data/graph.json` 是 Wiki 的机器可读知识图谱，供 Bot 导航和 D3.js 可视化。

## 结构

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

## 字段说明

### nodes
| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | 页面 slug（kebab-case） |
| title | string | 页面标题 |
| type | string | source / entity / concept / analysis |
| path | string | 相对于 `wiki/` 的路径 |
| tags | string[] | 标签数组 |
| summary | string | 一行摘要 |
| created | string | 创建日期 YYYY-MM-DD |
| updated | string | 最后更新日期 |
| source_count | number | 引用来源数 |

### links
| 字段 | 类型 | 说明 |
|------|------|------|
| source | string | 来源页面 id |
| target | string | 目标页面 id |
| type | string | mentions / references / contradicts / extends |
