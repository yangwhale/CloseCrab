---
name: study-material
description: 从 PDF/文档/图片中提取全部内容，整理成结构化 HTML 学习资料，并录入 Study Wiki。当用户说"提取文档"、"整理资料"、"学习资料"、"提取PDF"、"帮我整理这个文档"、"extract document"、"study material"，或上传 PDF/文档并要求提取内容时触发。
---

# 学习资料提取、整理与知识录入

从 PDF、文档、图片中 **一字不漏** 提取全部内容，整理成高质量 HTML 学习文档，最后录入 Study Wiki 形成可检索的知识网络。

## 完整工作流（三阶段）

```
PDF/文档 ──→ [阶段一] 逐页提取 + 页面截图 ──→ [阶段二] HTML 生成 ──→ [阶段三] Wiki 录入
              (Read + pdftoppm)               (CC Pages)             (Study Wiki)
```

---

## 阶段一：内容提取

### 1.1 接收输入

支持的输入格式：
- **PDF 文件**：用 Read 工具读取（支持指定页码范围，大 PDF 分批读取，每次最多 20 页）
- **图片文件**：用 Read 工具直接读取（PNG/JPG/JPEG/WEBP），自动 OCR 识别
- **文本文档**：直接读取 Markdown/TXT 等
- **网页 URL**：用 WebFetch 或 Tavily 提取

### 1.2 逐页提取原则

**核心原则：一字不漏，忠实原文**

- 逐页/逐图读取，不跳过任何内容
- 大 PDF 分批读取：`pages: "1-20"`，`pages: "21-40"`... 直到读完
- 图片中的文字全部 OCR 提取
- 表格保持原始结构
- 数学公式、特殊符号准确还原
- 图表、示意图：原始 PDF 页面截图已嵌入，文字提取作为补充说明
- **填空题答案**：原文中手写或填写的答案要提取出来，用红色标注 `<span class="answer">答案</span>`
- 提取完成后，对照原文检查是否有遗漏

### 1.3 图片处理 — PDF 原页截图

**核心要求：每一页都要嵌入原始 PDF 页面截图，用户能直接看到教材原图。**

#### 1.3.1 PDF 转图片

使用 `pdftoppm` 将 PDF 每页转为 PNG（200 DPI，清晰且文件大小合理）：

```bash
mkdir -p /gcs/cc-pages/assets/study-{topic}
pdftoppm -png -r 200 /path/to/input.pdf /gcs/cc-pages/assets/study-{topic}/page
```

输出文件：`page-01.png`、`page-02.png`... 自动编号。

验证图片可访问：
```bash
curl -s -o /dev/null -w "%{http_code}" https://www.closecrab.com/assets/study-{topic}/page-01.png
# 应返回 200
```

#### 1.3.2 嵌入 HTML

每个 page-card 在 `<span class="page-num">` 之后、`<h3>` 之前插入图片：

```html
<div class="page-card"><span class="page-num">P8</span>
<img class="page-img" src="/assets/study-{topic}/page-08.png" alt="Page 8" loading="lazy">
<h3>标题</h3>
<p>文字提取内容...</p>
</div>
```

合并页面（如 P12–13）插入多张图片：
```html
<div class="page-card"><span class="page-num">P12–13</span>
<img class="page-img" src="/assets/study-{topic}/page-12.png" alt="Page 12" loading="lazy">
<img class="page-img" src="/assets/study-{topic}/page-13.png" alt="Page 13" loading="lazy">
<h3>标题</h3>
```

CSS 样式（已含在标准模板中）：
```css
.page-img{width:100%;border-radius:12px;margin:10px 0 14px;border:1px solid var(--card-border);display:block}
```

#### 1.3.3 批量插入脚本

对于大文档，用 Python 脚本自动在每个 page-card 中插入对应图片，避免手动逐个编辑。脚本解析 `<span class="page-num">PX</span>` 中的页码，映射到 `page-XX.png`。

#### 1.3.4 其他

- `loading="lazy"` 必须加——62 页图片不能同时加载
- 图片模糊时标注 `[此处原文模糊，可能为：xxx]`
- `<div class="image-desc">` 仅在需要**补充**描述图片细节时使用（作为图片的注释，不是替代品）

---

## 阶段二：HTML 生成

### 2.1 输出路径与命名

```
$CC_PAGES_WEB_ROOT/pages/study-{topic}-{YYYYMMDD-HHmmss}.html
```

示例：`study-plant-reproduction-20260501-150000.html`

### 2.2 视觉风格

**暗色 Glassmorphism 主题**（与 page-style skill 的亮色主题不同，学习资料专用暗色）

