---
name: ant-tpu-sync
description: 同步蚂蚁 TPU 项目资料库（钉钉知识库 → CC Pages HTML 镜像）。触发词「同步蚂蚁项目 / 同步蚂蚁文档 / 扫一遍蚂蚁库 / 更新蚂蚁资料库 / 蚂蚁 TPU 文档同步」。按钉钉每篇文档的最后编辑时间检测变化，只重新同步更新的文档，并维护一份 hierarchical 总纲。
trigger: 用户说「同步蚂蚁项目/文档/进度」「扫一遍蚂蚁库看哪个更新了」「更新蚂蚁 TPU 资料库」「重新生成蚂蚁总纲」时激活。
---

# ant-tpu-sync — 蚂蚁 TPU 资料库同步

把钉钉知识库（空间 `15eGe6boVpjOez3N`「TPU项目资料库」）整库镜像到 CC Pages，
靠**钉钉每个节点的最后编辑时间**（`contentUpdatedTime`）做增量同步，并维护一份
hierarchical 总纲 `index.html`。

## 架构（关键：这是按需流程，不是常驻 daemon）
钉钉登录态只活在 **gLinux 已登录的 Chrome**（cookie/XSRF 会过期轮换）。所以同步
= jarvis 收到触发词时，通过 chrome-devtools-mcp 驱动那个远程 Chrome 跑一套流程。
对标 `customer-qa-tracker`（procedure skill）。

```
enumerate (API) → diff vs manifest → extract 变化的 doc → clean+render → upload → 更新 manifest → render 总纲 → upload
```

## 存储布局（GCS-backed CC Pages）
```
pages/ant-tpu/index.html          总纲（全节点树 + 双时间戳）
pages/ant-tpu/docs/<slug>.html    每篇文档镜像（adoc/md/code）
pages/ant-tpu/manifest.json       同步状态（同步的心脏）
assets/ant-tpu/files/<name>       pdf/mp4/webm/zip/adraw 二进制
assets/ant-tpu/img/<slug>/...     各 adoc 内嵌图片（本地化，防签名 URL 过期）
```
URL 前缀：`https://cc.higcp.com/pages/ant-tpu/`（IAP，Chris 已登录可看）。
manifest 本地缓存：`~/.cache/ant-tpu-sync/manifest.json`（gsutil 拉 GCS 上那份为准）。

## 同步流程（每步对应 scripts/）

### 0. 前置：确认 gLinux Chrome 登录态
`mcp__chrome-devtools-mcp__list_pages` 看有没有 docs.dingtalk.com 页；没有就
`navigate_page` 到 `https://docs.dingtalk.com/i/nodes/NkDwLng8ZL0yyP3ruampjGd1VKMEvZBY`。
登录失效 → enumerate 返回 `NO_XSRF_COOKIE`，提示 Chris 在 gLinux Chrome 重新登录钉钉。

### 1. enumerate（拿全树 + 最新时间戳）
`evaluate_script` 跑 `scripts/enumerate.js`，`filePath:/tmp/ant-tpu-enum-fresh.json`。
chrome-mcp 在 gLinux，结果在 gLinux，拉回本地：
`ssh glinux 'cat /tmp/ant-tpu-enum-fresh.json' > /tmp/ant-tpu-enum-fresh.json`

### 2. diff（算 new/stale/deleted）
`python3 scripts/diff_manifest.py --enum /tmp/ant-tpu-enum-fresh.json \
  --manifest ~/.cache/ant-tpu-sync/manifest.json --out /tmp/ant-worklist.json`
`to_sync = new + stale`。日常通常只有 2-3 篇。

### 3. extract + render（只处理 to_sync）
- **adoc**：**实战用 raw CDP**（`scripts/cdp_extract.py`，在 gLinux 上跑），不靠
  chrome-devtools-mcp（它会 flaky / 卡）。cdp_extract 自己连 `http://127.0.0.1:9222/json`
  里 docs.dingtalk.com tab 的 webSocketDebuggerUrl（`websocket-client`，**必须
  `suppress_origin=True`** 否则 Chrome 403），在页面上下文建 iframe 读
  `contentDocument.body.innerHTML` + 滚动触发懒加载图 + fetch 图片成 base64。
  输出 `/tmp/ant-extract-batchN.json` + `/tmp/ant-imgs-batchN.json` → ssh 拉回 →
  `process_batch.py`（解 base64 图 → `STAGE/img/<slug>/N.webp` + 建 imgmap + 写 raw/meta
  + shell `render_doc.py --mode adoc`）。⚠️ 必须走 iframe，直接导航 alidocs 会 forbidden。
  - **懒加载图**：已加载用 `<img src>`，未加载存 `data-src`，只在滚动后才有真 src。
    `render_doc._img_hash` + cdp_extract 的 `imgs_from_html()` 用正则
    `/resources/img/([0-9a-f]+)` 抓 raw HTML 里**所有** hash（同时覆盖 src+data-src），
    保证 12 图不漏成 2 图。
