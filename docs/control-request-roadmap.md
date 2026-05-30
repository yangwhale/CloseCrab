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

- [x] **0.1 control_response 请求/响应关联** — 完成
  - worker 维护 `_pending_ctrl: dict[request_id, asyncio.Future]`；`send_control(subtype, payload, timeout)` 建 future + 发 control_request；reader 收到同 request_id 的 control_response 时 `fut.set_result(resp)`。带外通道，不占 `_lock`，空闲/忙碌（send 进行中）都能用。
  - 实测：`/tmp/mcp_status_test.py` + `/tmp/mcp_cmd_e2e_test.py` —— 空闲 + 并发（send 跑 5s Bash 期间）双场景 mcp_status 均返回 8 connected servers。
  - 影响文件：`workers/claude_code.py`（`_pending_ctrl` + reader future-resolve + `send_control`）
  - 解锁：MCP 热管理（2.x）。get_* 类（4.x）spawn 模式不可行，见下。

- ⚠️ **get_* 查询在 spawn 模式不可行（探针实证）**
  - `get_context_usage` / `get_session_cost` / `get_settings` / `get_binary_version` 在 binary 里只作为 thin-client SEND 侧 `sendControlRequest({subtype})` + 独立 `--remote` responder 存在，**spawned agent 的 `[bridge:repl]` dispatch 没有对应 case**。
  - 探针 `/tmp/getstar_probe.py` 确认：4 个全部 NO RESPONSE。
  - 结论：`/context` `/cost` `/version` 拿不到 binary 权威数字，继续用 usage 事件估算。L2 §4 整段划掉。

---

## 已完成基线

- [x] **effort 控制**（`apply_flag_settings.effortLevel`）— `/low /medium /high /xhigh` 飞书命令
  - 实测：4.7 上 low avg 74s vs xhigh >120s，生效确认
  - commit: a8ae9b8（claude_code.py `set_effort_level` + bot.py `set_effort` + feishu.py 命令）

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
- [x] **2.1 worker.mcp_status()** — 查 MCP 连接状态。返回 `mcpServers[]`（name/status/serverInfo/config/scope/tools）。实测 8 servers 全 connected。
- [x] **2.2 worker.mcp_reconnect(name)** — 重连挂掉的 MCP。**坑**：binary handler destructure `serverName` 非 `name`（2.1.143 offset 157555664 "Cannot destructure property 'serverName'"），传错回 `Server not found: undefined`。改 `{"serverName": name}` 后 binary 真正执行重连。主用途：LOAS2 cert 过期免冷重启。
- [x] **2.3 /mcp 飞书命令** — `/mcp` 列 server 状态（🟢/🔴 + tools 数），`/mcp reconnect <name>` 重连。core 层 `mcp_status_cmd` / `mcp_reconnect_cmd`，channel 层 `/mcp` 分支。
- [ ] **2.4 MCP 懒加载** — 冷启动只挂核心 MCP，按需 `mcp_set_servers` 加（砍冷启动 token，依赖 2.x）。schema 已侦察：`mcp_set_servers {servers}`、字段对齐待核。

### 3. thinking budget 控制
- subtype: `set_max_thinking_tokens {max_thinking_tokens:<int>}`
- 价值：比 effort 更细的思考量硬上限；语音模式秒回
- [x] **3.1 worker.set_max_thinking_tokens(n) → /think 命令** — commit 08eb176（`set_thinking_live` + bot.py `set_thinking` + feishu.py `/think <n>`，fire-and-forget）
- [ ] **3.2 语音模式自动低 thinking** — voice channel path 自动设低，文字模式恢复（待做，task #16）

---

## L2 — 现有功能升级

### 4. 实时状态查询 — ⚠️ spawn 模式不可行，整段废弃
- ~~4.1 get_context_usage → /context~~ — get_* 无 agent dispatch，NO RESPONSE（见 L0）。`/context` 继续用 usage 事件估算（本就如此，不受影响）。
- ~~4.2 get_session_cost → /cost~~ — 同上，用 token×价表估算。
- ~~4.3 get_settings → /status~~ — 同上，用 Firestore 缓存 + worker 内存状态。
- [ ] **4.4 主动压缩触发** — context >85% 自动 `/cmp`（可基于估算值做，不依赖 get_*）

### 5. interrupt 干净停
- subtype: `interrupt`
- [x] **5.1 「停」改用 interrupt** — commit bca5ca8。软中断停 turn 但进程保活（warm MCP + cache 保留），下条消息复用 warm proc 无需 --resume；sendall 失败兜底硬杀。binary 探针 + worker 集成测试全 PASS。

### 6. set_permission_mode 动态权限
- subtype: `set_permission_mode {mode, ultraplan}`
- [x] **6.1 /plan 命令** — commit 08eb176（`set_permission_mode_live` + bot.py `set_permission_mode_cmd` + feishu.py `/plan [mode]`，校验 plan/default/acceptedits/bypasspermissions）
- [ ] **6.2 按用户权限分级** — 信任用户 bypass，其他 default

---

## L3 — 新玩法

- [ ] **7. rewind_files → /undo**（`{user_message_id, dry_run}`）— 回滚本 session 改过的文件，先 dry_run 预览（依赖 L0）
- [ ] **8. reload_plugins** — 热重载插件不重启
- [ ] **9. submit_feedback** — 飞书表情 reaction 接 thumbs up/down
- [ ] **10. generate_session_title** — 自动会话标题
- [ ] **11. get_binary_version → /version** — 准确版本报告（依赖 L0）

---

## 推荐实现顺序（进度）

1. [x] **effort 控制** — a8ae9b8
2. [x] **1.1–1.3 set_model 热切** — a8ae9b8
3. [x] **A trio fire-and-forget**：3.1 /think + 6.1 /plan（08eb176）+ 5.1 软中断（bca5ca8）
4. [x] **L0 基础设施** — `_pending_ctrl` future + `send_control` helper + reader future-resolve，完成
5. [~] **2.x MCP 热管理** — 2.1 mcp_status + 2.2 mcp_reconnect + 2.3 /mcp 命令完成，2.4 懒加载待做
6. [ ] **3.2 语音模式自动低 thinking**（task #16）
7. ~~4.1–4.3 实时状态~~ — get_* spawn 不可行，废弃
8. 其余按需（L3 rewind/plugins/feedback…）

> 全部在小爱（xiaoaitongxue）单 bot 验证，未铺给其他 bot。cc-tw bots 共享同一 checkout，铺开只需各自 SIGHUP 重启。
