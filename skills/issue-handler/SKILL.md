---
name: issue-handler
description: 处理 GitHub Issue：阅读留言、调研问题、生成报告文档、回复 issue 互动。当用户说"处理 issue"、"回答 issue"、"做这个 issue"、"handle issue"、"看看 issue"时触发。
---

# GitHub Issue Handler

自动化处理 GitHub repo 中的 issue：阅读留言 → 理解问题 → 调研 → 生成文档 → 发布 → 回复互动。

**核心原则：不要自己关 issue，让提问者确认满意后自行关闭。**

## 触发条件

- 用户说"处理这个 issue"、"做这个 issue"、"回答 issue"、"handle issue"
- 用户给出 issue URL 或 repo + issue 编号
- 用户查看 issue 后说"做"

## 工作流程

### Phase 1: 阅读 Issue 和留言

1. 用 GitHub MCP (`mcp__plugin_github_github__issue_read`) 读取 issue 内容
2. **读取所有留言**：用 `gh api repos/{owner}/{repo}/issues/{number}/comments` 获取全部评论，了解讨论上下文
3. 分析 issue 当前状态：
   - 提问者原始需求是什么
   - 是否已有人回复，回复了什么
   - 提问者是否有追问或补充
   - 是否需要进一步调研或补充回答
4. 分析 issue 要求：
   - 是否需要调研（数据收集、对比分析）
   - 是否需要生成文档（报告、教程、对比表）
   - 是否需要写代码（脚本、配置、示例）
5. 确认 repo 本地路径（通常在 `~/gpu-tpu-pedia` 或用户指定位置）

### Phase 2: 调研与数据收集

1. 根据 issue 主题，启动 Agent 并行调研（使用 `subagent_type: general-purpose`）
2. 从官方来源收集数据（官方文档、博客、白皮书）
3. 交叉验证关键数据，标注数据来源

### Phase 3: 生成文档

1. **格式选择**：
   - 对比类 → HTML 文档（带柱状图、表格、分析）
   - 教程类 → Markdown 文档
   - 代码类 → 代码文件 + README

2. **HTML 文档规范**（如果生成 HTML）：
   - 深色主题、响应式布局
   - 包含 OG 标签（og:title, og:description, og:image）
   - 文件名格式：`{topic}-{YYYYMMDD}.html`
   - 写入 repo 对应目录（如 `tpu/`, `gpu/`）

3. **内容风格**：
   - 大白话讲解，不要学术腔
   - 数据来源标注
   - 客观中立，不带销售语气

### Phase 4: 发布

1. **CC Pages**：将 HTML 写到 `$CC_PAGES_WEB_ROOT/pages/`
2. **OG 截图**：用 Playwright 截图（1200×630）保存到 `$CC_PAGES_WEB_ROOT/assets/`
3. **PDF**：用 Playwright `page.pdf()` 生成 PDF 到 assets 目录
4. **GitHub Pages**：如果 repo 开了 Pages，文件 push 后自动部署
5. **GitHub repo**：commit 到对应目录

### Phase 5: 回复 Issue 互动

1. **先读留言**：处理前必须读完 issue 所有留言，理解讨论上下文
2. 用 `mcp__plugin_github_github__add_issue_comment` 在 issue 留言：
   - 像回答问题一样自然回复，不要机械模板
   - 简要说明完成了什么
   - 给出在线阅读链接（GitHub Pages / CC Pages）
   - 给出 PDF 下载链接（如有）
   - 列出报告的主要内容章节
   - 标注数据来源
   - 结尾友好地请提问者查看，有问题可以继续留言讨论
3. **绝对不要自动关闭 issue**，让提问者确认满意后自行关闭
4. 通知用户（Discord / 直接回复）

### Phase 6: 跟进留言（如有后续）

当用户再次说"看看 issue"或"处理 issue"时：
1. 重新读取 issue 的所有留言
2. 检查是否有新的追问或反馈
3. 针对追问回复，补充内容或修改文档
4. 继续互动，直到提问者满意关闭 issue

## Issue 留言风格

**像人一样回答问题，不要像机器人发通知。** 留言应该是在回答提问者的问题，语气自然。

### 首次回复模板（参考，不要死板照搬）

```markdown
Hi，报告做好了，看看是不是你要的：

📖 **在线阅读**: [GitHub Pages 链接]
📄 **PDF 版本**: [链接]

主要内容包括：
- [要点1]
- [要点2]
- ...

数据来源：[来源简述]

有问题随时留言，我来补充。觉得 OK 的话可以关掉这个 issue。
```

### 跟进回复（回答追问）

```markdown
[直接回答问题]

[如果更新了文档] 文档已更新：[链接]
[如果需要讨论] 你觉得这样可以吗？
```

## 使用方式

### /issue-handler [repo] [issue_number]

处理指定 repo 的 issue。

**参数**：
- `repo`：仓库名（owner/repo 格式）
- `issue_number`：issue 编号

**示例**：
```
/issue-handler gpu-tpu-pedia 1
/issue-handler owner/repo-name 2
```

### /issue-handler list [repo]

列出指定 repo 的 open issues。

**示例**：
```
/issue-handler list gpu-tpu-pedia
```

## GitHub Pages 启用

如果 repo 未开启 GitHub Pages，自动通过 API 启用：

```bash
curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/{owner}/{repo}/pages \
  -d '{"build_type":"legacy","source":{"branch":"main","path":"/"}}'
```

需要 `.nojekyll` 文件确保原始 HTML 正确渲染。

## 注意事项

- **先读留言再动手**：处理前必须读完 issue 正文 + 所有评论，不要遗漏上下文
- **不要自动关 issue**：永远让提问者自行关闭，你只负责回答问题和交付成果
- 回复 issue 时像回答问题一样自然，不要机械模板
- 调研时优先使用官方数据源
- 文档风格保持大白话，客观中立
- 如果 issue 有多个问题，逐一回答
- 如果有追问，及时跟进补充
- 关闭 issue 的 API（MCP `issue_write`）有 bug，可能返回成功但实际没关，如果真需要关（用户明确要求），用 REST API `curl -X PATCH` 代替

## 已知的坑

- PAT 没有 `workflow` scope 时推不了 `.github/workflows/` 文件，改用 API 直接启用 Pages（legacy 模式）
- GitHub Pages 启用后不会自动触发首次 build，需要手动 `POST /pages/builds` 或推一个新 commit
- 需要 `.nojekyll` 文件否则 Jekyll 会跳过某些文件
