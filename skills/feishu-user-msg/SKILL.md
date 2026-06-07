---
name: feishu-user-msg
description: 以 Chris 身份通过飞书 API 发消息到 bot DM 窗口。支持文字和语音。Siri 快捷指令的服务端。
trigger: 当用户说"以我的身份发飞书"、"feishu user msg"、"给某bot发消息"、"siri发消息"时触发。
---

# 飞书用户身份消息

以 Chris 的飞书账号身份发消息到任意 bot 的 DM 窗口。消息显示为 Chris 发出，bot 按正常聊天路径处理。

## 用法

```bash
# 发文字给 Jarvis
python3 ~/.claude/skills/feishu-user-msg/scripts/send_as_user.py \
  --to jarvis --text "明天三点提醒我开会"

# 发语音给 Jarvis
python3 ~/.claude/skills/feishu-user-msg/scripts/send_as_user.py \
  --to jarvis --audio /tmp/voice.ogg

# 查看状态
python3 ~/.claude/skills/feishu-user-msg/scripts/send_as_user.py --status

# 初始化/重新授权
python3 ~/.claude/skills/feishu-user-msg/scripts/send_as_user.py --init-token --code "CODE"
```

## 可用 bot

仅 **jarvis**。飞书 OAuth token 是 per-app 的，只能发到授权应用自己的聊天窗口。给其他 bot 发消息请通过 Jarvis 转发（inbox）。

## Token 管理

- 存储：`~/.closecrab/feishu-user-token.json`
- access_token 2h 有效，自动刷新
- refresh_token 30 天有效，<7 天时打 warning
- 过期后需重新 OAuth 授权（`--init-token`）
