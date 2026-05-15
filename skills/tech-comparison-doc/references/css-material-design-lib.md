# CSS Component Library — Material Design

推荐的默认起手套件。遵循 Material Design 规范：纯白背景、浅阴影、无毛玻璃、无渐变文字。

## CSS Custom Properties

```css
:root {
  --bg: #ffffff;
  --bg-subtle: #f8f9fa;
  --surface: #ffffff;
  --border: #dadce0;
  --border-light: #e8eaed;
  --text: #202124;
  --text-2: #5f6368;
  --text-3: #9aa0a6;
  --brand-a: #1a73e8;        /* Google Blue — Framework A */
  --brand-a-light: #e8f0fe;
  --brand-b: #0d9488;        /* Teal — Framework B */
  --brand-b-light: #e6f4f1;
  --shared: #137333;          /* Green — 共享组件 */
  --shared-light: #e6f4ea;
  --green: #137333;
  --amber: #e37400;
  --red: #c5221f;
  --radius: 8px;
  --radius-lg: 12px;
  --max-w: 1140px;
  --font: 'Google Sans', 'Inter', 'Roboto', system-ui, sans-serif;
  --shadow-sm: 0 1px 2px rgba(60,64,67,0.3), 0 1px 3px rgba(60,64,67,0.15);
  --shadow-md: 0 1px 3px rgba(60,64,67,0.3), 0 4px 8px rgba(60,64,67,0.15);
}
```

## Layout

```css
.wrap { max-width: var(--max-w); margin: 0 auto; padding: 0 24px; }
section { margin-bottom: 64px; }
.dual { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 768px) { .dual { grid-template-columns: 1fr; } }
```

## Card（白底 + 1px 边框 + 浅阴影）

```css
.card {
  background: var(--surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: 24px;
}

/* 带顶部强调色条 */
.card.brand-a { border-top: 3px solid var(--brand-a); }
.card.brand-b { border-top: 3px solid var(--brand-b); }
```

## Typography

```css
h1 { font-size: 2rem; font-weight: 700; color: var(--text); letter-spacing: -0.02em; }
h2 { font-size: 1.5rem; font-weight: 600; color: var(--text); margin-bottom: 8px; }
h3 { font-size: 1.1rem; font-weight: 600; color: var(--text); }

.eyebrow {
  font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.1em; color: var(--text-3);
}
.section-lead { font-size: 1rem; color: var(--text-2); max-width: 680px; line-height: 1.7; }
```

## Insight Callout（浅蓝色背景，无毛玻璃）

```html
<div class="insight">
  <div class="insight-icon">💡</div>
  <div class="insight-body">
    <strong>标题。</strong>内容文本。
  </div>
</div>
```

```css
.insight {
  display: flex; gap: 14px; padding: 16px 20px;
  border-radius: var(--radius);
  background: var(--brand-a-light);
  border-left: 3px solid var(--brand-a);
}
.insight-icon { flex-shrink: 0; font-size: 1rem; margin-top: 2px; }
.insight-body { font-size: 0.9rem; color: var(--text-2); line-height: 1.65; }
.insight-body strong { color: var(--text); }
```

## Performance Number Card

```css
.perf-card {
  padding: 24px; text-align: center;
  background: var(--bg-subtle);
  border-radius: var(--radius);
}
.perf-num {
  font-size: 2rem; font-weight: 800;
  color: var(--brand-a);  /* 纯色，不用渐变 */
}
.perf-context { font-size: 0.82rem; color: var(--text-2); margin-top: 4px; }
.perf-source { font-size: 0.68rem; color: var(--text-3); margin-top: 8px; }
```

## Status Badges（表格内）

```css
.st-pass { color: var(--green); font-weight: 500; }   /* ✅ 已验证 */
.st-wip  { color: var(--amber); font-weight: 500; }   /* 🔨 开发中 */
.st-no   { color: var(--red); font-weight: 500; }     /* ✗ 不支持 */
.st-unk  { color: var(--text-3); }                     /* ❓ 未知 */
```

## Table

```css
.tbl-wrap { overflow-x: auto; }
.tbl-wrap table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
.tbl-wrap th {
  text-align: left; padding: 12px 16px;
  font-weight: 600; font-size: 0.75rem;
  text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--text-3); background: var(--bg-subtle);
  border-bottom: 2px solid var(--border);
}
.tbl-wrap td {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-light);
}
.tbl-wrap tr:hover td { background: #f1f3f4; }
.cat-row td {
  font-weight: 600; font-size: 0.72rem;
  text-transform: uppercase; color: var(--text-3);
  background: var(--bg-subtle); padding-top: 18px;
}
```

## Roadmap Pills

```css
.rm-status {
  display: inline-block; font-size: 0.6rem; font-weight: 700;
  padding: 2px 8px; border-radius: 4px; margin-left: 6px;
}
.rm-done { background: var(--shared-light); color: var(--shared); }
.rm-wip  { background: #fef7e0; color: #e37400; }
.rm-plan { background: var(--brand-a-light); color: var(--brand-a); }
```

## SVG Diagram Boilerplate（平面风格，无渐变）

```svg
<svg viewBox="0 0 960 520" xmlns="http://www.w3.org/2000/svg"
     style="width:100%;max-width:960px;font-family:Google Sans,Inter,system-ui,sans-serif">
  <defs>
    <filter id="shadow">
      <feDropShadow dx="0" dy="1" stdDeviation="1.5" flood-opacity="0.1"/>
    </filter>
  </defs>

  <!-- Framework A 节点（蓝色边框 + 白底） -->
  <rect x="50" y="50" width="200" height="44" rx="8"
        fill="#ffffff" filter="url(#shadow)"
        stroke="#1a73e8" stroke-width="1.5"/>
  <text x="150" y="76" text-anchor="middle"
        fill="#1a73e8" font-size="12" font-weight="600">Component A</text>

  <!-- Framework B 节点（teal 边框 + 白底） -->
  <rect x="300" y="50" width="200" height="44" rx="8"
        fill="#ffffff" filter="url(#shadow)"
        stroke="#0d9488" stroke-width="1.5"/>
  <text x="400" y="76" text-anchor="middle"
        fill="#0d9488" font-size="12" font-weight="600">Component B</text>

  <!-- 共享节点（绿色边框 + 浅绿底） -->
  <rect x="175" y="130" width="200" height="44" rx="8"
        fill="#e6f4ea" filter="url(#shadow)"
        stroke="#137333" stroke-width="1.5"/>
  <text x="275" y="156" text-anchor="middle"
        fill="#137333" font-size="12" font-weight="600">Shared Component</text>

  <!-- 连接线（细线 + 小箭头） -->
  <line x1="150" y1="94" x2="275" y2="130"
        stroke="#5f6368" stroke-width="1.2"/>
</svg>
```

## Scroll Animation（可选）

```javascript
const io = new IntersectionObserver((entries) => {
  entries.forEach(e => { if (e.isIntersecting) e.target.classList.add('visible') });
}, { threshold: 0.06 });
document.querySelectorAll('.reveal').forEach(el => io.observe(el));
```

```css
.reveal { opacity: 0; transform: translateY(16px); transition: opacity 0.5s, transform 0.5s; }
.reveal.visible { opacity: 1; transform: translateY(0); }
```
