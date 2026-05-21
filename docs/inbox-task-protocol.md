# Inbox Task Protocol — 多消息任务感知

> 设计与实现：2026-05-21
> 状态：V1 设计完成，待实施
> 作者：Chris × jarvis 协同设计

## 1. 问题

CloseCrab 的 Bot 间通过 Firestore Inbox 异步通信。当前协议（`scripts/inbox-send.py`）每条 inbox 消息都被接收方当成**独立的、无关联的事件**处理。

### 1.1 观测到的问题

Chris 派活给小爱同学跑 5 阶段测试，小爱同学每完成一阶段发一次 inbox，最终发 1 条 done。整个任务产生 6 条 inbox 消息：

```
小爱 → jarvis: "阶段 1 完成: GPU 检测通过"  ← 触发 turn 1, Claude 开始处理
小爱 → jarvis: "阶段 2 完成: TPU 初始化"    ← 触发 turn 2, Claude 又处理一遍
小爱 → jarvis: "阶段 3 完成: 数据加载"      ← 触发 turn 3
小爱 → jarvis: "阶段 4 完成: 模型 forward"  ← 触发 turn 4
小爱 → jarvis: "阶段 5 完成: 模型 backward" ← 触发 turn 5
小爱 → jarvis: "任务全部完成，结论 XXX"      ← 触发 turn 6
```

**问题表现**：
- jarvis 无法识别这 6 条消息属于同一件事 → 处理 6 次，回复 6 次，消耗 6 倍 token
- 飞书 UI 出现 6 个独立的螃蟹卡片排队转圈
- 主 session context 被 5 条无意义的进度消息污染
- 同时派活给 2 个 bot 时，问题加剧 → 5-6 个 spinner card 同时排队

### 1.2 根因

`closecrab/utils/firestore_inbox.py` 的 doc schema 里**已经有 `task_id` 字段**（line 135、156），但 `scripts/inbox-send.py` line 40 **hardcode 成空字符串**。换句话说：协议字段在，但发送端从未填，接收端也从未利用。

## 2. 设计目标

| 目标 | 说明 |
|------|------|
| **G1** | 同一任务的多条 inbox 消息必须能被接收方识别为一个整体 |
| **G2** | 进度消息**对用户可见**（飞书 chat 显示），但**不触发 Claude turn**（不烧 context、不烧 token） |
| **G3** | 只有 done 消息触发 1 次 Claude turn，prompt 包含所有 buffer 的进度 + 最终结论 |
| **G4** | 外部 bot 调度在语义上等同于 sub-agent 调度 — 主 session 必须拿到完整 summary |
| **G5** | 向后兼容 — 老版本 bot 不发 task_id 仍按现有行为处理 |
| **G6** | 多 bot 并发派活时，飞书 UI 上每个 task 独立 timeline，不互相覆盖 |

### 非目标（V1 不做）

- 嵌套子任务（task tree） — `parent_task_id` 字段保留但 V1 不实现
- 任务持久化跨 jarvis 重启 — V1 接受 "jarvis 崩了 → 任务上下文丢失"
- 任务取消 / pause / resume — V1 只支持线性 progress → done
- 跨频道任务展示 — V1 只在派活的频道里展示 timeline

## 3. 协议设计

### 3.1 Firestore `messages` 文档扩展 schema

```python
{
    # 现有字段
    "from": "xiaoaitongxue",
    "to": "jarvis",
    "instruction": "阶段 2 完成: TPU 初始化",
    "status": "pending",
    "result": "",
    "created_at": <Timestamp>,

    # 新增字段（全部可选，保持向后兼容）
    "task_id": "a3f2c1",            # 8 字符短 hash, 同一任务多消息共享
    "task_name": "测试 GPU 训练 Qwen3.5",  # 仅 kickoff 时 set，后续消息可省略
    "phase": "progress",            # "kickoff" | "progress" | "done"
    "phase_seq": 2,                 # 序号 1, 2, 3, ... 用于排序和去重
    "phase_label": "TPU 初始化",     # 短标签，飞书 UI 上显示
    "parent_task_id": "",           # V2 用，V1 始终空
}
```

