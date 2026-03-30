---
name: page-style
description: 默认 HTML 页面风格规范。生成报告、对比文档、技术页面时自动套用此风格。亮色 glassmorphism 主题 + SVG 图表。
---

# 默认页面风格：亮色 Glassmorphism

生成 HTML 报告/文档时的默认视觉风格。所有通过 issue-handler、手动生成报告等场景产出的 HTML 页面，都应遵循此规范。

## 设计语言

**亮色主题 + Glassmorphism（毛玻璃）+ SVG 图表**

### 色彩体系

```css
:root {
  /* 页面背景 */
  --bg-page: #f0f2f5;

  /* 毛玻璃效果 */
  --glass-bg: rgba(255,255,255,0.55);
  --glass-border: rgba(255,255,255,0.7);
  --glass-shadow: 0 8px 32px rgba(0,0,0,0.08);
  --glass-blur: blur(20px);

  /* 文字 */
  --text-primary: #1a1a2e;
  --text-secondary: #5a5a7a;
  --text-muted: #8a8aaa;

  /* 品牌色（按需替换） */
  --accent-blue: #4285F4;    /* Google Blue */
  --accent-green: #76B900;   /* NVIDIA Green */
}
```

### 背景装饰

页面背景不能是纯色——需要有 **渐变色块 (blobs)** 让 `backdrop-filter: blur()` 有东西可模糊：

```css
/* 至少 2-3 个固定位置的径向渐变色块 */
body::before {
  content: '';
  position: fixed;
  top: -200px; left: -100px;
  width: 600px; height: 600px;
  background: radial-gradient(circle, rgba(主色,0.12) 0%, transparent 70%);
  border-radius: 50%;
  z-index: 0;
  pointer-events: none;
}
body::after {
  content: '';
  position: fixed;
  bottom: -200px; right: -100px;
  width: 700px; height: 700px;
  background: radial-gradient(circle, rgba(副色,0.10) 0%, transparent 70%);
  border-radius: 50%;
  z-index: 0;
  pointer-events: none;
}
/* 可选：中间区域第三个色块用额外 div */
```

### 毛玻璃卡片（核心）

所有内容容器（卡片、表格、图表、分析框）统一使用毛玻璃效果：

```css
.glass {
  background: var(--glass-bg);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  border: 1px solid var(--glass-border);
  border-radius: 16px;
  box-shadow: var(--glass-shadow);
}
```

关键参数：
- `border-radius: 16px`（统一圆角）
- `backdrop-filter: blur(20px)`（模糊强度）
- 背景透明度 `0.55`（不能太高否则看不到模糊效果，不能太低否则文字不清晰）
- 鼠标悬停可选 `transform: translateY(-2px)` 微上浮效果

### Hero 区域

```css
.hero {
  background: linear-gradient(135deg, rgba(主色,0.08) 0%, rgba(255,255,255,0.9) 40%, rgba(副色,0.06) 100%);
  backdrop-filter: blur(10px);
  border-bottom: 3px solid;
  border-image: linear-gradient(90deg, 主色, 副色) 1;
  padding: 60px 0 48px;
  text-align: center;
}
```

- 标题用 `background-clip: text` 渐变文字
- 副标题用 `--text-secondary`
- Badge 用半透明背景 + 品牌色边框

### 段落标题

```css
section h2 {
  font-size: 1.8rem;
  padding-left: 16px;
  border-left: 4px solid;
  border-image: linear-gradient(180deg, 主色, 副色) 1;
}
```

### 表格

- 外层容器套 `.glass` 效果
- 表头背景 `rgba(主色, 0.06)`
- 行悬停 `rgba(主色, 0.04)`
- 分组标题行 `rgba(主色, 0.06)` + 粗体
- 胜出单元格用对应品牌色浅底 `rgba(色,0.08)` + 深色文字

### 标注框 (Callout)

```css
.callout {
  padding: 16px 20px;
  border-radius: 12px;
  border-left: 4px solid 品牌色;
  background: rgba(品牌色, 0.08);
  backdrop-filter: var(--glass-blur);
}
```

## 图表规范

### SVG 柱状图（默认）

**不要用 CSS div 做柱状图**，统一使用 SVG 内联：

