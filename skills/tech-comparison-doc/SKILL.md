---
name: tech-comparison-doc
description: "Build comprehensive technical comparison documents (HTML) with iterative research, SVG diagrams, review loops, and CC Pages publishing. Default style follows page-style skill (Material Design). Trigger on: '对比文档', '技术对比', 'comparison document', 'framework comparison', '横向对比', '横评', 'compare X vs Y'."
---

# Technical Comparison Document Builder

从零构建高质量技术对比文档的完整方法论。经过实战验证（TPU 推理框架对比项目，7 版迭代，3 轮 review）。

## ⚠️ 起手必做（Phase 0）

```
开始写任何文档前，先读两个 skill：
1. page-style skill → 获取 Chris 的视觉偏好（Material Design，禁止 glassmorphism）
2. internal-doc-site skill → 获取文档站建设全流程模板
```

**血泪教训：** 2026-05-15 TPU 对比文档用了 glassmorphism + grain texture，效果漂亮但违反了 Chris 明确的 Material Design 偏好。Bunny R3 review 才发现。

## 核心原则

1. **文档是涌现式的** — 不要试图一次写完。写着写着才知道缺什么数据。
2. **一图顶千言** — 架构图、决策流程图、生态关系图是必须的。
3. **Review 循环是质量保障** — 发给 Bunny 做结构化 review，按 P0/P1/P2 优先级迭代。
4. **多视角审查** — Claude 看工程正确性，Gemini 看前端审美（`gemini-ui-reviewer`）。
5. **方法论 > 文档本身** — 过程中积累的经验比输出物更有价值。

## Phase 1: 信息搜集（并行多 Agent）

### 策略
- 主 session 用 GitHub API 搜仓库元数据（PRs、issues、commits、README）
- Spawn 2-3 个子 agent **并行**搜补充信息（博客、文档站、benchmark）
- 自己也动手搜关键资料（web_fetch 博客、GitHub issue 读路线图）

### 子 Agent 管理要点

| 配置项 | 错误做法 | 正确做法 |
|--------|---------|---------|
| 模型 | 不指定（默认可能是 flash-lite） | 显式指定 `model: "anthropic-vertex/claude-sonnet-4-6"` 或确认默认配置 |
| 超时 | 90-120 秒 | `runTimeoutSeconds: 600`（10 分钟） |
| 任务粒度 | 一个子 agent 做所有事 | 拆细：一个搜 benchmark，一个搜路线图 |
| 进度监控 | yield 后瞎等 | 不 yield，用 `subagents list` + `sessions_history` 每 30-60 秒查进度 |
| 上下文 | fork 父 session | isolated 模式，需要的数据写进 task 描述 |

### 信息搜集清单

- [ ] 双方 README / 官方文档
- [ ] 双方 GitHub issue 路线图（搜 "roadmap" label）
- [ ] 双方最近 30 天 PR 合入记录
- [ ] 官方博客文章（web_fetch）
- [ ] 性能 benchmark 数据（PR 描述里经常有）
- [ ] 社区规模（contributors、stars）
- [ ] 硬件支持矩阵

## Phase 2: 骨架搭建

### 推荐章节结构（Bunny 验证版）

```
01 Hero / Quick Stats — 5s 内抓住读者
02 Overview / 概览 — 各方一句话画像
03 Design Philosophy — 为什么不一样
04 Architecture — 静态结构图（组件层级）
05 Data Flow — 动态流程图（swimlane）
06 Performance — 数据表格 + 时效说明
07 Feature Matrix — 特性逐项对比
08 Roadmap — 各方下一步 + ETA
09 Decision Guide — 流程图 + 场景卡片
10 Summary — 不是竞争是互补
```

### 先骨架再填肉
1. 先输出完整 HTML 框架（所有章节标题 + 空卡片）
2. 逐节填充内容
3. 每填完一节检查：数据来源标注了吗？有 insight 卡片吗？

## Phase 3: SVG 图表

### 必须画的图

| 图类型 | 工具选择 | 放在哪 |
|--------|---------|--------|
| 架构对比图（组件层级） | 手写 inline SVG 或 fireworks-tech-graph | Section 04 |
| 数据流/生命周期图（swimlane） | 手写 inline SVG（需要精确像素控制） | Section 05 |
| 技术生态关系图（多层级连线） | **drawio-skill**（复杂图表优势明显） | Section 04 |
| 选型决策流程图 | 手写 inline SVG（节点 ≤ 5 个） | Section 09 |

### 图表工具选择

| 工具 | 适合场景 | 备注 |
|------|---------|------|
| **fireworks-tech-graph** | 架构图、概念图 | 7 套预定义风格，自动 XML 校验 + PNG 导出 |
| **drawio-skill** | 复杂关系图、多层连线 | .drawio 文件可用桌面端继续编辑 |
| 手写 inline SVG | 精确控制、文档内嵌 | 适合 swimlane、简单流程图 |

### SVG 三大铁律（Bunny 经验）

**(1) viewBox 优先于固定宽高**
```html
<svg viewBox="0 0 960 290" style="width:100%;max-width:960px">
```

**(2) 三种视觉语言原则——独立运作不叠加**
- 颜色编码 = 主体归属（蓝=框架A / 绿=框架B / 灰=共享）
- 边框样式 = 语义类型（dashed=边界 / bold=优势 / solid=普通）
- 背景填充 = 区域分组
- ❌ 不要"红色 + bold + 渐变背景"叠在一个元素上

**(3) 文字密度限制**
- 单元素 ≤ 2 行文字
- 文字距框边 ≥ 6px
- 字号 ≥ 7px

## Phase 4: 视觉设计

