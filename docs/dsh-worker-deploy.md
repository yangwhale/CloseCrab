# 在一台新机器上把 dsh worker 跑起来（含 Gemini）

> 目标：让一个 bot 用 **DeepSeek Harness** 当 agent runtime，模型可以是
> Claude、**也可以是 Gemini** —— 后者是这个 worker 存在的理由，Claude Code 不支持 Gemini。
>
> 本文所有命令与报错都是 2026-08-17 在 cc-tw 与 gLinux 两台机器上实跑出来的。
> 设计与代码细节见 [`.claude/rules/workers.md`](../.claude/rules/workers.md) 的 DSHWorker 一节，
> 这里只讲**怎么装、为什么这么装、装不上怎么查**。

---

## 0. 先看这张图：模型是怎么走到 Gemini 的

```
bot 进程
  └─ DSHWorker  ── stdio JSON-RPC ──►  dsh --profile closecrab   (Node ≥22.19)
                                          │
                                          └─ llm-pi-ai ──HTTPS──► LiteLLM 网关
                                                                     │
                                                          ┌──────────┴──────────┐
                                                          ▼                     ▼
                                                   Vertex Anthropic       Vertex Gemini
                                                   (claude-opus-5)        (gemini-3.7-flash)
```

**中间那个 LiteLLM 不是可选项，尤其对 Gemini。** 理由在 §2，先记住结论：
**Gemini 必须经 LiteLLM，不能直连 Vertex。**

---

## 1. 前置条件

| 要求 | 怎么查 | 不满足会怎样 |
|---|---|---|
| Node **^22.19 \|\| >=24** | `node --version` | dsh 启动报 `Promise.withResolvers is not a function` |
| `pnpm` | `pnpm --version` | `dsh: pnpm not found on PATH`，而且是在**建完 profile 目录之后**才报 |
| 能访问 LiteLLM 网关 | `curl -s -o /dev/null -w '%{http_code}\n' $LITELLM_URL/health/liveliness` | 每轮回复都是 `[dsh 出错] no credential ...` |
| `LITELLM_KEY` | `echo ${LITELLM_KEY:0:6}` | 同上 |

> ### ⚠️ node 的坑：不要只看 `node --version`
>
> `run.sh` 用的是 **nvm 下版本号最高**的那个（`ls ... | sort -V | tail -1`），
> 而 `command -v node` 拿到的是 nvm 的 **`default` 别名**。gLinux 上 default 指
> v20、实际装着 v22 和 v24 —— 只看 default 会得出「这台跑不了 dsh」的错误结论。
>
> 更隐蔽的是 **PATH 顺序**：`~/.local/bin/node` 如果是个指向旧版本的软链，会盖掉
> nvm 选出来的新版本。`run.sh` 已修（nvm 那条放最后），但**手动测试时要自己注意**：
>
> ```bash
> # 手动跑 dsh 前，用跟 run.sh 一样的方式定 node
> NVM_LATEST="$(ls -d "$HOME/.nvm/versions/node"/v* 2>/dev/null | sort -V | tail -1)"
> [ -n "$NVM_LATEST" ] && export PATH="$NVM_LATEST/bin:$PATH"
> node --version   # 必须 >= 22.19
> ```

`deploy.sh` 第 13 步和 `scripts/dsh-setup.sh` 都已按这个规则解析 node，
并且**缺 pnpm 会自动装**，所以走标准部署流程不用手工处理这两条。

---

## 2. 为什么非得用 LiteLLM，不能直连 Vertex

这是最容易被质疑、也最该写清楚的一条。**我试过直连，失败了，原因很具体。**

### 2.1 原生 Vertex dialect：dsh 没暴露

pi-ai 这个库**支持** `google-vertex`，但 dsh 的 `llm-pi-ai` 插件只注册了三种：

```
openai-completions   openai-responses   anthropic-messages
```

配 `api: google-vertex` 直接报：

```
no adapter registered for provider "vertex"
```

### 2.2 Vertex 的 OpenAI 兼容端点：纯对话能通，**一带工具就死**

Vertex 自己有个 OpenAI 兼容端点，看起来正好能用：

```
https://aiplatform.googleapis.com/v1/projects/$PROJ/locations/global/endpoints/openapi
模型 id 要写 google/gemini-3.7-flash
```

curl 打过去纯对话没问题，**agent 一调工具就 400**：