**Schema 语义**：
- `task_id` 是 task 主键。kickoff 阶段由发送方生成（短 hash），后续 progress / done 必须沿用同一个
- `task_name` 只在 `phase == "kickoff"` 时由发送方提供；接收方 cache 起来，progress/done 阶段不需要重复
- `phase` 是 enum：
  - `kickoff` — 任务开启的第一条消息（可选；如果发送方不发 kickoff，接收方在第一条 progress 时自动建任务）
  - `progress` — 中间进度
  - `done` — 任务结束的最后一条消息，必须只有 1 条
- `phase_seq` 用于排序。重复 `phase_seq` 后到的覆盖先到的
- `phase_label` 是给人看的短标签（≤30 字符），用于飞书 timeline 卡片

### 3.2 `scripts/inbox-send.py` CLI 扩展

```bash
# 老用法（仍支持，task_id 留空，按现有行为）
python3 ~/CloseCrab/scripts/inbox-send.py jarvis "随便说句话"

# 新用法 — 开始一个多阶段任务
python3 ~/CloseCrab/scripts/inbox-send.py jarvis "开始测试 Qwen3.5" \
    --task-name "测试 GPU 训练 Qwen3.5" --phase kickoff
# stdout: a3f2c1   ← 自动生成的 task_id，调用方记下来后续用

# 续报进度
python3 ~/CloseCrab/scripts/inbox-send.py jarvis "GPU 检测通过" \
    --task-id a3f2c1 --phase progress --phase-seq 1 --phase-label "GPU 检测"

python3 ~/CloseCrab/scripts/inbox-send.py jarvis "TPU 初始化成功" \
    --task-id a3f2c1 --phase progress --phase-seq 2 --phase-label "TPU 初始化"

# 报完结
python3 ~/CloseCrab/scripts/inbox-send.py jarvis "全部通过，结论 XXX YYY" \
    --task-id a3f2c1 --phase done --phase-label "测试完成"
```

**Flag 行为**：
- `--task-name <str>` — 任务人类可读名（≤80 字符）；只在 kickoff 时生效
- `--task-id <hex8>` — 显式指定 task_id；不传时若 phase=kickoff 则自动生成
- `--phase <kickoff|progress|done>` — 阶段；不传默认 progress（兼容老脚本）
- `--phase-seq <int>` — 进度序号；progress/done 阶段必填
- `--phase-label <str>` — 短标签

**向后兼容**：所有新 flag 都可选；不传任何 flag 时行为与现有完全一致（task_id = "", phase = ""，接收方走 fallback path）。

**stdout 行为**：传 `--phase kickoff` 时第一行打印 task_id，方便 shell 脚本捕获：
```bash
TASK_ID=$(python3 ~/CloseCrab/scripts/inbox-send.py jarvis "开始" --task-name "X" --phase kickoff)
python3 ~/CloseCrab/scripts/inbox-send.py jarvis "进度 1" --task-id $TASK_ID --phase progress --phase-seq 1
```

### 3.3 `closecrab/utils/firestore_inbox.py` 改造

扩展 `_on_snapshot` 读取新字段，callback 签名加新参数：

```python
# 旧签名
async def callback(from_bot, instruction, doc_id, task_id): ...

# 新签名（task_id 已存在，扩展 task_name/phase/phase_seq/phase_label）
async def callback(
    from_bot: str,
    instruction: str,
    doc_id: str,
    task_id: str = "",
    task_name: str = "",
    phase: str = "",        # "" | "kickoff" | "progress" | "done"
    phase_seq: int = 0,
    phase_label: str = "",
): ...
```

通过默认参数保证向后兼容（discord channel 还没用新 protocol，老 callback 签名仍能工作）。

### 3.4 `closecrab/channels/feishu.py` 改造（核心）

#### TaskState 内存结构

