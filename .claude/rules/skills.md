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
必须包含以下 frontmatter：
```yaml
---
name: skill-name
description: 一句话描述
trigger: 触发关键词或场景
---
```

## 规则
- 新建 skill 用 `skill-creator` skill 生成模板，不要手动创建
- Skill 名用 kebab-case（如 `sglang-installer`，不是 `sglang_installer`）
- SKILL.md 里写清楚触发条件和使用示例，Claude 靠这个判断何时激活
- 脚本应该幂等（重复执行不出错）
- 不要在 skill 里硬编码机器地址或 credentials
- 部署后 skill 通过 symlink 挂载：`~/.claude/skills/{name}` → `CloseCrab/skills/{name}`