```
400: Function call is missing a thought_signature in functionCall parts.
     This is required for tools to work correctly, and missing thought_signature
     may lead to degraded model performance.
     Additional data, function call `default_api:bash`, position 2.
```

**根因**：Gemini 3.x 的工具调用要求每个 function call 回传一个
**thought signature（思考签名）**。OpenAI 的 schema 里**没有承载这个字段的位置** ——
兼容层把它丢掉，下一轮 agent 按 OpenAI 格式回传工具结果时签名已经没了，Vertex 拒绝。

链路是这样断的：

```
1. 模型返回 function call  ✅ 带 thoughtSignature
2. OpenAI 兼容层转换       ❌ 签名无处安放，丢弃
3. agent 回传工具结果      ❌ 签名缺失
4. Vertex 校验             ❌ 400
```

**这不是配置能修的，是协议信息丢失。** 对 agent 场景等于不可用
（纯聊天机器人不受影响，但那不是我们要的）。

### 2.3 LiteLLM 为什么行

它翻译到 **Gemini 原生 API**，签名在它内部完整往返 —— 响应里能看到
`provider_specific_fields.thought_signatures`。

⇒ **Gemini 一律配 `litellm/gemini-*`。** 等 dsh 把 `google-vertex` 这个原生
dialect 注册出来，才有直连的可能；worker 侧的 `provider/model` 解析和
token 续期已经写好了（见 §6），到那天改一行配置就能切。

---

## 3. LiteLLM 网关要提供什么

> **网关不在本仓库。** 它是一台单独的机器上的 docker compose。
> 这一节是给「从零搭网关」的人看的；网关已经有了就跳到 §4。

### 3.1 最小 compose

```yaml
services:
  litellm:
    image: ghcr.io/berriai/litellm:main-stable
    restart: always
    volumes:
      - ./config.yaml:/app/config.yaml
      - ./adc.json:/app/adc.json:ro        # 有 Vertex 权限的 SA key
    command: [ "--config", "/app/config.yaml" ]
    ports: [ "80:4000" ]
    environment:
      DATABASE_URL: "postgresql://USER:PASSWORD@db:5432/litellm"
      STORE_MODEL_IN_DB: "True"
      GOOGLE_APPLICATION_CREDENTIALS: "/app/adc.json"
    env_file: [ .env ]                     # LITELLM_MASTER_KEY 放这里
    depends_on: { db: { condition: service_healthy } }
```

### 3.2 模型条目

**Gemini —— 什么都不用加，默认就对：**

```yaml
- model_name: gemini-3.7-flash
  litellm_params:
    model: vertex_ai/gemini-3.7-flash
    vertex_project: YOUR_PROJECT
    vertex_location: global
```

> **不要给 Gemini 写 `reasoning_effort: high`。** 这台网关原来给好几个 Gemini
> 条目钉了 high，实测 `gemini-3.1-pro-preview` 首字延迟 **24.6s**，删掉后
> **3.2s**。它是替所有调用方做主，而且做的是最慢那个选择。

**Claude —— 三个字段缺一不可：**

```yaml
- model_name: claude-opus-5
  litellm_params:
    model: vertex_ai/claude-opus-5
    vertex_project: YOUR_PROJECT
    vertex_location: global
    additional_drop_params: [ temperature, top_p ]   # Vertex Anthropic 不收这两个
    cache_control_injection_points:                  # 不写就完全没有 prompt 缓存
      - { location: message, role: system }
      - { location: message, index: -1 }
  model_info:
    max_input_tokens: 1000000
```

`cache_control_injection_points` 是**省钱的关键**，客户端一个参数都不用改：

| 计价项 | 相对一般输入 |
|---|---:|
| 一般 input | 100% |
| cache **write** | 125%（只付一次） |
| cache **read** | **10%** |

一段 46K 的稳定前缀，Opus 5 每轮不开缓存 **$0.234**；开了第一次 $0.293、
之后每轮 **$0.023** —— 第二轮回本，第十轮省 88%。

> **验证缓存真的生效**：同一个请求连发两次，第二次的 `cache_read_input_tokens`
> 必须 > 0。前缀太短不会命中 —— Haiku 的最小可缓存长度比 Opus 高，3K 时静默不缓存，
> 4.8K 才开始命中。**这是模型特性不是配置错。**

**改完 config 必须：**

```bash
docker compose up -d --force-recreate litellm
```

> ⚠️ **只改了 bind-mount 的文件时，`docker compose up -d` 不会重建容器** ——
> 它认为服务定义没变。不加 `--force-recreate` 你会对着旧配置debug 半天
> （问过我怎么知道的）。