- **md / 代码 py/json/yaml/sh**：file 类型，走 §4 文件下载拿 raw →
  `scripts/render_files.py`（自动按 ext 选 md/code 模式 shell render_doc.py，
  LANG 映射 py→python/sh→bash/...）。批量一把梭，输出 `STAGE/docs/<slug>.html`。

### 4. 二进制 + 图片下载（已验证的真实机制）
- **adoc 内嵌图片**：`alidocs.dingtalk.com/core/api/resources/img/...` 需 cookie。
  在 gLinux Chrome 页面上下文 `fetch(url,{credentials:'include'})` → blob →
  FileReader base64 回传（cdp_extract 已含）→ 本地写 webp → gsutil 传
  `assets/ant-tpu/img/<slug>/`。`static.dingtalk.com`/`img.alicdn.com` 公开图可直接 curl。
- **文件下载（md/code/二进制统一走这条）**：`scripts/download_files.py`（gLinux 上跑）。
  机制：Chrome 页面上下文 fetch
  `https://docs.dingtalk.com/box/api/v2/file/download?dentryUuid=<uuid>`
  （带 `x-xsrf-token` + `credentials:include`）→ 返回
  `data.ossUrlPreSignatureInfo.preSignUrls[]` = **自包含签名 OSS URL**（*.trans.dingtalk.com）。
  这些 URL **裸 curl 无需 cookie** 就能下（签名自带鉴权）。multipart 按数组序拼接。
  输出 `/tmp/ant-files/<slug>.<ext>`（slug 含 uuid[:6] 后缀，**防同名文件互相覆盖**——
  库里有 4 组同名不同目录：job.yaml×2 / dashboard.json×2 / setup.sh×2 / create-log-metrics.sh×2）。
  结果写 `/tmp/ant-files-result.json`。`--only-ext pdf,zip` 可筛类型。
  - ⚠️ **adraw 钉钉绘图格式下不了**（file/download 返 403）→ best-effort：manifest 标
    `status:error` + 存 `dingtalk_url`，总纲回落「钉钉原文」链接。
  - gLinux **没有 gsutil/gcloud**：下完 `tar cf - . | ssh→tar xf -` 拉本地，再本地 gsutil 传
    `assets/ant-tpu/files/`（zip 141MB 也一起传，仅给下载链接不内嵌）。

### 5. 更新 manifest + 渲染总纲
`scripts/build_manifest.py --enum /tmp/ant-tpu-enum-fresh.json --files-dir /tmp/ant-files-local
--out manifest.json`：以 enumerate 为树真值，逐节点算 slug → 解析本地产物
（docs/<slug>.html→`local_html` / files/<slug>.<ext>→`local_asset`）+ sha256 + status +
`dingtalk_url`。folder 标 `status:folder`，下不了的标 `error`。
`scripts/render_index.py --manifest manifest.json --out index.html`：全节点 hierarchical
树（folder 优先、按 path 排序、ext 图标、双时间戳、stale/new badge），doc 链 docs/、
binary 链 assets/files/、无本地产物回落 `dingtalk_url`，含纯 JS lightbox（图片点击放大）。

### 6. 发布
gsutil 直传（带下方 compute SA 环境，nested 路径必需）：
docs/ rsync → `pages/ant-tpu/docs/`、index.html + manifest.json → `pages/ant-tpu/`、
files → `assets/ant-tpu/files/`、img → `assets/ant-tpu/img/`。
单文件外发可用 `~/CloseCrab/scripts/publish-cc-page.sh`（自带 URL 校验）。
裸 URL 发 Chris（**绝不加引号/反引号**，见 memory feedback_no-quotes-in-links）。
发布后 file:// 截图肉眼核验（cc.higcp.com 是 IAP，gLinux Chrome 无 IAP cookie 打不开镜像，
但 index 自包含 HTML+CSS 可 file:// 预览）。

## 上传细节
gsutil 必须用可用环境（publish-cc-page.sh 已封装）：
`CLOUDSDK_CORE_ACCOUNT=604327164091-compute@developer.gserviceaccount.com`
`CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false`
目标 bucket：`gs://chris-pgp-host-asia/cc-pages/{pages|assets}/ant-tpu/...`

## 关键常量
- space_id `15eGe6boVpjOez3N` · root_uuid `pGBa2Lm8aGLllAvacMD4Q7v9VgN7R35y`
- docs.dingtalk.com 容器节点 `NkDwLng8ZL0yyP3ruampjGd1VKMEvZBY`
- 文档构成（2026-05-30）：33 adoc + 7 md + 16 代码/配置 = 56 篇可渲染；
  10 二进制（5 pdf + 2 mp4 + 1 webm + 1 zip + 1 adraw）≈ 286MB；25 文件夹。

## 质量自检（精益求精）
渲染后抽查：表格列对齐、图片可见（不是裂图/签名过期）、代码高亮、标题层级。
adoc 清洗后应无 `sc-` styled-component 类、无 `<svg>`、无 `data-cangjie` 残留。
BS4 核验脚本（喂渲染好的 HTML）应满足：裸 div=0、table chrome 文字（`正在移动`）=0、
孤儿序号（`^\d+[.、)]$` 独立文本节点）=0、stray bullet（●○■ 裸字符）=0。

