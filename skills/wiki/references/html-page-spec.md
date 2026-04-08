# Wiki 页面 HTML 规范

Bot 生成或更新 Wiki 页面时，遵循以下 HTML 结构规范。

## Meta 标签（必须）

每个 Wiki 页面的 `<head>` 必须包含：

```html
<meta name="wiki-type" content="concept">           <!-- source|entity|concept|analysis -->
<meta name="wiki-tags" content="ai,knowledge,llm">  <!-- 逗号分隔标签 -->
<meta name="wiki-created" content="2026-04-07">
<meta name="wiki-updated" content="2026-04-07">
<meta name="wiki-sources" content="3">               <!-- 引用来源数 -->
<meta name="wiki-links-to" content="rag,memex">      <!-- 出链页面 slug -->
```

## HTML 结构模板

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{页面标题} — CC Wiki</title>
  <!-- wiki-* meta tags -->
  <!-- OG tags for sharing -->
  <link rel="stylesheet" href="../style.css">
</head>
<body>
  <nav class="wiki-nav">
    <a href="../index.html">Index</a>
    <a href="../search.html">Search</a>
    <a href="../graph.html">Graph</a>
    <a href="../log.html">Log</a>
  </nav>

  <article class="wiki-content">
    <header data-pagefind-ignore>
      <div class="wiki-meta">
        <span class="wiki-type" data-type="{type}" data-pagefind-filter="type">{TYPE}</span>
        <span class="wiki-date">Created: {date} · Updated: {date}</span>
      </div>
      <h1 data-pagefind-meta="title">{标题}</h1>
      <p class="wiki-summary">{一行摘要}</p>
      <div class="wiki-tags">
        <span class="wiki-tag" data-pagefind-filter="tag">{tag}</span>
      </div>
    </header>

    <main>
      <!-- 页面主体内容（Pagefind 自动索引此区域） -->
    </main>

    <section class="wiki-backlinks" data-pagefind-ignore>
      <h3>引用了此页面的页面</h3>
      <ul>
        <li><a href="...">{title}</a></li>
      </ul>
    </section>

    <section class="wiki-sources-list" data-pagefind-ignore>
      <h3>参考来源</h3>
      <ul>
        <li><a href="...">{source title}</a></li>
      </ul>
    </section>

    <!-- 局部图谱（自动生成，显示当前页面的关联关系） -->
    <section class="wiki-local-graph" data-pagefind-ignore>
      <h3>关联图谱</h3>
      <div class="local-graph-container" data-page-slug="{slug}"></div>
    </section>
  </article>

  <footer class="wiki-footer">
    CC Wiki · Maintained by CloseCrab Bot
  </footer>
  <!-- 局部图谱 JS（从共享文件加载） -->
  <script src="../local-graph.js"></script>
</body>
</html>
```

## Pagefind 搜索集成

每个页面自动被 Pagefind 索引。关键属性：
- `data-pagefind-meta="title"` — 在 `<h1>` 上，让搜索结果显示页面标题
- `data-pagefind-filter="type"` — 在 `.wiki-type` 上，支持按类型过滤
- `data-pagefind-filter="tag"` — 在每个 `.wiki-tag` 上，支持按标签过滤
- `data-pagefind-ignore` — 在 header/backlinks/sources-list/local-graph 上，排除非正文内容

## 局部图谱

每个页面底部包含一个以当前页面为中心的关系小图谱（D3.js），显示直接关联的 1 跳邻居节点。
- `data-page-slug="{slug}"` 告诉 JS 当前页面的 slug
- `local-graph.js` 从 `../wiki-data/graph.json` 加载数据并渲染
- 不需要手动维护，rebuild-graph.py 更新 graph.json 后自动生效

## 交叉引用约定

```html
<!-- Wiki 内部链接 -->
<a href="../concepts/rag.html" class="wiki-link">RAG</a>

<!-- 原始资料引用 -->
<a href="../../raw/articles/xxx.html" class="source-ref">[来源]</a>

<!-- 外部链接 -->
<a href="https://..." target="_blank" rel="noopener">外部链接</a>
```

## 特殊标注（Callouts）

```html
<!-- 矛盾标注 -->
<div class="wiki-callout wiki-warning">
  <strong>矛盾</strong>：此观点与 <a href="...">xxx</a> 中的结论冲突。
</div>

<!-- 不确定标注 -->
<div class="wiki-callout wiki-question">
  <strong>待验证</strong>：此数据来源单一，需要更多佐证。
</div>

<!-- 过时标注 -->
<div class="wiki-callout wiki-outdated">
  <strong>可能过时</strong>：较新的来源 <a href="...">xxx</a> 提供了更新数据。
</div>
```