核心 CSS 变量：
```css
:root {
  --bg: #0f1a12;                          /* 深色背景（色调随学科调整） */
  --card: rgba(255,255,255,0.06);         /* 毛玻璃卡片 */
  --card-border: rgba(255,255,255,0.12);
  --text: #e8f5e9;                        /* 主文字 */
  --text2: #a5d6a7;                       /* 副文字 */
  --accent: #4caf50;                      /* 主强调色（随学科调整） */
  --highlight: #fff9c4;                   /* 高亮色 */
  --formula-bg: rgba(33,150,243,0.08);    /* 公式背景 */
  --formula-border: #42a5f5;              /* 公式左边框 */
}
```

#### 学科色板参考

| 学科 | --bg | --accent | --text/--text2 | 适用场景 |
|------|------|----------|----------------|----------|
| 生物/植物 | `#0f1a12` 深绿黑 | `#4caf50` 绿 | `#e8f5e9`/`#a5d6a7` | 植物、生态、细胞 |
| 健康/人体 | `#14100f` 暖深棕 | `#ff8f00` 琥珀 | `#fff3e0`/`#ffcc80` | 青春期、人体系统 |
| 数学 | `#0f1220` 深蓝黑 | `#42a5f5` 蓝 | `#e3f2fd`/`#90caf9` | 数学、几何 |
| 语文 | `#1a1210` 暖褐 | `#d4a574` 棕 | `#efebe9`/`#bcaaa4` | 中文、英文、文学 |
| 物理/化学 | `#0f1018` 深紫黑 | `#ab47bc` 紫 | `#f3e5f5`/`#ce93d8` | 物理、化学 |

#### 多强调色（分类色编码）

当内容有明确的分类维度（如性别、阵营、对比组）时，增加辅助强调色：

```css
/* 示例：健康教育的性别色编码 */
--accent-male: #42a5f5;     /* 男性主题 — 蓝色 */
--accent-female: #f06292;   /* 女性主题 — 粉色 */

.tag-male { background: rgba(66,165,245,.15); color: var(--accent-male); }
.tag-female { background: rgba(240,98,146,.15); color: var(--accent-female); }
.knowledge-card.male h3 { border-bottom-color: var(--accent-male); color: var(--accent-male); }
.knowledge-card.female h3 { border-bottom-color: var(--accent-female); color: var(--accent-female); }
```

适用场景：男/女生殖系统对比、植物/动物对比、正方/反方辩论等。

### 2.3 核心 CSS 组件

| 组件 | class | 用途 |
|------|-------|------|
| 页面卡片 | `.page-card` | Tab 1 逐页内容，含 `.page-num` 页码标签 |
| 页面图片 | `.page-img` | 原始 PDF 页面截图，`width:100%;border-radius:12px;loading:lazy` |
| 知识卡片 | `.knowledge-card` | Tab 2 知识整理，每个知识点一个 card |
| 练习区 | `.exercise` | Tab 3 练习题分组 |
| 高亮 | `.highlight` | 重点内容黄色渐变高亮 |
| 公式框 | `.formula-box` | 蓝色左边框 + 浅蓝背景，放关键公式/规则 |
| 图片描述 | `.image-desc` | 虚线边框 + 斜体，描述图表内容 |
| 目录 | `.toc` | 带锚点链接的目录导航 |
| 答案 | `.answer` | 填空答案红色标注 |
| 答案展开 | `<details>` | 练习题答案默认隐藏，点击展开 |
| 词汇网格 | `.vocab-grid` | 双栏词汇表，`grid-template-columns:1fr 1fr` |
| 分类标签 | `.tag-male`/`.tag-female` 等 | 分类维度的彩色标签（见多强调色） |

### 2.4 三 Tab 结构（纯 CSS 切换，无 JS）

使用 `input[type=radio]` + `:checked` 兄弟选择器实现 Tab 切换：

```html
<input type="radio" name="tab" id="tab1" checked>
<input type="radio" name="tab" id="tab2">
<input type="radio" name="tab" id="tab3">

<div class="tab-nav">
  <label for="tab1">📖 逐页原文稿</label>
  <label for="tab2">🧠 知识整理</label>
  <label for="tab3">🗺️ 思维导图 & 练习</label>
</div>

<div class="tab-content">
  <div class="t1">...</div>  <!-- Tab 1 -->
  <div class="t2">...</div>  <!-- Tab 2 -->
  <div class="t3">...</div>  <!-- Tab 3 -->
</div>
```

CSS 切换逻辑：
```css
input[name="tab"] { display: none }
.tab-content > div { display: none }
#tab1:checked ~ .tab-content .t1 { display: block }
#tab2:checked ~ .tab-content .t2 { display: block }
#tab3:checked ~ .tab-content .t3 { display: block }
```

#### Tab 1：逐页原文稿

