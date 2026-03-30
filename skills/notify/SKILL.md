---
name: notify
description: 发送通知/报告到各平台（Discord、飞书、微信）。当用户说"通知我"、"发给我"、"发discord"、"发飞书"、"发微信"、"ds通知"、"notify"等关键词时触发。合并了 discord-report、feishu-report、wechat-report 三个 skill。
---

# 多平台通知发送

将任务报告或通知发送到 Discord、飞书、微信。根据用户指定的平台选择对应脚本。

## 触发条件

- "通知我"、"发给我"、"notify me"
- "发discord"、"ds通知"、"ds发给我"
- "发飞书"、"飞书通知"、"飞书发给我"
- "发微信"、"微信通知"、"微信发给我"

## 报告格式

生成 Markdown 格式报告：

```markdown
## 任务摘要
[简要描述]

## 完成的工作
- [工作项 1]
- [工作项 2]

## 关键变更
[主要代码/配置变更]

## 注意事项
[后续步骤或需注意的问题]
```

## 发送命令

### Discord

```bash
~/.claude/scripts/send-to-discord.sh "报告内容" "标题"

# 带图片
~/.claude/scripts/send-to-discord.sh "报告内容" "标题" "/path/to/image.png"

# stdin 传入
cat << 'EOF' | ~/.claude/scripts/send-to-discord.sh "" "标题"
报告内容...
EOF
```

- 显示为 Embed 卡片（蓝色侧边条）
- 单条 Embed 最大 4096 字符，自动分片
- 支持图片（本地文件或 URL，≤25MB）

### 飞书

```bash
~/.claude/scripts/send-to-feishu.sh "报告内容" "标题"

# stdin
cat << 'EOF' | ~/.claude/scripts/send-to-feishu.sh
报告内容...
EOF
```

- 显示为卡片消息
- 最大约 30KB，超长自动截断

### 微信（Server酱）

```bash
~/.claude/scripts/send-to-wechat.sh "报告内容" "标题"

# stdin
cat << 'EOF' | ~/.claude/scripts/send-to-wechat.sh
报告内容...
EOF
```

- 标题最长 256 字符，内容最大 32KB
- 支持完整 Markdown

## 平台 Markdown 差异

| 特性 | Discord | 飞书 | 微信 |
|------|---------|------|------|
| 粗体/斜体 | Y | Y | Y |
| 代码块 | Y | Y | Y |
| 链接 | Y | Y | Y |
| 表格 | N | 原生 column_set | Y |
| 图片 | 附件 | 需上传 | Y |

**Discord/飞书表格替代**：用代码块对齐或列表格式（详见 `chat-style` skill）。

## 平台选择逻辑

- 用户明确指定平台 → 用指定的
- 未指定但当前在 Discord 对话 → 用 Discord
- 未指定但当前在飞书对话 → 用飞书
- 说"通知我"但未指定 → 问用户

## 故障排查

- **Discord 发送失败**：检查 Bot 是否运行、Token 有效、频道权限
- **飞书发送失败**：检查 Webhook URL 有效、网络连通
- **微信发送失败**：检查 Server酱 SendKey 有效