### 3.3 已知的网关侧缺陷

`/spend/logs` 返回的 **`cache_read_input_tokens` 整列是 0**，而模型响应里明明有值。
**靠 spend log 统计「缓存省了多少钱」会算错**，要看响应里的 usage。

---

## 4. 装 dsh 并建 profile

标准路径就是跑部署脚本，第 13 步会做完全部：

```bash
cd ~/CloseCrab && ./deploy.sh          # 或只跑这一步：
scripts/dsh-setup.sh                   # 幂等；--check 只体检不改动
```

它做四件事，每件都有对应的坑：

1. **建 profile 骨架**。不能用 `dsh plugin --profile <新名> add @deepseek-ai/dsh-base`
   —— dsh-base 依赖一个**没发布**的包（`dsh-settings-local`），pnpm 直接 404。
   bundle 本来就从 dsh 安装目录解析，profile 只需要**声明**它、不能依赖它。
   脚本的做法是先让 dsh 自建 headless 模板再 fork。
2. **装 `dsh-sdk-jsonrpc-server` + `dsh-sdk-protocol`**。前者是常驻会话与流式事件的
   来源（dsh 自带的 web / headless profile 都撑不起聊天 bot）；后者是它的 peer，
   pnpm 不会自动带。
3. **写 patch 层**（模型路由 + 分级 + MCP）。
4. **冒烟测试**：真的发一条 `initialize` 上去，看到 `serverInfo` 才算成功。
   「配置能解析」不等于「运行时能应答」。

### 内部工具的 MCP 不要写进这个脚本

**CloseCrab 是公开仓库。** 任何点名内部工具的东西（server 名、绝对路径、端口）
都放私有目录：

```
$PRIVATE_SKILLS_DIR/dsh/profile-overlay.sh      # 默认 ~/private-skills/dsh/
```

`dsh-setup.sh` 只有一句「存在就 source 它」，公开这边看不出追加了什么。
overlay 直接往 `$PATCH` 追加行即可。

### patch 文件两条反直觉规则（写错就整棵树起不来）

- **一条 patch 整体替换目标 row 的 `config`，不做深合并。** 只想加一个字段，
  结果原有必填项一起没了。漏掉 `session-title-llm` 的 `timeoutMs`
  → **整个插件树加载失败，agent 连 bash 都没有**，而报错只说某个 entry invalid。
- **`- id: X` 是改已有 row；加新 row 要用 `- insert: [...]`。**
  拿 `- id:` 加新 row 只会打一行 `patch: entry "X" not found` 然后**继续跑**，
  你会以为配好了。

---

## 5. 把一个 bot 切过去

```bash
python3 scripts/config-manage.py set-worker-type <bot> dsh
python3 scripts/config-manage.py set <bot> model "litellm/gemini-3.7-flash"
BOT_NAME=<你自己> ./scripts/launcher.sh restart <bot>
```

模型串是 **`provider/model`**（按第一个斜杠切）。`provider` 是 profile 里
`llm-pi-ai.providers` 下的键名，不是厂商名。

验证：

```bash
grep "DSHWorker started" ~/.claude/closecrab/<bot>/bot.log | tail -1
# → model=litellm/gemini-3.7-flash 就对了
```

> **模型必须同时在 profile 的 `models:` 目录里声明过**，否则调用时报
> `pi-ai provider "litellm" has no configured model "..."`，
> 而模型侧看到的只有一句 `subagent run failed`，非常难查。

---

## 6. 模型分级（可选，但很省钱）

dsh **没有「主/次/快」这种档位**，它让**每个会调 LLM 的消费者各自路由**：

| 消费者 | patch 里的 row | 键名 |
|---|---|---|
| 主 agent | `agent-default-model` | `provider` / `model` |
| 会话起标题 | `session-title-llm` | `provider` / `model`（都省略则继承主请求） |
| 上下文压缩摘要 | `compaction-basic` | **`summarizationProvider` / `summarizationModel`** |
| spawn 子 agent | `tool-subagent` | `agentOptions.{provider,model,maxTokens}` |
| fork 子 agent | `tool-subagent-fork` | 同上 |

`dsh-setup.sh` 默认配的是 opus-5 主、sonnet-5 子 agent 与压缩、haiku 起标题与 fork。
网关账本可验证三档确实分开（一个 session 内 opus 42 次 / sonnet 4 次 / haiku 2 次）。

