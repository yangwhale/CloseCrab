---
globs: skills/**
---

# Skill 开发规则

## 结构
每个 skill 是一个目录：
```
skills/{skill-name}/
├── SKILL.md           # 必需：触发条件、用法、示例
├── scripts/           # 可选：Python/Bash 脚本
└── references/        # 可选：文档、配置模板
```

## SKILL.md 格式
```yaml
---
name: skill-name           # 必需
description: 一句话描述     # 必需 —— agent 靠它判断什么时候用这个 skill
trigger: 触发关键词或场景   # 可选，48 个 skill 里只有 7 个有
---
```
`description` 承担了实际的路由作用，写清楚"什么场景下该用"比写"这是什么"更有用。

## 规则
- 新建 skill 用 `skill-creator` skill 生成模板，不要手动创建
- Skill 名用 kebab-case（如 `sglang-installer`，不是 `sglang_installer`）
- SKILL.md 里写清楚触发条件和使用示例，Claude 靠这个判断何时激活
- 脚本应该幂等（重复执行不出错）
- 不要在 skill 里硬编码机器地址或 credentials

## 部署机制 —— 是拷贝，不是 symlink

`deploy.sh` 第 4 步（`deploy.sh:720-751`）做的是 **`cp -a`**，而且开头会
显式 `rm` 掉遗留的 `~/.claude/skills` symlink。这有两个后果，都很容易踩：

1. **改了 `skills/` 下的文件，不重跑 deploy 就不生效** —— 跑着的 bot 用的是
   `~/.claude/skills/` 下的那份拷贝。别指望改完源码 bot 立刻看到。
2. **直接改 `~/.claude/skills/` 下的文件，下次 deploy 会被覆盖**（`rm -rf` 后重拷）。
   改动一律回到仓库源目录。

### allowlist 决定装不装

不在 `config/skill-allowlist.txt` 里的 skill **根本不会被部署**（`deploy.sh:731`
的 `skill_allowed` 过滤）。新建 skill 后如果 agent 端"凭空找不到"，
第一个要查的就是这个文件 —— 不是 SKILL.md 写错了。

### 两个来源

| 来源 | 路径 | 说明 |
|---|---|---|
| 公共 | 仓库内 `skills/` | 跟代码一起版本管理 |
| 私有 | `$PRIVATE_SKILLS_DIR`（默认 `~/private-skills`） | 不进本仓库；目录不存在就整段跳过 |

两个来源都过同一份 allowlist。私有 skill 的名字**不要出现在本仓库的任何文档里**。