## 钉钉文档渲染引擎（render_doc.py 的可复用知识）

这是「钉钉文档 → 干净语义 HTML」的通用能力，跟蚂蚁项目无关，任何钉钉 adoc 都能用。
钉钉用自研 **cangjie** 富文本编辑器，DOM 满是编辑器私有结构，直接拿 innerHTML 会塌成
文字墙 + 泄漏编辑器工具条文字。`clean_adoc()` 把它清成 Material 模板能渲染的语义 HTML。

### cangjie DOM 模型（清洗的依据）
- `data-cangjie-leaf-block="true"` — 一个**块级逻辑行**（段落/列表项/标题）。真标题已带
  `<h1>`-`<h6>`；其它 leaf-block 是普通文本行。
- `data-cangjie-group-block="true"` — **嵌套容器**；列表缩进深度 = 祖先里这种 block 的个数。
- `data-cangjie-not-editable="true"` — 编辑器 chrome（折叠按钮等），整块删。
- `sc-*` class — styled-component 包裹 div，**不能进 KEEP**，否则正文塌成文字墙。
- `data-testid` — **双重身份**：
  - 内容（保留）：`list` / `group-list-container` / `list-symbol*` / `cangjie-image` /
    `editor-image-real-box` / `link` / `hetu` / `horizon-line` / `br-anchor` / 数字行标。
  - chrome（删）：`table-*` 全系（拖拽/缩放/工具条）+ `cangjie-selection-layer` + `drag-hander`。
  - **已验证：没有任何 data-testid 包裹真 `<table>`**，所以删 chrome 子树不会误删表格。
- 图片 `<img src>` 是**相对路径** `/core/api/resources/img/<hash>?...`；imgmap 的 key 是
  **绝对 URL** `https://alidocs.dingtalk.com/core/...`。靠 hash 正则 `/resources/img/([0-9a-f]+)`
  二级索引兜底匹配。

### clean_adoc 9 步管线（顺序敏感）
1. **drop subtrees** — `DROP_TREE`(svg/script/style/…) + `data-testid` 以 `table-` 开头或
   = `cangjie-selection-layer`/`drag-hander` + `data-cangjie-not-editable` + `#doc-title-area`。
   **必须在步骤 2 剥属性前跑**，否则 data-testid 被删就认不出 chrome。
1.5. **leaf-block → `<p class="lvlN">`** — 非标题非表格的 leaf-block 按 group-block 祖先
   深度 N 转段落，让嵌套大纲渲染成缩进块而不是一坨文字墙。
2. **strip attrs + localize imgs** — 按 `ATTR_KEEP` 白名单留属性；img src 走 imgmap →
   hash 兜底 → 加 `loading=lazy`。
3. **unwrap** 非 `KEEP` 标签（保留子节点，去标签本身）。`KEEP` **不含 div**。
4. **drop empty** 块元素，迭代 3 次（清空子节点会让父节点也空）。
5. **unwrap span**（行内全摊平成文本）。
6. **strip 零宽/BOM 字符 + bullet-only glyph** — 用 `bullet_only` 正则剥掉 span-unwrap
   后残留的 ●○■▪ 折叠按钮裸字符。
7. **drop empty** 再来一次。
8. **`group_lvl_lists()`** — 把连续 `<p class="lvlN">`（N≥1）栈式合并成**原生嵌套
   `<ul><li>`**（lvl0/标题/表格终止一段 run），浏览器自动对齐 marker+文字+缩进。
9. **`merge_orphan_numbers()`** — 把孤立的 `1.`/`2.`（list-symbol 拆出的裸文本，浮在
   `<h2>` 前面）回接到后面的 h1-6/p/li。正则要求结尾有 `.、)` 所以表格里的数字（`12`）不会误中。

### 4 个硬化的渲染坑（根因 → 修法，回归时先查这几处）
1. **图片断图** — imgmap key 绝对、raw src 相对，直接查 dict 全 miss。修：`_img_hash()`
   按资源哈希建 `imgmap_by_hash` 二级索引兜底。
2. **正文塌成文字墙** — `sc-*` div 进了 KEEP + 没还原 leaf-block 结构。修：KEEP 去 div +
   步骤 1.5 按 group-block 深度转 `p.lvlN`。
3. **bullet 脱节 + 残留 glyph** — 旧版用绝对定位 `::before` 画符号，跟文字竖直脱节，
   还残留折叠按钮 ●○■。修：步骤 8 合原生 `<ul><li>` + `LVL_CSS` 用原生 `::marker` +
   步骤 6 剥 glyph。
4. **table chrome 文字 + 孤儿序号** — 每张表后泄漏 `正在移动0列表格0行表格`（`table-*`
   testid 工具条），有序标题序号 `1.` 脱节漂在标题前。修：步骤 1 删 table-* chrome 子树 +
   步骤 9 `merge_orphan_numbers` 回接序号。
