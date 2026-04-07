# log.json 格式规范

`wiki-data/log.json` 是 Wiki 操作日志的机器可读版本，与 `wiki/log.html` 并行维护。

## 结构

```json
{
  "entries": [
    {
      "timestamp": "2026-04-07T02:45:00Z",
      "operation": "ingest",
      "title": "Karpathy LLM Wiki Gist",
      "description": "录入 Karpathy 的 LLM Wiki 模式 Gist",
      "pages_created": ["sources/karpathy-llm-wiki", "entities/karpathy", "entities/obsidian", "concepts/rag", "concepts/knowledge-compounding", "concepts/memex"],
      "pages_updated": [],
      "source": "https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f"
    }
  ]
}
```

## 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| timestamp | string | ISO 8601 时间戳 |
| operation | string | ingest / query / lint / update / delete |
| title | string | 操作标题（简短描述） |
| description | string | 操作详情 |
| pages_created | string[] | 新建的页面 slug 列表 |
| pages_updated | string[] | 更新的页面 slug 列表 |
| source | string? | 来源 URL（ingest 时填写，可选） |

## 使用场景

- `/wiki status`：读取 log.json 统计最近操作
- `/wiki lint`：检查操作频率、发现长期未更新的页面
- Bot 日常感知：快速了解 Wiki 活跃度，无需解析 HTML
