---
name: bot-config
description: 管理 CloseCrab bot 配置（Firestore）。添加新 bot、给已有 bot 添加 channel、切换 channel、查看配置。当用户说"添加一个 bot"、"新建 bot"、"加个飞书 channel"、"加个 discord channel"、"切换到飞书"、"bot 配置"、"add bot"、"add channel"等关键词时触发。
---

# Bot Config 管理

所有 bot 配置存储在 Firestore（由 `FIRESTORE_PROJECT` / `FIRESTORE_DATABASE` 环境变量配置），通过 `scripts/config-manage.py` 管理。

## 管理脚本位置

```
~/CloseCrab/scripts/config-manage.py
```

## 支持的操作

### 1. 查看

```bash
# 列出所有 bot
python3 ~/CloseCrab/scripts/config-manage.py list

# 查看某个 bot 配置（密钥自动打码）
python3 ~/CloseCrab/scripts/config-manage.py show <bot_name>
```

### 2. 创建新 bot

用 AskUserQuestion 收集以下信息：
1. **Bot 名称** — 英文小写，用于 Firestore document ID 和 `run.sh <name>` 启动
2. **首选 channel 类型** — discord / feishu / lark / dingtalk
3. **channel 凭据** — 根据类型不同（见下方各 channel 指南）

```bash
python3 ~/CloseCrab/scripts/config-manage.py create <bot_name> \
  --channel <type> \
  --description "描述" \
  [channel-specific options]
```

### 3. 给已有 bot 添加 channel

```bash
python3 ~/CloseCrab/scripts/config-manage.py add-channel <bot_name> <channel_type> [options]
```

### 4. 切换活跃 channel

```bash
python3 ~/CloseCrab/scripts/config-manage.py set-channel <bot_name> <channel_type>
```

### 5. 修改配置字段

```bash
python3 ~/CloseCrab/scripts/config-manage.py set <bot_name> <field> <value>
```

### 6. 删除 bot

```bash
python3 ~/CloseCrab/scripts/config-manage.py delete <bot_name> --yes
```

---

## Channel 创建指南

### Discord

**需要的信息：**
- `--token` — Bot Token

**可选：**
- `--log-channel-id` — 日志频道 ID
- `--auto-respond-channels` — 自动回复频道 ID（逗号分隔）

