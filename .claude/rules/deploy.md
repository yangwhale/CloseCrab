---
globs: deploy.sh, run.sh, scripts/*.sh, scripts/*.py
---

# 部署和运维脚本规则

## deploy.sh
- 809 行，自动化安装 Claude Code + Skills + Bot Python 依赖
- 支持 `--cc-only`（只装 CC 环境）、`--bot`（补装 Bot 依赖）、`--npm`（用 npm 代替官方 installer）
- 会生成 `.env`、`~/.claude/settings.json`、skills symlinks
- 修改 deploy.sh 后**必须**在至少一台 VM 上测试 `./deploy.sh --cc-only` 通过

## run.sh
- 自动重启 wrapper，检测退出码决定是否重启
- 连续崩溃 >10 次自动停止（dirty restart 保护）
- 不要改退出码约定（42=restart, 130/137=不重启, 1=不重启）

## scripts/ 目录
- `config-manage.py` — Bot CRUD（Firestore），修改时注意向后兼容 Firestore schema
- `inbox-send.py` — Bot 间消息，依赖 `BOT_NAME` 环境变量
- `dispatch-bot.sh` — 多 bot 调度（deploy/recall/move/check）
- `send-to-discord.sh` — 直接发 Discord 消息（webhook）
- `sync-memory.sh` — 记忆同步（本地 ↔ private repo）

## 注意
- 所有脚本都假设在 CloseCrab 根目录执行或通过绝对路径调用
- 不要删除 `config/env.sh`，deploy.sh 依赖它
- 修改 Firestore schema 时考虑已部署 bot 的兼容性
