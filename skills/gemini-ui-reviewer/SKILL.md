---
name: gemini-ui-reviewer
description: Let Gemini review and optimize frontend UI/CSS design. Gemini has better aesthetic sense for modern web design.
trigger: When the user says "让Gemini看看UI", "Gemini review UI", "请Gemini优化前端", "UI让Gemini改", "前端设计review", "gemini-ui", or when doing frontend CSS work and want a professional design review.
---

# Gemini UI Reviewer

Use Gemini's superior frontend aesthetic sense to review and optimize UI/CSS design. This skill leverages Gemini CLI for design review and CSS generation, while Claude handles integration, deployment, and engineering context.

## Philosophy

- **Gemini designs, Claude integrates** — Gemini outputs polished CSS, Claude handles codebase integration
- **Screenshot-driven review** — Always send actual screenshots, not just code
- **Concrete suggestions** — Ask for specific CSS changes, not abstract feedback

## Prerequisites

- `gemini` CLI installed and authenticated (`which gemini`)
- Chrome DevTools MCP for screenshots (optional, can use local file rendering)

## Workflow

### Step 1: Capture Current State

Take screenshots of the pages to review. Prefer local file rendering if the site is behind auth:

```bash
# Use Chrome MCP to navigate and screenshot
# navigate_page → file:///path/to/page.html
# take_screenshot → /tmp/screenshot.png
```

### Step 2: Send to Gemini for Review

Use `gemini -p` (headless mode) with the CSS piped via stdin:

```bash
cat /path/to/style.css | gemini -p "你是一个资深 UI/UX 设计师，擅长现代 Web 设计和 Google Material Design。

请审阅以下 CSS 并给出具体改进建议。重点关注：
1. 视觉层次感（信息密度、留白、字号梯度）
2. 色彩运用（accent 色用法、状态色）
3. 交互反馈（hover、active、focus 状态）
4. 组件一致性（卡片、标签、按钮风格）
5. 排版（字体、行高、间距节奏感）

用中文回答，直接给结论和代码。" 2>&1 | grep -v '^\[' | grep -v '^unknown format' | grep -v '^Loading extension'
```

### Step 3: Let Gemini Generate Complete CSS

Based on the review feedback, ask Gemini to output a complete replacement CSS:

```bash
cat /path/to/style.css | gemini -p "你是一个资深 UI/UX 设计师和前端工程师。

任务：请直接输出一份完整的、优化后的 CSS 文件。

设计目标：[描述目标风格]

你之前给的审阅意见（请全部落实）：
[粘贴上一步的建议要点]

额外要求：
- 保留所有现有的 CSS 类名
- 保留所有 CSS 变量名
- 输出完整的 CSS 文件内容，不要省略
- 不要输出解释文字，只输出纯 CSS 代码" 2>&1 | grep -v '^\[' | grep -v '^unknown format' | grep -v '^Loading extension'
```

### Step 4: Claude Integrates

Claude handles:
1. Write the Gemini-generated CSS to the target file
2. Update any inline styles in Python/JS files that conflict
3. Run rebuild pipeline
4. Take new screenshots for comparison
5. Deploy

### Step 5: Verify with Screenshots

Take new screenshots and compare before/after. Optionally send back to Gemini for a second pass.

## Gemini CLI Notes

- **Headless mode**: `gemini -p "prompt"` — non-interactive, returns output and exits
- **Pipe input**: `cat file | gemini -p "prompt"` — file content becomes context
- **Filter noise**: `2>&1 | grep -v '^\[' | grep -v '^unknown format' | grep -v '^Loading extension'` — removes extension loading warnings
- **Screenshots**: Gemini CLI can't receive image files in headless pipe mode. For visual review, describe the UI in text or use the interactive `gemini` with file arguments
- **Timeout**: Gemini can take 30-60s for complex CSS generation, set timeout accordingly

## Prompt Templates

### Quick Review (get suggestions only)
```
你是资深 UI/UX 设计师。审阅这个 CSS，给出 5-8 条具体改进建议。
重点：层次感、色彩、交互反馈、组件一致性、排版。
直接给结论和代码片段，不要废话。用中文。
```

### Full Rewrite (get complete CSS)
```
你是资深前端工程师。基于以下审阅意见，直接输出一份完整优化后的 CSS 文件。
保留所有类名和变量名。只输出纯 CSS，不要解释。
```

### Style Transfer (match a reference)
```
你是资深 UI/UX 设计师。我要把这个 CSS 改成 [目标风格] 的风格。
参照 [参考项目/网站] 的设计语言。直接输出完整替换 CSS。
```

## Example Session

```
User: 让 Gemini review 一下 Wiki 的 UI

Jarvis:
1. Chrome MCP 截图 index/health/entity 页面
2. cat style.css | gemini -p "[review prompt]" → 收到 6 条建议
3. 展示建议给用户，确认后：
4. cat style.css | gemini -p "[rewrite prompt with suggestions]" → 完整 CSS
5. Write CSS → rebuild → deploy → 截图对比
```
