# HTML Template Design Reference

CSS component library for the multimodal explainer document. All components are bundled in `assets/html-template.html` ready to clone.

## Design System

**Style**: Google Cloud Material Design (Chris's preference — not glassmorphism, not gradient hell, not dark mode).

**Fonts**:
- `Google Sans` (UI / headings)
- `PingFang SC`, `Microsoft YaHei` (CJK fallback)
- `Roboto Mono` (code, ASCII diagrams)

**Color palette** (CSS variables):
```css
--primary: #1A73E8;        /* Google blue */
--primary-light: #E8F0FE;
--primary-dark: #0F4FAB;
--text: #202124;
--text-2: #5F6368;         /* secondary text */
--border: #DADCE0;
--bg: #FFFFFF;
--bg-alt: #F8F9FA;         /* card alt background */
--bg-code: #F1F3F4;
--success: #1E8E3E;
--success-light: #E6F4EA;
--warning: #F9AB00;
--warning-light: #FEF7E0;
--warning-dark: #B06000;
--error: #D93025;
--error-light: #FCE8E6;
--error-dark: #A50E0E;
--purple: #7627bb;         /* for analogies */
--purple-light: #F3E8FD;
```

## Layout

Two-column grid: 240px sticky sidenav + content column. Mobile collapses to single column.

```html
<div class="layout">
  <nav class="sidenav">...</nav>
  <div class="content">
    <div class="hero">...</div>
    <div class="card" id="sec-0">...</div>
    <div class="card" id="sec-1">...</div>
    ...
  </div>
</div>
```

## Components

### Hero block (top of page)

```html
<div class="hero" id="hero">
  <h1>{Title}</h1>
  <div class="subtitle">{One-line description}</div>
  <div class="hero-meta">
    <div class="hero-meta-item"><span class="label">受众</span><span class="value">{audience}</span></div>
    ... (3-4 meta items)
  </div>
</div>
```

### Voice overview (full index, after hero)

```html
<div class="voice-overview">
  <h3>🎧 {N} 段语音讲解 · 总时长约 {total} 分钟</h3>
  <p>{Listening guidance: order, splits, total duration}</p>
  <div class="ov-list">
    <div class="ov-item">
      <span class="ov-num">0</span>
      <a href="#sec-0" class="ov-name">{Section name}</a>
      <span class="ov-dur">{1m53s}</span>
    </div>
    ...
  </div>
</div>
```

### Section card

```html
<div class="card" id="sec-N">
  <h2>{N}. {Section title}</h2>
  <div class="card-desc">{One-paragraph what this section covers}</div>

  <!-- Audio player goes here -->
  <div class="voice-player">...</div>

  <!-- Subsections -->
  <h3 id="sec-N-1">{N.1} {Subsection title}</h3>
  <p>...</p>
</div>
```

### Voice player (per section)

```html
<div class="voice-player">
  <div class="voice-label">
    <span class="voice-icon">🎧</span>
    <span class="voice-title">语音讲解 · {section name}</span>
    <span class="voice-duration">{2m46s}</span>
  </div>
  <audio controls preload="none">
    <source src="voice/{filename}.ogg" type="audio/ogg">
    您的浏览器不支持 audio。
  </audio>
</div>
```

**Key attributes**:
- `preload="none"` — don't auto-download, only load when user clicks play (saves bandwidth on long pages)
- `<source>` element instead of `src=` attribute — allows multi-format fallback if needed
- Voice files live in `voice/` subdirectory under the HTML — same domain so IAP cookie works seamlessly

### Term box (concept definition)

```html
<div class="term">
  <span class="term-name">中文术语
    <span class="term-en">(English Name, 中文翻译)</span>
  </span>
  <div class="term-def">{One-paragraph definition}</div>
  <div class="term-example">{Concrete example, prefixed automatically with "例: "}</div>
</div>
```

### Callout boxes (highlights)

```html
<!-- Blue: tip / recommendation -->
<div class="callout tip"><strong>建议</strong>：...</div>

<!-- Amber: warning / caveat -->
<div class="callout warn"><strong>注意</strong>：...</div>

<!-- Green: fact / confirmed data -->
<div class="callout fact"><strong>实测数据</strong>：...</div>

<!-- Red (rare): critical / blocker -->
<div class="callout red"><strong>风险</strong>：...</div>
```

### Analogy box (purple, for类比)

```html
<div class="analogy">
  像把保时捷的引擎换装到丰田车架上 — 外形是丰田，动力是保时捷。
</div>
```

The `::before` pseudo-element auto-prepends "💡 类比 — ".

### Flow diagram (ASCII art)

```html
<div class="flow-diagram">
原始数据 (T 级 token)
              │
              ▼
   ┌──────────────────────────┐
   │  阶段 1: 预训练 (PT)      │
   └──────────────────────────┘
              │
              ▼
   ...
</div>
```

Uses `white-space: pre` + Roboto Mono. Centered with `text-align: center`.

### Tables

Standard `<table>` with `<thead>` / `<tbody>`. Uses var(--border) for separators, var(--bg-alt) hover. Compare-two-stacks tables are common (e.g. NVIDIA vs Google).

### Pills (inline badges)

```html
<span class="pill green">LIKELY</span>
<span class="pill amber">PARTIAL</span>
<span class="pill red">AT RISK</span>
<span class="pill blue">DISPUTED</span>
<span class="pill purple">SPECIAL</span>
<span class="pill gray">UNKNOWN</span>
```

### Code

Inline: `<code>` (auto styled with --bg-code + --purple text)
Block: `<pre>` (multi-line code blocks)

## Sidenav

```html
<nav class="sidenav">
  <h3>导览</h3>
  <a href="#hero" class="section">🏠 引子</a>
  <a href="#sec-0" class="section">0. {Top-level}</a>
  <div class="divider"></div>
  <a href="#sec-1" class="section">1. {Section}</a>
  <a href="#sec-1-1">1.1 {Subsection}</a>     <!-- indented, no .section -->
  <a href="#sec-1-2">1.2 ...</a>
  <div class="divider"></div>
  <a href="#sec-2" class="section">2. ...</a>
  ...
</nav>
```

`.section` class is bold + indented less; non-section anchors are sub-items.

## Responsive

Mobile breakpoint at `900px`:
- Sidenav becomes block (not sticky)
- Cards reduce padding
- Hero meta grid wraps single column

Print breakpoint:
- Sidenav hidden
- Cards lose shadow, gain border for page-break clarity
- Reduce font to 12px

## Footer

```html
<div class="card" style="text-align: center; color: var(--text-2); font-size: 12px; padding: 20px;">
  文档创建于 {date} · 配套 <a href="...">相关文档</a> · {分发说明}
</div>
```
