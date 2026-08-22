# 页面风格：Google Cloud Material Design

所有 HTML 页面（技术报告、对比文档）必须使用 Google Cloud 官方的干净 Material Design 风格。绝对禁止使用 Glassmorphism（毛玻璃）、渐变文字或模糊背景。

## 视觉原则
- 背景: 纯白 #FFFFFF 或极浅灰 #F8F9FA。
- 卡片: 白色背景 #FFFFFF，细边框 #DADCE0，浅阴影 0 1px 2px 0 rgba(60,64,67,0.3)。
- 强调色: Google Blue #1A73E8。
- 字体: Google Sans, Roboto, Arial, sans-serif。
- 图标: 避免滥用 emoji，尽量使用纯文本结构或官方 icon。

## 排版规范
- 页面必须包含目录 (TOC)。
- 标题层级分明，字体加粗，颜色为 #202124。
- 正文颜色 #3C4043，次要文本 #5F6368。
- 卡片内部需要留白充足 (Padding 24px+)。

## 图表规范 (SVG)
- 使用平面蓝白灰配色，不使用渐变填充。
- 线条清晰 (stroke width 1-2px)，文字对齐。

## 站点图标（favicon）—— 每个 HTML 文档都必须有

**不带 favicon 的页面，浏览器标签上是一个黑白地球，看着像半成品。**
所有自己产出的 HTML 一律在 `<head>` 里加这一行，紧跟在 `<title>` 后面：

```html
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🦀</text></svg>">
```

🦀 是 CloseCrab 的标识。**这一行没有任何外部依赖** —— 不引文件、不发请求，
本地 `file://` 打开、GCS 桶、GitHub Pages 全都一样能用，复制粘贴即可。

### 为什么是内联 SVG，不是 PNG 或 ico

**图标由浏览器用本机的 emoji 字体现画。** Mac 上出来的是 Apple Color Emoji
那只红螃蟹，Windows / Android 各是各的版本 —— 在每个系统上都是「原生」的样子。

反例（2026-08-22 实际走过一遍）：先把 Noto Color Emoji 的 🦀 渲染成 PNG 发出去，
等于把 Google 那只偏橙、腿很细的字形**钉死在所有平台**，在 Mac 上一眼就不对。
**不要烤字形。**

### 只有这两种情况才额外放位图文件

| 场景 | 要什么 | 原因 |
|---|---|---|
| 整站（多页面、别人也会做新页） | 站点根目录放 `favicon.ico` | 浏览器不看 link 标签时会自动去根目录要，一次覆盖全站 |
| iOS 存到桌面 | `apple-touch-icon.png`（180×180） | iOS 那个位置**不认 SVG favicon**，必须是位图 |

这两个是回落路径，主路径永远是上面那行内联 SVG。
真要生成位图，从 `NotoColorEmoji.ttf` 渲染 —— 那是 CBDT 位图字体，
PIL 只接受 109 这个 strike 尺寸，先渲染再上采样；按字形实际 bbox 裁紧，
边距别超过 2%（螃蟹宽大于高，留白多了 16px 下就糊成一团）。

### 强制点

`scripts/publish-cc-page.sh` 会在上传前检查，缺了就**当场补进源文件**并打一行提示。
手写 HTML 或往 GitHub Pages 推的时候没有这道关卡，**靠自觉**。