⚠️ `compaction-basic` **不收 `provider` / `model`**，写了报 `unknown key "provider"`。

---

## 7. 排障速查

| 症状 | 真实原因 | 怎么办 |
|---|---|---|
| `Promise.withResolvers is not a function` | node < 22（多半是 PATH 里有旧版本软链把 nvm 的新版盖了） | 按 §1 那段定 PATH |
| `dsh: pnpm not found on PATH` | 缺 pnpm，且**目录已建**，重跑像在续跑 | `npm i -g --prefix ~/.npm-global pnpm` |
| 启动报 `invalid config` / `unknown key` | patch 整体替换丢了必填项，或键名不对 | 从 `--dump-default-config` 把原 config 抄全再叠字段 |
| `patch: entry "X" not found`（只是警告） | 拿 `- id:` 加新 row | 改用 `- insert:` |
| 整个 runtime 起不来，报 MCP schema 错 | 某个 MCP 的 `!!js process.env.XXX` 是 undefined | 那个变量给个空串也行；一个可选凭据不该拖垮全部 |
| `no credential for provider route` | `LITELLM_KEY` 没进环境 | 跑 `./deploy.sh` 从 Firestore 拉，或写进 `.zshenv` |
| 带工具就 `400 ... thought_signature` | 直连了 Vertex 兼容端点 | 见 §2，改回 `litellm/` |
| `has no configured model` / `subagent run failed` | 模型没在 profile 的 `models:` 里声明 | 加进去 |
| snap 装的命令全废（`snap-confine ... cap_dac_override`） | dsh 默认 `workspace-write` 沙箱剥 capability | worker 已默认 `DSH_PERMISSION_MODE=danger-full-access`（与另外四个 worker 对齐） |
| 回复是空字符串 | 早期 bug，已修；若复现看 `turn/end` 的 `reason` | worker 现在会把 `[dsh 出错] <原因>` 报出来 |
| 飞书卡片 CTX 一直 0 | 早期 bug，已修 | 升级到 `0aee7c7` 之后 |

日志在这三个地方，按顺序查：

```
~/.claude/closecrab/<bot>/bot.log            # worker 视角
~/.claude/closecrab/<bot>/dsh-stderr-*.log   # dsh 启动失败的真正原因在这
$DSH_HOME/sessions/*/*/session.jsonl.zstd    # 逐事件，zstd -dc 解开
```

---

## 8. 它做不到什么

| | |
|---|---|
| **中断不保留历史** | JSON-RPC 只有 initialize / session/prompt / shutdown，**没有 cancel**。`interrupt()` 只能杀进程；而 session id **不能复用**（复用会报 id collision），所以中断即丢 dsh 侧历史，工作目录还在 |
| **Gemini 不能直连 Vertex** | §2 |
| **没有按轮语义召回** | 记忆走 `$DSH_HOME/AGENTS.md`（等价于 Claude Code 的 CLAUDE.md，同一批文件），但只有静态索引。**可执行的常量要写进索引行本身**，只放详情页对它等于不存在 |
| **上下文压缩未经实测** | 代码里有、生产会走，但从没触发过 |

---

## 9. 一次跑通的检查清单

```bash
# 1. node 与 pnpm
NVM_LATEST="$(ls -d "$HOME/.nvm/versions/node"/v* 2>/dev/null | sort -V | tail -1)"
[ -n "$NVM_LATEST" ] && export PATH="$NVM_LATEST/bin:$PATH"
node --version && pnpm --version

# 2. 网关通不通、key 在不在
curl -s -o /dev/null -w '%{http_code}\n' "$LITELLM_URL/health/liveliness"
[ -n "$LITELLM_KEY" ] && echo "key ok"

# 3. 建 profile（幂等，最后必须看到 handshake OK）
scripts/dsh-setup.sh

# 4. 切 bot 并重启
python3 scripts/config-manage.py set-worker-type <bot> dsh
python3 scripts/config-manage.py set <bot> model "litellm/gemini-3.7-flash"
BOT_NAME=<你自己> ./scripts/launcher.sh restart <bot>

# 5. 验证：这一条要能跑出工具调用，不能只回文字
python3 scripts/inbox-send.py <bot> "用 bash 跑 \`node --version\`，一句话报我。"
```

第 5 步**必须带工具**。纯文字能通、带工具挂，正是直连 Vertex 那个坑的症状 ——
只测「你好」会漏掉它。