```python
@dataclass
class _TaskState:
    task_id: str
    task_name: str           # kickoff 时 set，永不变
    worker_bot: str          # 发起方 bot name (即 from_bot)
    kickoff_at: datetime
    last_update_at: datetime
    main_card_id: str        # kickoff 时创建的主卡片 ID（飞书 message_id）
    progress_card_ids: List[str]  # 每个 progress 一个小卡片
    progress_buffer: List[Dict]   # [{seq, label, content, ts}, ...] 用于 done 时组装 prompt
    status: str              # "active" | "done"
    chat_id: str             # 飞书 chat（用于发卡片）
```

```python
self._task_registry: Dict[str, _TaskState] = {}  # task_id → state
```

#### Phase Dispatch 在 `_on_inbox_message`

```python
async def _on_inbox_message(
    self, from_bot, instruction, record_id,
    task_id="", task_name="", phase="", phase_seq=0, phase_label="",
):
    # Fallback: 没 task_id 走老路径（现有行为）
    if not task_id:
        return await self._execute_task(
            task_id=record_id, summary=instruction,
            description=f"来自 {from_bot}", ...
        )

    # 有 task_id, 按 phase 分流
    if phase == "kickoff":
        await self._handle_task_kickoff(
            from_bot, task_id, task_name, instruction, record_id,
        )
    elif phase == "progress":
        await self._handle_task_progress(
            task_id, phase_seq, phase_label, instruction, from_bot,
        )
    elif phase == "done":
        await self._handle_task_done(
            task_id, phase_seq, phase_label, instruction, from_bot, record_id,
        )
    else:
        # phase 不识别，走 fallback
        await self._execute_task(...)
```

#### Handler 1 — kickoff

```python
async def _handle_task_kickoff(self, from_bot, task_id, task_name, kickoff_text, record_id):
    # 1. 找一个合适的 chat — Chris 派活时记下来的 chat，或 fallback 到 inbox notify chat
    chat_id = self._resolve_task_chat(from_bot)

    # 2. 发主卡片：「📋 任务开启 — {task_name} (来自 {from_bot}) · ID: {task_id}」
    main_card_id = await self._async_send_card_with_id(
        chat_id=chat_id,
        title=f"📋 {task_name}",
        body=f"**任务开启** | 来自 `{from_bot}` | ID: `{task_id}`\n\n{kickoff_text}",
    )

    # 3. 注册 task
    self._task_registry[task_id] = _TaskState(
        task_id=task_id, task_name=task_name, worker_bot=from_bot,
        kickoff_at=datetime.utcnow(), last_update_at=datetime.utcnow(),
        main_card_id=main_card_id, progress_card_ids=[],
        progress_buffer=[], status="active", chat_id=chat_id,
    )

    # 4. Firestore 标记 done（这条 kickoff 处理完，不用再 trigger turn）
    self._inbox.mark_processed(record_id, "kickoff received")

    # ⚠️ 关键：不 trigger Claude turn
```

#### Handler 2 — progress（最关键，绕过 BotCore）

```python
async def _handle_task_progress(self, task_id, phase_seq, phase_label, content, from_bot):
    task = self._task_registry.get(task_id)
    if not task:
        # 没见过这个 task — 可能 jarvis 重启了，buffer 丢了
        # 兜底：把这条当独立消息处理
        return await self._execute_task(
            task_id=task_id, summary=f"[孤立进度] {content}",
            description=f"task_id={task_id} 找不到 kickoff", ...
        )

    # 1. 写入 buffer（按 phase_seq 排序，重复覆盖）
    task.progress_buffer = [b for b in task.progress_buffer if b["seq"] != phase_seq]
    task.progress_buffer.append({
        "seq": phase_seq, "label": phase_label,
        "content": content, "ts": datetime.utcnow(),
    })
    task.progress_buffer.sort(key=lambda b: b["seq"])
    task.last_update_at = datetime.utcnow()

    # 2. 发一个小进度卡片到飞书（旁路！不经过 BotCore）
    progress_card_id = await self._async_send_card_with_id(
        chat_id=task.chat_id,
        title=f"⏳ 进度 {phase_seq}: {phase_label}",
        body=f"**{task.task_name}** ({task_id[:6]})\n\n{content[:500]}",
        small=True,  # 紧凑模式
    )
    task.progress_card_ids.append(progress_card_id)

    # 3. Firestore 标记 done
    self._inbox.mark_processed(record_id, f"progress {phase_seq} buffered")

    # ⚠️ 关键：不 trigger Claude turn，progress_buffer 留在 jarvis 内存里
```

