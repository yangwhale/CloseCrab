> ⚠️ **历史参考**：本组件库使用 glassmorphism + 紫色风格，**违反 page-style skill 的 Material Design 偏好**。
> 仅供研究"我们之前踩过什么坑"使用，**新文档不要直接复制**。
> 推荐使用 `css-material-design-lib.md` 作为起点。

# CSS Component Library for Tech Comparison Docs

可复用的 CSS 组件代码片段。复制到新文档中即可使用。

## CSS Custom Properties (必须)

```css
:root {
  --bg: #f6f6f9;
  --surface: rgba(255,255,255,0.72);
  --surface-solid: #fff;
  --border: rgba(0,0,0,0.06);
  --border-strong: rgba(0,0,0,0.1);
  --text: #111827;
  --text-2: #4b5563;
  --text-3: #9ca3af;
  --brand-a: #2563eb;       /* 框架 A 主色 */
  --brand-a-soft: #dbeafe;
  --brand-b: #7c3aed;       /* 框架 B 主色 */
  --brand-b-soft: #ede9fe;
  --shared: #059669;         /* 共享组件 */
  --green: #059669;
  --amber: #d97706;
  --red: #dc2626;
  --radius: 20px;
  --radius-sm: 12px;
  --blur: 28px;
  --max-w: 1140px;
  --font: 'Inter', -apple-system, system-ui, sans-serif;
  --shadow-md: 0 4px 20px rgba(0,0,0,0.05);
}
```

## Grain Texture Overlay

```css
body::before {
  content: '';
  position: fixed; inset: 0; z-index: 9999;
  pointer-events: none; opacity: 0.018;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
}
```

## Ambient Gradient Blobs

```css
.ambient {
  position: fixed; border-radius: 50%;
  pointer-events: none; z-index: 0; filter: blur(100px);
}
.ambient-1 {
  width: 600px; height: 600px; top: -10%; right: -5%;
  background: radial-gradient(circle, rgba(37,99,235,0.06), transparent 70%);
}
.ambient-2 {
  width: 500px; height: 500px; bottom: -5%; left: -8%;
  background: radial-gradient(circle, rgba(124,58,237,0.05), transparent 70%);
}
```

## Glass Card

```css
.glass {
  background: var(--surface);
  backdrop-filter: blur(var(--blur));
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-md);
}
```

## Insight Callout

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
  display: flex; gap: 16px; padding: 24px 28px;
  border-radius: var(--radius); margin: 24px 0;
  background: linear-gradient(135deg, rgba(37,99,235,0.04), rgba(124,58,237,0.04));
  border: 1px solid rgba(37,99,235,0.08);
}
.insight-icon {
  flex-shrink: 0; width: 32px; height: 32px; border-radius: 10px;
  background: linear-gradient(135deg, var(--brand-a), var(--brand-b));
  display: flex; align-items: center; justify-content: center;
  font-size: 0.9rem; color: #fff;
}
.insight-body { font-size: 0.9rem; color: var(--text-2); line-height: 1.65; }
.insight-body strong { color: var(--text); font-weight: 600; }
```

## Performance Number Card

```html
<div class="glass perf-card">
  <div class="perf-num">3.6×</div>
  <div class="perf-context">描述文本</div>
  <div class="perf-source">来源：xxx</div>
</div>
```

```css
.perf-card { padding: 24px; text-align: center; }
.perf-num {
  font-size: 2.2rem; font-weight: 800; letter-spacing: -0.04em;
  background: linear-gradient(135deg, var(--brand-a), var(--brand-b));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.perf-context { font-size: 0.82rem; color: var(--text-2); margin-top: 4px; }
.perf-source { font-size: 0.68rem; color: var(--text-3); margin-top: 8px; }
```

## Status Badges (表格内)

```css
.st-pass { color: var(--green); font-weight: 500; }   /* ✅ 已验证 */
.st-wip  { color: var(--amber); font-weight: 500; }   /* 🔨 开发中 */
.st-no   { color: var(--red); font-weight: 500; }     /* ✗ 不支持 */
.st-unk  { color: var(--text-3); }                     /* ❓ 未知 */
```

## Roadmap Status Pills

```css
.rm-status {
  display: inline-block; font-size: 0.6rem; font-weight: 700;
  padding: 2px 7px; border-radius: 4px; margin-left: 6px;
}
.rm-done { background: #d1fae5; color: #065f46; }
.rm-wip  { background: #fef3c7; color: #92400e; }
.rm-plan { background: #e0e7ff; color: #3730a3; }
```

## Scroll Animation

```javascript
document.addEventListener('DOMContentLoaded', () => {
  const io = new IntersectionObserver((entries) => {
    entries.forEach(e => { if (e.isIntersecting) e.target.classList.add('visible') });
  }, { threshold: 0.06, rootMargin: '0px 0px -30px 0px' });
  document.querySelectorAll('.reveal,.stagger').forEach(el => io.observe(el));
});
```

## SVG Diagram Boilerplate

```svg
<svg viewBox="0 0 960 520" xmlns="http://www.w3.org/2000/svg"
     style="width:100%;max-width:960px;margin:0 auto;display:block;
            font-family:Inter,system-ui,sans-serif">
  <defs>
    <linearGradient id="ga" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#2563eb" stop-opacity="0.08"/>
      <stop offset="100%" stop-color="#2563eb" stop-opacity="0.02"/>
    </linearGradient>
    <filter id="shadow">
      <feDropShadow dx="0" dy="1" stdDeviation="2" flood-opacity="0.06"/>
    </filter>
    <marker id="arrow" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6" fill="none" stroke="#6b7280" stroke-width="1.2"/>
    </marker>
  </defs>

  <!-- 节点模板 -->
  <rect x="50" y="55" width="380" height="42" rx="8"
        fill="white" filter="url(#shadow)"
        stroke="#2563eb" stroke-opacity="0.15" stroke-width="1"/>
  <text x="240" y="77" text-anchor="middle" fill="#2563eb"
        font-size="11" font-weight="600">组件名称</text>
  <text x="240" y="90" text-anchor="middle" fill="#9ca3af"
        font-size="8.5">说明文字</text>
</svg>
```
