---
name: feishu-mail
description: 飞书企业邮箱收发。当用户说"发邮件"、"收邮件"、"查邮件"、"回复邮件"、"send email"、"check email"等关键词时触发。
license: MIT
---

# 飞书企业邮箱（SMTP + IMAP）

每个 bot 有独立企业邮箱，SMTP 发信 + IMAP 收信，无需 OAuth。From 显示名自动取邮箱前缀。邮箱配置存储在 Firestore `bots/{bot_name}` 的 `email` 字段。

## 触发条件

- "发邮件"、"发 email"、"send email"、"邮件通知"
- "查邮件"、"收邮件"、"check email"、"看看邮箱"
- "回复邮件"、"reply email"

## 发送邮件

```bash
python3 ~/.claude/skills/feishu-mail/send_mail.py \
  --to "收件人@example.com" \
  --subject "标题" \
  --body "正文内容"
```

**参数：**
- `--to`: 收件人（必填，多个逗号分隔）
- `--subject`: 标题（必填）
- `--body`: 正文（必填）
- `--cc`: 抄送（可选）
- `--html`: 正文为 HTML 格式

**示例：**
```bash
# 纯文本
python3 ~/.claude/skills/feishu-mail/send_mail.py \
  --to "user@example.com" --subject "通知" --body "任务完成"

# HTML 邮件
python3 ~/.claude/skills/feishu-mail/send_mail.py \
  --to "user@example.com" --subject "报告" --html \
  --body "<h2>标题</h2><p>详情...</p>"
```

## 查收邮件

```bash
python3 ~/.claude/skills/feishu-mail/recv_mail.py [选项]
```

**参数：**
- `--limit N`: 最多显示 N 封（默认 10）
- `--unread`: 只看未读
- `--from "xxx"`: 按发件人过滤
- `--subject "xxx"`: 按主题过滤
- `--json`: JSON 格式输出（适合程序处理）
- `--folder "INBOX"`: 文件夹（默认 INBOX）

**示例：**
```bash
# 查看最近 5 封
python3 ~/.claude/skills/feishu-mail/recv_mail.py --limit 5

# 只看未读
python3 ~/.claude/skills/feishu-mail/recv_mail.py --unread

# 按发件人过滤
python3 ~/.claude/skills/feishu-mail/recv_mail.py --from "sender@example.com"

# JSON 输出
python3 ~/.claude/skills/feishu-mail/recv_mail.py --json --limit 3
```

## 回复邮件

```bash
python3 ~/.claude/skills/feishu-mail/reply_mail.py \
  --id "邮件ID" \
  --body "回复内容"
```

**参数：**
- `--id`: 邮件 ID（从 recv_mail.py 输出获取，必填）
- `--body`: 回复正文（必填）
- `--html`: 正文为 HTML 格式
- `--folder`: 文件夹（默认 INBOX）

**示例：**
```bash
# 先查邮件获取 ID
python3 ~/.claude/skills/feishu-mail/recv_mail.py --limit 3
# 回复 ID 为 1 的邮件
python3 ~/.claude/skills/feishu-mail/reply_mail.py --id 1 --body "收到，谢谢！"
```

## 典型工作流

```bash
# 1. 查看未读邮件
python3 ~/.claude/skills/feishu-mail/recv_mail.py --unread --json

# 2. 根据内容决定回复
python3 ~/.claude/skills/feishu-mail/reply_mail.py --id 1 --body "已处理"

# 3. 主动发新邮件
python3 ~/.claude/skills/feishu-mail/send_mail.py \
  --to "someone@example.com" --subject "结果" --body "任务完成"
```

## 配置

凭据从 Firestore `bots/{bot_name}` 的 `email` 字段读取，`main.py` 启动时自动映射到环境变量：

- `FEISHU_SMTP_HOST`: smtp.feishu.cn
- `FEISHU_SMTP_PORT`: 465 (SSL)
- `FEISHU_SMTP_USER`: bot 企业邮箱地址
- `FEISHU_SMTP_PASS`: SMTP/IMAP 专用密码（飞书管理后台生成）

多 bot 同机时，各 bot 从 Firestore 读取独立邮箱配置，`main.py` 启动时自动设置环境变量。

IMAP 使用相同凭据，服务器 `imap.feishu.cn:993` (SSL)。

## 限制

- 发信频率: 200 封/100 秒
- 单日上限: 100 封/发件人