#### Handler 3 — done（assemble prompt + trigger turn）

```python
async def _handle_task_done(self, task_id, phase_seq, phase_label, done_text, from_bot, record_id):
    task = self._task_registry.get(task_id)
    if not task:
        # 找不到 task — 走 fallback 独立处理
        return await self._execute_task(
            task_id=task_id, summary=done_text,
            description=f"[孤立 done] task_id={task_id}", ...
        )

    # 1. 标记任务结束
    task.status = "done"

    # 2. 组装给主 session 的 prompt — 这是关键
    prompt = self._assemble_done_prompt(task, done_text)

    # 3. 调用 _execute_task 触发 Claude turn（创建新的螃蟹卡片）
    await self._execute_task(
        task_id=record_id,  # 用 Firestore record_id 做防重复
        summary=f"✅ {task.task_name}",
        description=f"来自 {from_bot} 的多阶段任务总结",
        inbox_from=from_bot,
        inbox_record_id=record_id,
        prompt_override=prompt,  # 关键：用我们组装的 prompt
    )

    # 4. 可选：把任务从 registry 移走（保留 30min 用于 UI 引用）
    asyncio.create_task(self._gc_task_state(task_id, delay=1800))


def _assemble_done_prompt(self, task: _TaskState, done_text: str) -> str:
    """组装多阶段任务的总结 prompt 给 Claude"""
    lines = [
        f"# 多阶段任务完成 — {task.task_name}",
        f"",
        f"由 `{task.worker_bot}` 执行，task_id=`{task.task_id}`",
        f"开始时间: {task.kickoff_at.isoformat()}",
        f"结束时间: {datetime.utcnow().isoformat()}",
        f"",
        f"## 过程回顾（共 {len(task.progress_buffer)} 个阶段）",
        f"",
    ]
    for b in task.progress_buffer:
        lines.append(f"### 阶段 {b['seq']}: {b['label']}")
        lines.append(f"_{b['ts'].isoformat()}_")
        lines.append("")
        lines.append(b["content"])
        lines.append("")

    lines.extend([
        f"## 最终结论",
        f"",
        done_text,
        f"",
        f"---",
        f"请基于以上完整过程给我一个综合分析。",
    ])
    return "\n".join(lines)
```

### 3.5 BotCore 改造（极小）

需要在 `BotCore.handle_message()` 接受 `prompt_override` 参数 — 当 inbox done 已经组装好 prompt 时，直接用，不再用 `instruction` 字段生成默认 prompt。

```python
# closecrab/core/bot.py
async def handle_message(
    self, message: UnifiedMessage,
    prompt_override: Optional[str] = None,  # 新增
):
    ...
    prompt = prompt_override or self._build_default_prompt(message)
    ...
```

### 3.6 System Prompt 协议教育（所有 bot）

`closecrab/main.py:build_system_prompt()` 加一段（所有 channel 都加）：

```markdown
## Inbox 多阶段任务协议

当你给其他 bot 派多阶段任务，或者用 inbox-send.py 给主 bot 汇报时，必须用 task_id 串起来：

```bash
# 开任务（自动拿 task_id）
TASK_ID=$(python3 ~/CloseCrab/scripts/inbox-send.py <主bot> "<开始描述>" \
    --task-name "<任务名 ≤80 字符>" --phase kickoff)

# 中间进度（每阶段一条）
python3 ~/CloseCrab/scripts/inbox-send.py <主bot> "<阶段内容>" \
    --task-id $TASK_ID --phase progress --phase-seq <序号> --phase-label "<短标签 ≤30 字符>"

