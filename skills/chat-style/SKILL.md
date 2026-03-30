---
name: chat-style
description: 聊天平台消息格式化规则。当 Claude Code 通过聊天平台（Discord、飞书等）运行时自动适配消息风格，避免渲染问题。当用户说"聊天风格"、"chat风格"、"消息格式"时触发。
---

# 聊天平台消息风格指南

通过聊天平台（Discord、飞书等）与用户交互时，遵循以下规则让消息正确显示且易读。

## 核心原则

聊天平台不是文档。简短、直接、对话式。

## 格式规则

### 可以用

- **粗体** 强调重点
- `代码` 标记技术术语
- ```代码块``` 贴代码（带语言标记）
- 无序列表 `-` 和有序列表 `1.`
- `> 引用` 引用内容
- `||剧透||` 隐藏长输出（Discord 特有）

### 不要用

- **嵌套列表** — 大部分聊天平台不支持缩进列表
- **图片 `![]()`** — 不渲染，用文件附件替代
- **脚注 `[^1]`** — 不支持

### 表格

- **飞书**：可以直接用 markdown 表格语法 `| col |`，bot 会自动转换为 `column_set` 原生表格（灰白交替行）
- **Discord**：不支持表格，改用代码块对齐或列表格式

飞书表格注意事项：
- 表格单元格内的反引号 `` ` `` 会被自动去除（`column_set` 内不支持行内代码）
- 第一个 `#` 标题会变成卡片彩色 header（indigo），不要在表格前重复写标题
- `---` 分隔线会变成卡片原生 `hr` 元素

Discord 表格替代方案 — 代码块对齐（用英文/ASCII 避免双宽字符错位）：

```
Model   VRAM         FP16         FP8
A100    80GB HBM2e   312 TFLOPS   N/A
H100    80GB HBM3    989 TFLOPS   1979 TFLOPS
B200    192GB HBM3e  2250 TFLOPS  4500 TFLOPS
```

Discord 列表格式（适合少量字段或中文标签）：

**A100 SXM**
- VRAM: 80GB HBM2e
- FP16: 312 TFLOPS

**H100 SXM**
- VRAM: 80GB HBM3
- FP16: 989 TFLOPS

## 消息长度

- 聊天消息保持简短，长回复拆分成多条
- 在自然断点（换行、段落）处切分
- 优先发核心结论，细节按需展开

## 写作风格

- 短句为主，1-3 句话说清一件事
- 不要 "我很高兴为您..." 之类的废话
- 中文为主，技术术语保留英文
- 匹配对话的语气和节奏
- 结论先行，不要铺垫

## 代码输出

- 短代码（<10行）直接贴代码块
- 长代码建议用户看文件
- 错误信息只贴关键行，不要整段 stack trace

## 富内容页面

复杂内容（大表格、图表、报告）不适合聊天消息时，生成 HTML 页面到 CC Pages：

1. 写 HTML 到 `$CC_PAGES_WEB_ROOT/pages/{topic}-{YYYYMMDD-HHmmss}.html`
2. 用 Playwright 截图(1200x630) 保存到 `$CC_PAGES_WEB_ROOT/assets/og-{topic}.png`
3. 页面 og:image 指向专属截图
4. 发送链接 `$CC_PAGES_URL_PREFIX/pages/{filename}`

### OG 标签模板

```html
<meta property="og:title" content="页面标题">
<meta property="og:description" content="简短描述">
<meta property="og:image" content="$CC_PAGES_URL_PREFIX/assets/og-{topic}.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="theme-color" content="#22C55E">
```

## 进度更新

长任务中主动汇报，但不要刷屏：
- 开始时：一句话说清在做什么
- 关键节点：完成了什么 / 遇到问题
- 结束时：结果 + 变更摘要
