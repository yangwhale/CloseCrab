---
name: cc-pages-backup
description: CC Pages 增量备份到 GitHub private repo
trigger: 用户说"备份 CC Pages"、"sync pages"、"backup pages" 时触发
---

# CC Pages Backup

将 CC Pages 的 pages/ 和 assets/ 增量同步到 GitHub private repo (`yangwhale/my-private`)。

## 触发场景

- 用户说 "备份 CC Pages" / "sync pages" / "backup pages"
- 定期维护时主动执行
- 写了新的重要文档后

## 使用方法

```bash
# 增量同步 + commit + push
~/CloseCrab/skills/cc-pages-backup/scripts/backup-cc-pages.sh

# 先看看会同步什么（不实际执行）
~/CloseCrab/skills/cc-pages-backup/scripts/backup-cc-pages.sh --dry-run
```

## 备份范围

| 目录 | 包含 | 排除 |
|------|------|------|
| pages/ | 全部 HTML 技术文档 | 无 |
| assets/ | PNG/JPG/WebP/OGG/MP3/HTML/CSS/JS/PDF/PPTX | .pb/.trace.json/.mp4/.m4a/.webm/.zip/.wav/tb-traces/ |

排除的大文件（性能 trace、视频、ZIP）留在 GCS bucket 不进 GitHub。

## 依赖

- gcsfuse mount 在 `/home/chrisya/cc-pages-new`（挂载 `chris-pgp-host-asia` bucket 的 `cc-pages` 子目录）
- 如果 mount 不在，脚本会报错并给出重新挂载命令

## 备份目标

- 本地路径: `~/my-private/cc-pages-backup/`
- GitHub: `github.com/yangwhale/my-private` → `cc-pages-backup/` 目录