# 结束（必须只发 1 条）
python3 ~/CloseCrab/scripts/inbox-send.py <主bot> "<最终结论 + 完整结果>" \
    --task-id $TASK_ID --phase done --phase-label "<完成标签>"
```

**关键规则**：
- 同一任务的所有消息**必须**用同一个 task_id
- progress 消息只显示给用户看，不触发主 bot 的 LLM 处理（节省 token）
- done 消息触发主 bot 的 1 次 LLM 处理，prompt 自动包含所有 progress
- done 消息里应该写**完整的最终结论**（不要假设主 bot 会回看 progress — 它能看到，但 done 自己就该是 self-contained）
- 不知道阶段数时，phase_seq 用单调递增的整数（1, 2, 3, ...）
- 单条消息任务（没有进度）跳过 kickoff/progress，只发 done 也可以；或者完全不传 phase（fallback 现有行为）
```

## 4. 边界情况

| Case | 行为 |
|------|------|
| jarvis 在 task 中崩溃，重启 | `_task_registry` 是内存，重启后丢；继续到来的 progress/done 找不到 task，走 fallback 独立处理。V1 接受。 |
| done 永远不到（worker bot 卡死） | 后台 GC task 检测 `now - last_update_at > 60min` 的 active task，自动当 done 处理（用兜底文本 "任务超时未收到 done"）。V1 不实现，V2 再加。 |
| jarvis 完全不知道某个 task_id（直接来了个 progress） | Fallback：当独立消息处理。**不**自动建 task。 |
| 重复的 phase_seq | 后到的覆盖先到的（同 seq 用最新内容） |
| phase_seq 乱序到达（先 2 后 1） | buffer 内部按 seq 排序，prompt 组装时按 seq 顺序 |
| 嵌套子任务 | V1 不支持 — `parent_task_id` 字段保留 |
| 多 bot 并发派活 | progress 完全旁路 BotCore，O(ms) 延迟，不排队；只有 done 走主 session worker pipe，对 N≤5 并发任务是 acceptable serialization |
| Discord channel 收到带 task_id 的 inbox | V1 Discord 不实现新协议；fallback 走老路径（视作独立消息）。V2 同步加。 |

## 5. 飞书 UI Timeline

```
┌────────────────────────────────────────────┐
│ 📋 测试 GPU 训练 Qwen3.5                    │
│ 任务开启 | 来自 xiaoaitongxue | ID: a3f2c1 │
└────────────────────────────────────────────┘
   ⏳ 进度 1: GPU 检测   (灰色小卡片)
   ⏳ 进度 2: TPU 初始化
   ⏳ 进度 3: 数据加载
   ⏳ 进度 4: 模型 forward
   ⏳ 进度 5: 模型 backward
┌────────────────────────────────────────────┐
│ ✅ 测试 GPU 训练 Qwen3.5  ← 这个触发 jarvis 处理
│ 总结：全部通过，结论 XXX YYY                │
└────────────────────────────────────────────┘
   🦀 jarvis 思考中... (新的螃蟹动画卡片)
   🦀 jarvis 回复：基于这 5 阶段的结果，我建议...
```

进度小卡片不带螃蟹动画，避免视觉混乱。

## 6. 实施顺序

1. **Stage A** — `scripts/inbox-send.py` 加 CLI flag + 自动 task_id 生成 (~30min)
2. **Stage B** — `closecrab/utils/firestore_inbox.py` 扩展 schema 读取 + callback 签名 (~20min)
3. **Stage C** — `closecrab/channels/feishu.py` 加 `_task_registry` + 3 个 handler (~90min)
4. **Stage D** — `closecrab/core/bot.py` 加 `prompt_override` 参数 (~15min)
5. **Stage E** — `closecrab/main.py:build_system_prompt()` 加协议教育段 (~15min)
6. **Stage F** — `scripts/test-inbox-protocol.py` mock 5 progress + 1 done，验证 (~30min)
7. **Stage G** — Chris SIGTERM 重启 jarvis（jarvis 不能自杀），用小爱同学跑真实测试
8. **Stage H** — 单 bot 测试 pass → Chris 决定是否上多 bot 并发测试