**目的**：让用户能逐页核对提取是否正确和完整，这是校验质量的唯一依据。

- 每一页单独一个 `.page-card`，标注页码 `P1`、`P2`...
- 忠实还原原文文字
- 填空答案用红色 `<span class="answer">` 标注
- 图表/示意图：通过原始 PDF 页面截图展示，文字提取作为可搜索内容
- **关键：一字不漏，包括页码、标题、正文、题目、选项**
- **相邻短页合并**：当 2-3 个连续页面内容很少（如标题页 + 正文页，或纯图片页 + 说明页），合并为一个 card，页码标为 `P12–13`。减少视觉碎片，但不遗漏内容
- 每页 card 结构（必须包含原始 PDF 页面截图）：
  ```html
  <div class="page-card">
    <span class="page-num">P1</span>
    <img class="page-img" src="/assets/study-{topic}/page-01.png" alt="Page 1" loading="lazy">
    <h3>页面标题</h3>
    <div class="text-content">
      <!-- 逐字还原的内容（文字提取，作为图片的可搜索补充） -->
    </div>
  </div>
  ```

#### Tab 2：知识整理

**目的**：学习复习用的精华版，按知识点重新组织。

要求：
- **开头有目录** `.toc`，列出所有知识 section 的锚点链接
- 按章节分 section → 每个知识点一个 `.knowledge-card`
- 合并相关页面内容（同一知识点可能分布在多页）
- 添加对比表格（如风媒花 vs 蟲媒花）
- 添加 `.formula-box` 放关键规则和记忆口诀
- 每个 card 末尾加 **💡 连结思考** 链接到相关 section
- 包含英中词汇表，用 `.vocab-grid` 双栏网格展示（按分类分组：花的部分、种子结构等）
  ```html
  <div class="vocab-grid">
    <div>Testis 睾丸</div><div>Ovary 卵巢</div>
    <div>Sperm 精子</div><div>Egg / Ovum 卵子</div>
  </div>
  ```
- 包含常见易错概念对比（❌ 错误认知 vs ✅ 正确认知）
- **知识点数量要丰富**：一个 60+ 页 PDF 应有 15-20 个 section，30 页左右 8-12 个

#### Tab 3：思维导图 & 练习题

**目的**：帮助巩固和检测学习效果。

**思维导图**：
- 用 inline SVG 绘制（不用外部库）
- 中心主题 → 分支 → 子分支的树状结构
- 不同分支用不同颜色系，颜色应与学科色板和多强调色一致
- 圆角矩形节点 `rx="18"`，连接线条
- **分支超过 6 个时必须加图例**（Legend），用小色块 + 文字标注每个分支的类别
- SVG 尺寸建议：宽 1000-1200px，高度根据分支数调整（6 分支 500px，10+ 分支 700px）
- 中心节点使用主强调色 `--accent`，分支按类别分色

**练习题要求**（题量要丰富，覆盖所有知识点）：

大文档（50+ 页）标准题量：
- 填空题 25 题（从原文提取，中英双语）
- 判断题 15 题（✅/❌）
- 选择题 12 题（A/B/C/D）
- 配对题 3 组（左右匹配）
- 排序题 3 组（按正确顺序排列）
- 分类题 3 组（归类练习）
- 看图标注题 2 题（用 SVG 画结构图 + 填标签）
- 简答题 8 题
- **总计约 65-70 题**

中等文档（20-50 页）：按比例缩减至 40-50 题
小文档（<20 页）：25-35 题，可省略看图标注和排序题
- 每题答案用 `<details><summary>查看答案</summary><div class="ans">答案</div></details>` 隐藏

### 2.5 生成策略

对于大文档（1000+ 行 HTML），采用分段生成策略：

1. 先生成 HTML head + CSS + Tab 1（逐页原文稿）
2. 保存为临时文件
3. 单独生成 Tab 2（知识整理）和 Tab 3（练习题）
4. 合并所有部分

如果需要增强某个 Tab：
1. 用 `head -N original.html` 提取要保留的部分
2. 将增强内容写入临时文件
3. 用 `cat` 合并

### 2.6 打印适配

```css
@media print {
  body { background: #fff; color: #222; }
  .tab-nav, input[name="tab"] { display: none !important; }
  .tab-content > div { display: block !important; }  /* 打印时显示所有 Tab */
  .page-card, .knowledge-card, .exercise { break-inside: avoid; }
}
```

### 2.7 响应式设计

```css
@media (max-width: 600px) {
  .container { padding: 12px 8px 40px; }
  h1.main-title { font-size: 1.4rem; }
  .tab-nav label { padding: 8px 14px; font-size: 0.85rem; }
}
```

### 2.8 发布