```html
<div class="svg-chart-wrapper"> <!-- 毛玻璃容器 -->
  <svg viewBox="0 0 700 100" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;">
    <defs>
      <linearGradient id="grad1" x1="0%" y1="0%" x2="100%" y2="0%">
        <stop offset="0%" style="stop-color:#深色"/>
        <stop offset="100%" style="stop-color:#亮色"/>
      </linearGradient>
    </defs>
    <!-- 标签 -->
    <text x="70" y="28" text-anchor="end" fill="品牌色" font-size="13" font-weight="600">标签A</text>
    <!-- 柱子 -->
    <rect x="80" y="10" width="按比例" height="28" rx="6" fill="url(#grad1)" opacity="0.9"/>
    <!-- 数值 -->
    <text x="柱子右侧" y="30" fill="深色" font-size="12" font-weight="700">数值</text>
  </svg>
</div>
```

要点：
- `viewBox="0 0 700 100"` 每组双柱图
- 柱子高度 `28px`，间距 `44px`
- `rx="6"` 圆角
- 渐变填充 `linearGradient`
- 标签在左侧 `text-anchor="end"`，数值在柱子右侧
- 最大值的柱子宽度 `520px`，其他按比例缩放

## 排版

- 字体：`'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`
- 行高：`1.7`
- 最大宽度：`1200px`
- 内边距：`0 24px`
- 正文字号：`0.92-0.93rem`
- 卡片网格：`grid-template-columns: repeat(auto-fit, minmax(340px, 1fr))`

## 打印适配

```css
@media print {
  body { background: #fff; }
  body::before, body::after, .bg-blob-mid { display: none; }
  .glass, .card, .analysis-box {
    backdrop-filter: none;
    background: #f8f8fc;
    border-color: #ddd;
    box-shadow: none;
  }
}
```

## Favicon（必须）

所有 CC Pages 页面统一使用 🦀 螃蟹 favicon：

```html
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🦀</text></svg>">
```

## OG 标签（必须）

每个页面都要有 OG 标签，用于 Discord / 飞书 / 社交分享预览：

```html
<meta property="og:title" content="页面标题">
<meta property="og:description" content="简短描述">
<meta property="og:image" content="$CC_PAGES_URL_PREFIX/assets/og-{topic}.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="theme-color" content="#4285F4">
```

## 可点击术语解释（Explain Term）

技术文档中的专业术语，可以做成点击弹出解释的交互：

### 样式
```css
.explain-term {
  border-bottom: 1.5px dashed var(--accent);
  cursor: pointer;
  transition: background 0.2s;
}
.explain-term:hover {
  background: rgba(37,99,235,0.08);
  border-bottom-style: solid;
}
```

### 用法
```html
<!-- 在正文中标记术语 -->
<span class="explain-term" data-explain="term-id">术语名称</span>

<!-- JS 中定义解释内容 -->
const EXPLANATIONS = {
  'term-id': {
    title: '术语标题',
    body: '<p>详细解释，支持 HTML 格式...</p>'
  }
};
```

### 弹窗
- 毛玻璃背景 `rgba(255,255,255,0.92)` + `backdrop-filter: blur(24px)`
- 圆角 16px，最大宽度 640px，最大高度 80vh 可滚动
- 支持 ESC 键关闭、点击背景关闭、X 按钮关闭
- 标题用 `var(--accent)` 色 + 底部分隔线

### 设计原则
- 表格/正文保持精简，详细解释移到弹窗
- 同一术语可在多处标记，共享同一个解释
- 弹窗内支持代码块、列表、粗体等富文本

## 参考实现

完整的参考页面：`gpu-tpu-pedia/tpu/tpu-v7-vs-b200-20260311.html`

这个文件是此风格规范的标准实现，包含：
- Hero + 渐变标题
- 毛玻璃卡片介绍
- 毛玻璃表格
- SVG 柱状图（6 组）
- 分析文本框
- Callout 标注
- 12 维度总结网格
- 数据来源卡片
- 页脚

可点击术语解释的参考实现：`gpu-to-tpu-guide-20260318.html`（含 7 个术语的完整 explain-term 系统）
