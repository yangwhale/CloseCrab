# control_request 能力 Roadmap

> CC stream-json 带外控制通道。不触发 LLM turn，直接改运行中 CC 进程状态。
> 核心价值：把「改 Firestore + 重启」的事变成「热切换、零重启、不丢 session」。
> 所有 subtype 均 binary 2.1.143 dispatch 实测确认（offset 163.9M handler）。
>
> **进度标记**：`[ ]` 未做 · `[~]` 进行中 · `[x]` 完成
> 实现某项后在此 mark done，并记 commit hash。

---

## 实现 pattern（参考 effort）

每个能力三层落地，照 `set_effort` 抄：
1. **worker 层**（`workers/claude_code.py`）：`async def <action>()` 发 control_request 到 `sock_in`
2. **core 层**（`core/bot.py`）：`async def <action>(user_key, ...)` 找 worker 调用
3. **channel 层**（`channels/feishu.py`）：`_TEXT_COMMANDS` 注册 + `_handle_text_command` 分支

fire-and-forget 类（set/apply/interrupt）直接发即可。
查询类（get_*/rewind dry_run）需要 **L0 基础设施**先做。

---

## L0 — 基础设施（多项依赖，先做）

- [ ] **0.1 control_response 请求/响应关联**
  - 现状：effort 是 fire-and-forget，不读返回。但 `get_*`/`rewind_files` 需要读 binary 的 control_response。
  - 做法：worker 维护 `_pending_ctrl: dict[request_id, asyncio.Future]`；发送时建 future，reader loop 收到 control_response 按 request_id resolve；`send_control(subtype, payload, timeout)` 封装。
  - 影响文件：`workers/claude_code.py`（reader loop + 新 helper）
  - 阻塞：3.x 之后所有 get_* 类

---

## 已完成基线

- [x] **effort 控制**（`apply_flag_settings.effortLevel`）— `/low /medium /high /xhigh` 飞书命令
  - 实测：4.7 上 low avg 74s vs xhigh >120s，生效确认
  - commit: 待 push（claude_code.py `set_effort_level` + bot.py `set_effort` + feishu.py 命令）

---

## L1 — 消灭重启（最高价值）

### 1. set_model 热切换
- subtype: `set_model`，参数 `{subtype:"set_model", model:"<alias>"|"default"}`（剥 @ 后缀）
- binary 核验：handler 读 `H.request.model`，schema `{subtype, model:string().optional()}`，设 `mainLoopModelForSession`；在免审批白名单 `Be5`。
- 价值：换 model 不再需要重启 + 丢 session。当前换 model 是重启第一大理由。
- [x] **1.1 worker.set_model_live(model)** — 发 set_model control_request（`claude_code.py` set_model_live）
- [x] **1.2 core.set_model(user_key, model)** — 剥 @ 后缀 + 调 worker（`bot.py` set_model）
- [x] **1.3 /model 飞书命令** — `/model` 列用法，`/model <name>` 热切（`feishu.py` _TEXT_COMMANDS + _handle_text_command arg）
- [x] **验证** — `/tmp/setmodel_probe.py` 同进程热切 PASS：opus-4-7→opus-4-6→opus-4-7（读 assistant message.model）。坑：目标 model 必须在 Vertex project 启用，否则回 `<synthetic>`。小爱已带新代码重启，`/model` 对它生效。
- [ ] **1.4（可选）自动按任务切** — 重活切 4.7、闲聊切 haiku 省钱

### 2. MCP 热管理
- subtype: `mcp_set_servers {servers:[...]}` / `mcp_reconnect` / `mcp_status` / `mcp_toggle`
- 价值：解决两个老大难——LOAS2 cert 过期（重连不重启）+ 冷启动 token（懒加载）
- [ ] **2.1 worker.mcp_status()** — 查 MCP 连接状态（依赖 L0）
- [ ] **2.2 worker.mcp_reconnect(name)** — 重连挂掉的 MCP
- [ ] **2.3 /mcp 飞书命令** — status / reconnect / toggle
- [ ] **2.4 MCP 懒加载** — 冷启动只挂核心 MCP，按需 `mcp_set_servers` 加（砍冷启动 token，依赖 2.x）

### 3. thinking budget 控制
- subtype: `set_max_thinking_tokens {max_thinking_tokens:<int>}`
- 价值：比 effort 更细的思考量硬上限；语音模式秒回
- [ ] **3.1 worker.set_max_thinking_tokens(n)**
- [ ] **3.2 语音模式自动低 thinking** — voice channel path 自动设低，文字模式恢复

---

## L2 — 现有功能升级

### 4. 实时状态查询（依赖 L0）
- [ ] **4.1 get_context_usage → /context** — 权威实时 context 占用（替换 usage 事件估算）
- [ ] **4.2 get_session_cost → /cost** — CC 算好的真实花费（替换 token×价表）
- [ ] **4.3 get_settings → /status 增强** — 真·实时 model/effort/权限（非 Firestore 缓存）
- [ ] **4.4 主动压缩触发** — context >85% 自动 `/cmp`（依赖 4.1）

### 5. interrupt 干净停
- subtype: `interrupt`
- [ ] **5.1 「停」改用 interrupt** — 停生成但保 session，不 kill worker（对比现状确认现在是怎么停的）

### 6. set_permission_mode 动态权限
- subtype: `set_permission_mode {mode, ultraplan}`
- [ ] **6.1 /plan 命令** — 临时进 plan 模式（风险任务）
- [ ] **6.2 按用户权限分级** — 信任用户 bypass，其他 default

---

## L3 — 新玩法

- [ ] **7. rewind_files → /undo**（`{user_message_id, dry_run}`）— 回滚本 session 改过的文件，先 dry_run 预览（依赖 L0）
- [ ] **8. reload_plugins** — 热重载插件不重启
- [ ] **9. submit_feedback** — 飞书表情 reaction 接 thumbs up/down
- [ ] **10. generate_session_title** — 自动会话标题
- [ ] **11. get_binary_version → /version** — 准确版本报告（依赖 L0）

---

## 推荐实现顺序

1. **1.1–1.3 set_model 热切**（性价比最高，跟 effort 同款 fire-and-forget，无需 L0）
2. **L0 基础设施**（解锁所有 get_* 查询）
3. **4.1–4.3 实时状态**（/context /cost /status）
4. **2.x MCP 热管理**（重连 + 懒加载）
5. **3.x thinking + 语音秒回**
6. 其余按需