## 7. 测试矩阵

| 测试 | 期望 |
|------|------|
| Mock 5 progress + 1 done，task_id 一致 | 飞书显示 1 主卡 + 5 小卡 + 1 done 卡，主 session 只触发 1 次 turn，prompt 包含全部 5 个阶段 |
| 老 inbox-send.py 用法（不带 flag） | 行为不变，每条消息独立触发 turn |
| Kickoff 之后 progress 乱序（seq 3, 1, 2, 5, 4） | buffer 内部排序，done prompt 中按 1-5 顺序展示 |
| 没有 kickoff，直接进 progress | Fallback 独立处理（不自动建 task） |
| 没有 progress，直接 kickoff → done | done prompt 中"过程回顾"为空，但任务名 + done 内容仍正常 |
| 同 task_id 重复 seq | 后到的覆盖先到的 |
| jarvis 重启后收到老 task 的 progress | Fallback 独立处理 |
| 同时派 2 个 task（不同 task_id） | 2 个独立 timeline，互不影响 |

## 8. V2 Roadmap（不在本次实施范围）

- Discord channel 实现新协议
- Task 持久化（jarvis 重启后从 Firestore replay）
- 60min done timeout 兜底
- 嵌套子任务（`parent_task_id`）
- 任务取消 / pause / resume
- `task_id` short hash 冲突检测
- Web UI 看 task 进度
- **给 inbox task 用独立 user_key** — 实现真正的 done turn 并发，绕开 BotCore per-user lock 串行

## 9. Bonus 修复：Firestore SDK Watch race (2026-05-21)

### 触发场景

V1 协议 Stage G 实测 (2026-05-21 04:44) 时撞上：小爱 startup 短时间内收到 4 条积压 inbox，Firestore SDK `BackgroundConsumer` thread 处理 ADDED 事件期间，`_snapshot_callback` 字段被 main thread race 成 None → `TypeError: 'NoneType' object is not callable` → thread die → listener 后续静默不工作，老版本 1 小时后 `RESUBSCRIBE_INTERVAL` 才自愈。

### SDK 内部缺陷

```python
# google/cloud/firestore_v1/watch.py:572
self._snapshot_callback(keys, appliedChanges, read_time)
# B thread (BackgroundConsumer) dereference 这一刻,
# A thread (main / unsubscribe / 错误处理) 可能把 _snapshot_callback 设 None
# 没有 lock, 没有 local snapshot, 没有 atomic check-and-call
```

### 修复 (closecrab/utils/firestore_inbox.py)

```python
HEALTH_CHECK_INTERVAL = 60   # 1h 兜底太慢，缩到 60s

async def _periodic_health_check(self):
    """看 listener.is_active 是否活, 死了主动 re-subscribe."""
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        if self._listener is None:
            continue
        if self._listener.is_active:
            continue
        log.warning("listener DEAD, re-subscribing...")
        old = self._listener
        self._listener = None
        try: old.unsubscribe()
        except: pass
        self._subscribe()
```

`listener.is_active` 是 Watch 的公开 `@property` (返回 `_consumer is not None and _consumer.is_active`)，是 BackgroundConsumer daemon thread 活不活的稳定信号。

**加上 `_on_snapshot` 顶层 try/except** 作 regression 保险：防我们 callback 内部出错把 thread 也带死。

### 实测验证

- 3 个 mock 测试 PASS (dead-listener re-subscribe / callback exception suppressed / V1 协议 8 mock 无回归)
- 生产 5+ 分钟真实负载零 false positive
- 配合 RESUBSCRIBE 1h 周期双层防御：health check 接 sudden death (race)，RESUBSCRIBE 接 slow degradation (gRPC stream 慢慢断)