**获取方式：**
1. 打开 [Discord Developer Portal](https://discord.com/developers/applications)
2. 点击 **New Application** → 起名 → 创建
3. 左侧 **Bot** 页面：
   - 点 **Reset Token** 复制 Token（只显示一次！）
   - 开启 3 个 Privileged Intents：**Presence Intent**、**Server Members Intent**、**Message Content Intent**
4. 左侧 **OAuth2** 页面：
   - Scopes: 勾选 `bot`、`applications.commands`
   - Bot Permissions: Send Messages, Read Message History, Embed Links, Attach Files, View Channels
   - 复制生成的 URL，在浏览器打开邀请 bot 进服务器
5. 左侧 **Installation** 页面：
   - **禁用 User Install**（安全考虑）
6. 获取频道 ID：Discord 设置 → 高级 → 开启开发者模式 → 右键频道 → 复制频道 ID

**示例：**
```bash
python3 ~/CloseCrab/scripts/config-manage.py create mybot \
  --channel discord \
  --token "MTQ3xxx..." \
  --log-channel-id "1234567890" \
  --auto-respond-channels "1111111111,2222222222"
```

---

### 飞书 (Feishu)

**需要的信息：**
- `--app-id` — 应用 App ID
- `--app-secret` — 应用 App Secret

**可选：**
- `--log-chat-id` — 日志群 Chat ID
- `--allowed-open-ids` — 允许的用户 Open ID（逗号分隔）
- `--auto-respond-chats` — 自动回复群 Chat ID（逗号分隔）

**获取方式：**
1. 打开 [飞书开放平台](https://open.feishu.cn/app)
2. 点击 **创建企业自建应用**
3. 在应用详情页的 **凭证与基础信息** 中获取 **App ID** 和 **App Secret**
4. 左侧 **添加应用能力** → 添加 **机器人**
5. **权限管理** → 申请以下权限：
   - `im:message` — 获取与发送消息
   - `im:message.group_at_msg` — 接收群聊 @机器人消息
   - `im:message.p2p_msg` — 接收私聊消息
   - `im:chat` — 获取群信息
   - `contact:user.base:readonly` — 获取用户基本信息（可选）
6. **事件与回调** → **事件配置**：
   - 启用 **长连接 (WebSocket)** 模式（推荐，无需公网 IP）
   - 订阅事件：`im.message.receive_v1`（接收消息）
7. **版本管理与发布** → 创建版本 → 申请发布 → 管理员审批
8. 发布后在飞书客户端搜索 bot 名称即可发起对话

**示例：**
```bash
python3 ~/CloseCrab/scripts/config-manage.py add-channel mybot feishu \
  --app-id "cli_a932b22651785cb2" \
  --app-secret "VEvf3daX..."
```

---

### Lark (国际版飞书)

**需要的信息：**
- `--app-id` — 应用 App ID（`cli_` 开头）
- `--app-secret` — 应用 App Secret

**获取方式：**
1. 打开 [Lark Developer Console](https://open.larksuite.com/app)
2. 步骤与飞书相同，但注意：
   - Lark Standard 版 **API 限额仅 10,000 次/月**（飞书基础版目前 1M/月）
   - 域名用 `open.larksuite.com` 而非 `open.feishu.cn`
   - SDK domain 用 `LARK_DOMAIN` 而非 `FEISHU_DOMAIN`

**注意事项：**
- 如果同时有飞书和 Lark 应用，需要分别创建两个应用（不同的 App ID）
- Lark 的 API quota 很低，建议只在必须接入国际用户时使用

**示例：**
```bash
python3 ~/CloseCrab/scripts/config-manage.py add-channel mybot lark \
  --app-id "cli_a948237ceff89eef" \
  --app-secret "L46GVtnm..."
```

---

### 钉钉 (DingTalk)

**需要的信息：**
- `--client-id` — 应用 Client ID（`ding` 开头）
- `--client-secret` — 应用 Client Secret

**获取方式：**
1. 打开 [钉钉开放平台](https://open-dev.dingtalk.com/)
2. 应用开发 → 企业内部开发 → 创建应用
3. 在 **应用凭证** 中获取 **Client ID** 和 **Client Secret**
4. 添加 **消息收发** 能力
5. 配置事件订阅（Stream 模式，无需公网 IP）

**示例：**
```bash
python3 ~/CloseCrab/scripts/config-manage.py create dingbot \
  --channel dingtalk \
  --client-id "dingxxx" \
  --client-secret "xxx"
```

---

## 创建后的操作

bot 配置写入 Firestore 后，还需要：

1. **启动 bot**：
   ```bash
   cd ~/CloseCrab && ./run.sh <bot_name>
   ```

2. **远程机器部署**（如需在其他机器运行）：
   ```bash
   ssh <machine> "cd ~/CloseCrab && git pull && ./run.sh <bot_name>"
   ```
   不需要 `.env` 文件，配置全从 Firestore 读取。

3. **加入 Team**（可选）：
   ```bash
   python3 ~/CloseCrab/scripts/config-manage.py set <bot_name> team \
     '{"role":"teammate","leader_bot_id":"1473626xxx","team_channel_id":"1477228xxx"}'
   ```

## 交互式引导

当用户没有提供完整信息时，用 AskUserQuestion 逐步引导：

1. 先问 **bot 名称**和 **channel 类型**
2. 根据 channel 类型，展示上方对应的「获取方式」步骤
3. 等用户提供凭据后执行创建命令
4. 创建完成后提示用户启动 bot