1. 生成 HTML 到 `$CC_PAGES_WEB_ROOT/pages/`
2. 发送链接 `$CC_PAGES_URL_PREFIX/pages/study-{topic}-{timestamp}.html`（**不带引号**）
3. 汇报：提取了多少页、多少字、多少个知识点、多少道练习题

---

## 阶段三：Wiki 录入

将 HTML 中整理好的知识录入 Study Wiki（`~/my-wiki-study/`），形成可检索、互联的知识网络。

### 3.1 Wiki 路径

```
WIKI_REPO=~/my-wiki-study
WIKI_CONTENT=~/my-wiki-study/content
WIKI_URL=https://www.closecrab.com/wiki-study
```

### 3.2 录入流程

1. **创建 source 页面**（1 个）
   - 路径：`content/sources/{slug}.md`
   - 这是教材/文档的主页面，链接到所有相关概念
   - 包含完整的知识点梳理（从 Tab 2 提炼）
   - Slug 命名：`{topic}-{source}-{date}`，如 `plant-reproduction-ngs-20260501`
   - 在末尾添加 HTML 学习资料链接

2. **创建 concept 页面**（多个）
   - 路径：`content/concepts/{slug}.md`
   - 每个核心概念一个独立页面
   - 示例：`flower-structure.md`、`pollination.md`、`fertilization.md`、`seed-structure.md`、`germination.md`、`seed-dispersal.md`
   - 包含：定义、详细内容、对比表格、易错点、相关链接

3. **创建 entity 页面**（按需）
   - 路径：`content/entities/{slug}.md`
   - 教材中反复出现的重要实体（如向日葵、蒲公英）
   - 只创建有足够内容支撑的实体

4. **确保所有 wikilink 有对应页面**
   - 录入完成后检查：所有 `[[slug]]` 引用的页面必须存在
   - 不要留下断链

### 3.3 Wiki 页面模板

```markdown
---
title: "中文标题 English Title"
description: "一句话摘要（用于搜索结果展示和 OG 描述）"
type: source | concept | entity
date: YYYY-MM-DD
tags:
  - biology
  - plants
aliases:
  - 别名1
---

## 定义

**术语 English Term** = 一句话定义。

## 详细内容

使用 [[wikilinks]] 链接到其他页面。
表格、列表用标准 Markdown。

## 易错点

⚠️ 易混淆的概念对比

## 相关

- [[other-concept]] — 一句话说明关系
- [[source-page]] — 完整教材来源
```

### 3.4 内容质量要求

- **双语标题**：`"中文标题 English Title"`
- **Wikilinks 互联**：每个概念页面应链接到相关概念和来源页面
- **表格优先**：对比性内容用表格呈现（如风媒花 vs 蟲媒花特征对比）
- **易错点必须有**：用 ⚠️ 标注常见错误认知
- **记忆口诀**：如果有助于记忆，加入口诀（如"动吃黏、风飘轻、水漂浮、弹爆开"）

### 3.5 构建部署

```bash
bash ~/my-wiki-study/scripts/build-and-sync.sh
```

部署后发送 Wiki 链接（不带引号）：
- 首页：`https://www.closecrab.com/wiki-study/`
- 具体页面：`https://www.closecrab.com/wiki-study/concepts/{slug}`

### 3.6 验证

部署后用浏览器访问关键页面，确认：
- clean URL 能正常访问（无 `.html` 后缀）
- 内链跳转正常
- 知识图谱（Graph）显示互联关系
- 反向链接（Backlinks）正确

---

## 注意事项

- PDF 超过 20 页必须分批读取
- 原文有错别字也保留，可用 `[sic]` 标注
- 数学内容用 Unicode 数学符号或 LaTeX 记法
- **链接不要用引号包裹**——飞书点击会把引号带进 URL 导致 404
- Tab 切换必须用纯 CSS，**不能用 JavaScript**
- 练习题答案默认隐藏，用 `<details>` 实现
- 思维导图用 inline SVG，不依赖外部库
- HTML 文件 CSS 必须内联，**唯一的外部资源是 PDF 页面截图**（托管在 GCS `/gcs/cc-pages/assets/study-{topic}/`，通过 `https://www.closecrab.com/assets/` 访问）
- **色彩一致性**：CSS 变量、Tab 2 knowledge-card modifier、Tab 3 思维导图分支色、图例色必须使用同一套色板
- **双语优先**：所有内容保持中英双语（术语附注音标更好），练习题也要中英对照
- **SVG 看图标注题**：必须用完整的器官结构图 + 空白标签框，不能只放文字描述
- Wiki 页面的 `type` 字段只能是：source、concept、entity、analysis
- Wiki 页面 frontmatter 必须有：title、type、date、tags
