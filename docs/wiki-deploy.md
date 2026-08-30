# 在一台新机器上把 Wiki 跑起来

> 目标：让这台机器上的 bot 能做到「凡事先问 Wiki」—— 有本地索引、能语义检索、
> 能通过 MCP 被 agent 直接调用。

## 先搞清楚一件事：Wiki 有两代，别混用

| | **v1（legacy）** | **v2（Quartz）** |
|---|---|---|
| 脚本 | 本仓库 `skills/wiki/scripts/` | **Wiki 仓库** `$WIKI_REPO/scripts/`（不在本仓库） |
| 站点生成 | 自制 HTML 模板 | [Quartz](https://quartz.jzhao.xyz/)（Node） |
| 内容目录 | `wiki/`（HTML） | `content/`（Markdown） |
| 索引 | `wiki-data/search-chunks.json` + `graph.json`，**查询依赖它** | **查询不依赖索引**，MCP 直接读 Markdown + `[[wikilinks]]`；`search-chunks.json` / `graph.json` 仍会由 `rebuild_incremental.py` 生成，但那是给**网站搜索页**用的 |
| 查询 | `wiki-query.py` | `query.py`（BM25 + 图增强 + 同义词） |
| MCP | `skills/wiki/scripts/wiki-mcp-server.py` | `$WIKI_REPO/scripts/wiki-mcp-server.py` |

**两代的脚本不能交叉用**（目录结构不一样）。已有 v1 Wiki 的机器继续用 v1，
新建议直接上 v2。下文默认 v2。

---

## 工具链住在哪：v2 的脚本**不在本仓库**

这一点先说清楚，否则后面每一步的路径都会找错。

| | v1 | v2 |
|---|---|---|
| 脚本归属 | 本仓库 `skills/wiki/scripts/` | **Wiki 仓库** `$WIKI_REPO/scripts/` |
| 怎么拿到 | 跟 CloseCrab 一起 clone | **clone Wiki 仓库即得** —— 内容和脚本本来就在一起 |

CloseCrab 曾经拷过一份 v2 工具链到 `skills/wiki/scripts-v2/`，
**已于 2026-08-09 删除**。原因不是它坏了，是它是第二份：
Wiki 仓库本身就是独立 git 仓库、脚本全部 tracked，注册的 MCP 也指向它。
两份各自演进必然漂移，而漂移的解法是「不该有第二份」，
不是「给第二份建同步」（rsync 会弄脏另一个仓库的 tracked 文件；
symlink 会让 Wiki 仓库不能独立 clone）。

所以：**要用 v2 工具链，就 clone Wiki 仓库。** 你反正也需要它的 content/。

## 一、这台机器归哪个 Wiki 管

这是**最容易踩的坑**：所有脚本都写成 `os.environ.get("WIKI_REPO", "~/my-wiki")`，
即「环境变量优先 + 默认兜底」。如果不设 `WIKI_REPO`，就会回落到 `~/my-wiki` ——
而这个目录在多数机器上并不存在，于是 `rebuild-graph` / `build-search-index` /
`fix-backlinks` 等一整套**静默失效**（不报错，只是什么也没做）。

`deploy.sh` 现在会自动探测并写进 `~/.claude/settings.json`：

```bash
# config/env.sh 的 compute_dynamic_vars() 按这个顺序探测
~/my-wiki-v2  →  ~/my-wiki  →  ~/my-wiki-study
```

想显式指定就在部署前 export：

```bash
export WIKI_REPO=~/my-wiki-study
./deploy.sh
```

**验证**（这一步别跳过）：

```bash
cd "$WIKI_REPO"/scripts
python3 -c "from wiki_utils import WIKI_REPO, WIKI_CONTENT; print(WIKI_REPO, WIKI_CONTENT.exists())"
# 必须打印 True。打印 False = 这台机器指错了 Wiki，后面全都白做。
```

> `wiki_utils.py` 里 `WIKI_REPO` 的取值顺序是 **环境变量 → 本文件所在仓库根（自定位）**。
> 早先它写死成 `~/my-wiki-v2`，导致 `deploy.sh` 的注入完全不生效 ——
> 设了 `WIKI_REPO=~/my-wiki-study` 也照样解析到 my-wiki-v2，
> 目录不存在 → 查询恒为空 → **不报错**。2026-08-09 已修。

---

## 二、四个环境变量

| 变量 | 必填 | 作用 | 不设会怎样 |
|---|---|---|---|
| `WIKI_REPO` | ✅ | Wiki 仓库根目录 | 回落 `~/my-wiki`，整套工具静默失效 |
| `WIKI_GCS` | ✅（要发布的话） | 构建产物上传到哪个 GCS 路径 | 构建完**报错退出 1**，明说没上传（不猜桶，也不静默跳过） |
| `WIKI_URL` | — | 站点公网地址，用于生成页面链接 | 链接留空 |
| `CC_PAGES_URL_PREFIX` | — | 从 CC Pages HTML 反向录入时的来源链接 | 来源链接留空 |

> 可选依赖 **`opencc`**：装了才支持繁简互查（简体查询命中繁体页面）。没装静默降级，简体查询不受影响。

都写在 `~/.claude/settings.json` 的 `env` 里（`deploy.sh` 会生成）。
**不要靠 shell export** —— 那个会跨进程继承，改了还不在 `/proc/environ` 里显形，
是最难查的一类配置来源。

---

## 三、从零建一个 v2 Wiki

```bash
# 1. 拿一份 Quartz 骨架（或 clone 你已有的 wiki 仓库）
git clone https://github.com/jackyzha0/quartz.git ~/my-wiki-v2
cd ~/my-wiki-v2 && npm install

# 2. 建目录结构
mkdir -p content/{sources,entities,concepts,analyses} wiki-data

# 3. 告诉这台机器它归谁管
export WIKI_REPO=~/my-wiki-v2

# 4. 录入第一篇内容（v2 的检索直接读 Markdown，**不需要**预先建索引）
cd "$WIKI_REPO"/scripts
python3 ingest.py text --slug hello --title "第一页" --text "这是第一条内容"
#   ingest.py 的签名: {url|pdf|text} [路径] --slug X --title Y [--tags a,b]

# 5. 冒烟
python3 status.py          # 页数、类型分布、最后构建时间
python3 query.py "任意关键词" --top-k 3
```

### 内容目录约定

```
content/
├── sources/    一篇资料一页（PDF / 网页 / 会议纪要）
├── entities/   实体（人 / 产品 / 项目 / 硬件型号）
├── concepts/   概念（技术 / 方法 / 理论）
└── analyses/   分析与对比（你自己的结论，最有价值的一层）
```

页面之间用 `[[slug]]` 互链，`gen-moc.py` 会据此生成 MOC，
`ingest.py` 会维护 `wiki-data/graph.json` 和 `search-chunks.json`。

---

## 四、让 agent 能调用它（MCP）

**没有 MCP，Wiki 的体验是打折的。** agent 只能 bash 调 `query.py` 再自己解析
stdout —— 拿不到结构化结果，也用不上 `wiki_ask` / `wiki_related` 这些
命令行没有的能力。实测一台没装 MCP 的机器，`query.py` 被调了 68 次，
那是它访问 Wiki 的唯一途径。

### 一条命令装完

```bash
~/CloseCrab/scripts/install-wiki-mcp.sh
```

幂等，可反复跑。它会依次：探测 `WIKI_REPO`（判据是「有 `content/`」）→
**从正在跑的 bot 进程反查解释器** → 校验依赖符号 → 装 `mcp` → 加载测试
→ 注册进 `~/.claude.json`（先备份）。

```bash
./install-wiki-mcp.sh --check    # 只体检不改动
./install-wiki-mcp.sh --from <另一个wiki>/scripts/wiki-mcp-server.py
WIKI_REPO=~/my-wiki-study ./install-wiki-mcp.sh
```

装完**必须重启 bot** —— MCP 配置是 CLI 启动时读的，改了不重启等于没改。

```bash
pkill -f 'closecrab --bot <name>'   # run.sh 会自动拉起
```

### 三个坑，装之前先知道

**① PyPI 上有两个都叫 `mcp` 的项目**

我们要的是官方 SDK（`mcp==1.27.x`，`server/` 下有 `fastmcp`）。
另一个 `mcp==2.x` 没有 `fastmcp`。装错了报的是：

```
ModuleNotFoundError: No module named 'mcp.server.fastmcp'
```

这个报错**看起来像「没装」，实际是「装了另一个同名项目」**。
2026-08-10 我照着报错去「卸载重装」，方向正好反了 ——
对比两台机器 `mcp/server/` 目录的内容才看清是版本不同不是包不同。
脚本里钉死了版本，升级前先确认 `mcp.server.fastmcp` 还在。

**② PEP 668：Ubuntu 24.04+ 的 pip 直接拒绝安装**

报 `externally-managed-environment`。要 `--user --break-system-packages`。

**③ 装到错的解释器上等于没装**

一台机器可能有 python3.12 和 3.14 两个。**bot 用哪个就得装到哪个的
user-site**。脚本从正在跑的 bot 进程 `/proc/<pid>/exe` 反查，不猜 ——
所以最好在 bot 跑着的时候装。

### MCP server 需要 Wiki 提供什么

它是个薄适配层，只依赖 8 个符号。你的 Wiki 只要有这些就能接：

| 来源 | 符号 |
|---|---|
| `wiki_utils.py` | `WIKI_CONTENT` `WIKI_URL` `all_pages` `parse_frontmatter` `extract_wikilinks` `find_page_by_slug` `page_url` |
| `query.py` | `query()` |

**缺哪个补哪个，不要整份 query.py 照搬** —— 各家 Wiki 的 query.py 本来就不同
（一个 29 页的 Study Wiki 不需要倒排索引和 LRU 缓存，那是给 456 页准备的）。

### ⚠️ 光有这 8 个符号还不够，还有运行时契约

2026-08-10 实证：一台机器 8 个符号全齐、9 个 tool 全部加载成功，
**一调用 4 个直接炸**。符号在不代表接口对得上，还有三处隐性约定：

| 约定 | server 假设 | 对不上会怎样 |
|---|---|---|
| `query()` 返回什么 | **list** of dict | 返回 dict 时 `for r in results` 迭代到 key 字符串，`r["title"]` 炸 |
| 结果项有哪些键 | 必须有 `path`（用来算 slug）、`title`、`url` | `KeyError: 'path'` |
| 额外符号 | `wiki_ask` 用了 `_tokenize` / `get_index` / `idx.get_page` | `ImportError` |

而且 server 用 `@_safe_tool` 把异常**包成 `{"error": ...}` 字符串返回、不抛出**，
所以「调用没报错」也不算通过 —— 必须看返回内容里有没有 error。

所以脚本的冒烟测试是**真调用每个 tool**（`scripts/wiki-mcp-smoke.py`），
参数从真实语料里取（用假词测不出来，空结果那条分支太短碰不到会炸的地方），
**任何一个 tool 失败就不注册** —— 宁可装不上，也别装上一个坏的。

### 装好之后有哪 9 个 tool

| Tool | 用途 |
|---|---|
| `wiki_query(question, top_k)` | BM25 检索，主入口 |
| `wiki_ask(question)` | RAG 式问答，直接提取答案段落 |
| `wiki_page(slug)` | 读整页 |
| `wiki_related(slug, top_k)` | 图 + tag + 类型推荐相关页 |
| `wiki_search(keyword)` | 结构化过滤 `type:source tag:tpu` |
| `wiki_list(type, tag)` | 按类型/标签列出 |
| `wiki_graph_neighbors(slug, depth)` | N-hop wikilink 邻居 |
| `wiki_graph_path(source, target)` | 两页最短路径 |
| `wiki_status()` | 统计 + 覆盖度 |

`wiki_ask` 和 `wiki_related` 是命令行完全没有的能力。

### 验证

```bash
./install-wiki-mcp.sh --check          # 应报 9 个 tool
```

重启 bot 后让 agent 真跑一次 `wiki_query("<你 Wiki 里确定有的词>")`。
**只看 --check 通过不够** —— 那只证明脚本能加载，不证明 CLI 真的注册上了。

---

## 五、检索质量：一个必须知道的坑

`query.py` 目前**只按 BM25 排序取 top-k，没有相关性下限**。中文语料上这会出问题，
因为分词退化成单字之后，「的 / 和 / 用 / 在 / 如何」这类虚词也算命中。

实测（2026-08-09，456 页的 TPU/GPU 语料）：

| | 真话题「TPU v7 Ironwood 的 HBM 带宽和 MFU」 | 假话题「如何用微波炉烤惠灵顿牛排」 |
|---|---|---|
| top score | 87.57 | **15.44** |
| 返回条数 | 3 | **5（全部无关）** |
| 命中词 | 12 个实词 tpu / v7 / ironwood / hbm / 带宽 … | **1 个虚词：「用」** |
| 耗时 | 156 ms | **4446 ms** |

假话题返回了 FP8 合成、Mooncake RDMA、Kimi 贡献者名单 —— 全无关，
**只因为命中了单个汉字「用」**。对「这事记过没」这个用途是硬伤。

**修法**（比单加分数阈值更稳）：

- 判据放在**命中词的构成**上，不是分数上。分数会随查询长度和语料规模漂移。
- 先用 CJK 功能字表把查询切成实词片段，只在片段内部取 bigram，虚词不进入分母；
  再用 `覆盖率 = 命中实词 / 全部实词` 做下限。
- **注意别矫枉过正**：只加阈值会**漏报**。实测「人是怎么受精的」这种口语化真问题，
  六个 bigram 里只有「受精」有意义，覆盖率被稀释到 33% 而被误拒。
  功能字切段之后恢复 100%。漏报比误报更糟 —— 该有的说没有会直接摧毁这个工具的用途。
- 附带收益：假查询慢 28 倍（虚词的倒排表巨大），按实词过滤同时是**性能修复**。

---

## 六、日常怎么用

```bash
cd "$WIKI_REPO"/scripts

python3 ingest.py <文件或URL> --slug xxx --title "..." --tags "a,b"   # 录入
python3 query.py "问题" --top-k 5                                      # 检索
python3 lint.py                                                        # 断链 / 孤儿页 / 缺失概念
python3 status.py                                                      # 健康度
python3 gen-moc.py                                                     # 重建 MOC
bash build-and-sync.sh                                                 # Quartz 构建 + 上传（需 WIKI_GCS）
```

**给 agent 的行为约定**（写进 bot 的 system prompt 或 CLAUDE.md）：

1. 回答知识性问题**之前**先 `wiki_query`，别凭记忆答
2. 用户分享有长期价值的资料时，主动问「要录入 Wiki 吗」
3. 产出有持久价值的分析后，建议回存为 `analyses/` 页面
4. 每 10 次 ingest 或超过一周，提醒跑一次 lint

---

## 七、排查

| 现象 | 原因 | 怎么查 |
|---|---|---|
| `Search index not found` | **v1 才有的报错**；v2 不读索引，出现它说明你在用 v1 脚本查 v2 目录 | 确认用的是 `$WIKI_REPO/scripts/query.py` |
| 脚本跑完什么也没发生 | `WIKI_REPO` 回落到不存在的默认值 | 同上。**这是最常见的一个** |
| MCP tool 不出现 | `~/.claude.json` 里没注册，或脚本路径不存在 | `python3 <那条路径> --help` 直接跑一下 |
| 查什么都返回一堆无关页 | 见第五节，缺相关性下限 | 看返回结果的 `matched_terms`，如果只命中虚词就是这个问题 |
| 构建了但网站没更新 | `WIKI_GCS` 没设 —— 2026-08-29 前脚本没守卫，空串传给 gcloud 直接崩在 `Unacceptable pattern: ''`，而 ingest 只看到「构建成功」 | 现在会明确报错退出 1。先 `echo $WIKI_GCS` |
| 站点上有这页，git 里查无此文 | **发布 ≠ 存档** —— `build-and-sync.sh` 只构建 + 上传，完全不碰版本控制 | 脚本末尾现在会报 `content/` 有几项没提交（2026-08-30 加，此前积过六天） |
| 页面 404，但 `curl` 看着是 302 | **IAP 对任何路径都回 302**，包括不存在的 —— 302 不能证明页面存在 | 别 curl，直接列桶：`gcloud storage ls gs://$WIKI_GCS_桶/…` |

---

## 附：先确认「哪份在跑」

同一个 BM25 检索，这个项目里**至少有四份实现**散落在两个仓库：

| 实现 | 位置 | 谁在跑 |
|---|---|---|
| `wiki-query.py` | 主仓 `skills/wiki/scripts/` | **无人调用**（只在 docstring / 注释里被提及） |
| 内联 BM25 | 主仓 `skills/wiki/scripts/wiki-mcp-server.py:90` | **无注册方**（deploy 注册的是 `$WIKI_REPO` 里那份） |
| `query.py` | Wiki 仓库 `scripts/` | ✅ 实际在用 |
| `wiki-mcp-server.py` | Wiki 仓库 `scripts/` | ✅ 实际在用（agent 走的就是它） |

2026-08-09 我和 athena 各自在这上面栽了一次，方向相反：

- 我把**能用的**判成死的 —— 看到 `~/my-wiki` 出现在代码里就下「写死」的结论，
  漏读了外面那层 `os.environ.get(...)`。按我的判断去删会毁掉 10 个在用的脚本。
- athena 把**没人用的**当成在用的 —— 核实了「文件确实是 tracked」，
  但没核实「有没有调用方」，于是把闸门改在了一个零调用的文件上。

**共同点：都没先确认「到底哪份在跑」。** 所以定下这个规矩：

> 改一个文件之前，先回答两个问题 ——
> **① 它的取值是怎么来的？**（别看字面量，看外面那层 `os.environ.get` / 配置读取）
> **② 谁在调用它？**（`git grep` 排除 docstring 和注释；MCP 类还要看谁注册）
> 两个都答不上来就先别动手。

### 一个反复出现的模式：回落到不存在的默认值

同一天在这个项目里撞了五次，全是同一个形状：

| 现象 | 那个不存在的默认值 |
|---|---|
| 定时探针找不到 gcloud | PATH 里写着 `$HOME/google-cloud-sdk/bin`，该目录不存在 |
| bot 用上了别人的 TTS 音色 | 音色挂在 Discord sidecar 启动流程里，关掉就回落到继承值 |
| wiki 脚本全体静默失效 | `WIKI_REPO` 未设 → 回落 `~/my-wiki`，该目录不存在 |
| Gemini 的 wiki MCP 从未生效 | 注入的脚本路径 `mcp-server/wiki_mcp.py` 不存在 |
| 4 个 skill 装不上 | allowlist 里列了公开仓库没有的目录 |

它们的共同点是**不报错**：系统看起来在正常运行，只是什么也没做。
所以本项目的取向是 —— **配置缺失就响亮失败，不要兜底**
（例：`tts_voice` 没配直接抛错；`WIKI_GCS` 没配就构建完 `exit 1`，
既不猜一个桶、也不静默跳过上传）。

> `WIKI_GCS` 这条是 2026-08-29 补上的，而它本身就是上面那张表的第四行：
> 脚本注释写着「没设就只构建不上传」，**但代码里根本没有那个守卫** ——
> 空串一路传到 `gcloud storage rsync`，崩在一句 `Unacceptable pattern: ''`。
> 而调用方（`/wiki ingest`）只看构建那段输出，于是**每次 ingest 都报成功、
> 每次都没发布**，直到有人点开链接发现 404。
> **教训：写在注释里的行为不算实现。** 声明了兜底/守卫，就要有一行代码对应它。