### ⚠️ 先查 page-style skill 确认风格偏好

当前偏好：**Material Design 风格**
- 背景纯白 / 极浅灰
- 卡片白底 + 1px 边框 + 浅阴影
- 强调色 Google Blue #1A73E8
- 字体 Google Sans / Roboto / Inter
- SVG 平面蓝白灰，不用渐变

### 配色禁区

| 禁用 | 原因 | 替代 |
|------|------|------|
| 紫色系 (#7c3aed, #8b5cf6) | "AI 味"最明显的颜色 | Teal (#0d9488)、Amber (#d97706) |
| Glassmorphism（毛玻璃） | Chris 明确禁止 | Material Design 卡片 |
| 渐变文字 / 霓虹光晕 | 2018 年审美，AI 生成味 | 纯色文字 |
| 黑紫渐变背景 | 同上 | 纯白 / 极浅灰 |

**推荐对比双色：** 蓝 (#2563eb) + 青绿 (#0d9488)

### 配色只用 3-5 个颜色
- 1 primary + 1 secondary + 1 success(绿) + 1 warning(amber) + 1 danger(红)
- 文字三档灰：#202124 / #5F6368 / #9CA3AF

### 排版节奏（Bunny 经验）
- insight block 之间至少隔一个内容块
- section lead 限 2 句话（what + why）
- 表格行数 > 8 时加分组 header
- 决策树节点 ≤ 5 个

## Phase 5: Review 循环

### 三轮迭代节奏

| 轮次 | 重点 | 预期提升 |
|------|------|---------|
| R1 | 结构问题（HTML 断裂、数据矛盾） | 75% → 95% |
| R2 | 细节问题（语义混淆、文案中性度） | 95% → 99% |
| R3 | 增强内容（数据流图、ETA、polish） | 99% → 100% |

### 多视角审查

| 审查者 | 强项 | 用法 |
|--------|------|------|
| Bunny (Claude) | 工程正确性、内容完整性、结构逻辑 | Firestore inbox 发 review 请求 |
| gemini-ui-reviewer | **前端审美**（行高、对比度、留白） | `gemini-ui review` 跑 design review |
| Jarvis (可选) | 并行第二视角 | 和 Bunny 同时 review，对比筛选 |

### Review 第一步：先验证 HTML 完整性
```bash
for tag in section div table tr td th pre code span; do
  open=$(grep -o "<$tag[ >]" FILE | wc -l)
  close=$(grep -o "</$tag>" FILE | wc -l)
  echo "$tag: $open/$close $([ $open -eq $close ] && echo '✓' || echo '✗')"
done
```

### 中立性原则
- 数据来源全部标注
- "❓ 未列出" 区分 "作者没查" vs "对端不支持"
- 决策树不把读者推向单一选项
- 总结段不写"X 是标准答案"

### 时效性管理
- Hero 标日期
- 数据表标采集日期
- Roadmap：已完成标月份 / 进行中标季度 / 规划标半年
- Footer 重申截止日期

## Phase 6: 发布

### CC Pages 发布
```bash
gsutil cp index.html gs://chris-pgp-host-asia/cc-pages/pages/{topic}-{YYYYMMDD}.html
# URL: https://cc.higcp.com/pages/{topic}-{YYYYMMDD}.html
```

### 发布后
- [ ] 录入 Wiki（长篇技术对比是 Wiki 优质内容来源）
- [ ] 飞书发链接不带引号
- [ ] 收集读者反馈 → 决定 P2 是否修

## 反模式（踩过的坑）

| 反模式 | 后果 | 正确做法 |
|--------|------|---------|
| 不看 page-style 就开始写 | 风格偏好踩坑 | Phase 0 先读 skill |
| 一次性让子 agent 写完整文档 | 超时 / 质量差 | 自己写骨架，子 agent 做局部编辑 |
| 子 agent 不指定模型 | 被降级到 flash-lite | 显式指定或确认默认配置 |
| yield 后瞎等 | 用户看不到进度 | 主动监控 + 汇报 |
| 纯文字堆叠 | 可读性差 | 一图顶千言，SVG + drawio |
| 自己觉得完了就发布 | 遗漏 P0（HTML 截断） | 至少一轮第三方 review |
| 性能数据不标注时间 | 读者被误导 | 每组数据标明时间和版本 |
| 只用 Claude review | 前端审美盲区 | 加 gemini-ui-reviewer |
| 用紫色 / glassmorphism | AI 味 / 违反偏好 | 查 page-style，用 Material Design |

## 设计参考资源（Bunny 推荐）

### 灵感
- Apple 产品页 — 顶级长滚动模板
- Stripe Press — 技术阅读体验标杆
- Vercel/Linear blog — SaaS 极简参考
- SemiAnalysis — 技术对比 long-form 标杆

### 工具
- Coolors.co — 配色 + accessibility
- Type Scale (typescale.com) — 字号比例
- Tabler Icons — 4500+ SVG 图标
- Radix Colors — 可访问性色板

### 书
- 《Refactoring UI》— 工程师写给工程师的设计书

## 依赖工具链

```
page-style          → 视觉风格规范（必读）
internal-doc-site   → 文档站全流程模板
fireworks-tech-graph → SVG 技术图生成
drawio-skill        → 复杂关系图（需要 xvfb-run 包装 CLI）
gemini-ui-reviewer  → Gemini 前端审美 review
wiki                → 发布后录入知识库
notify              → 多平台通知
```

## 文件结构

```
skills/tech-comparison-doc/
├── SKILL.md                    ← 本文件
└── references/
    └── css-component-lib.md    ← 可复用 CSS 组件代码片段
```
