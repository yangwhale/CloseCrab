# Bot 状态协议 —— bot 不说话的时候，它在干什么

**一句话**：把「这一轮 bot 在忙什么」从 CloseCrab 推到 LiveKit 房间里，
客户端拿去显示。服务端在这个仓库，客户端在 `agent-starter-swift`。

> **这份文档是那个 JSON 的单一来源。** 在它存在之前，契约只以注释的形式
> 散在两个仓库的两个文件里 —— 改一边忘另一边，症状是「屏幕上显示的和
> 实际不符」，而那种错看起来永远很合理。

## 一、为什么要有

语音 bot 有一段天然的沉默期：你说完了，它在干活，还没开口。这段可能是
三秒，也可能是三分钟。期间用户唯一能做的判断是「它是不是死了」。

更要命的是其中一种沉默**需要你动手** —— 它在等你批准方案、等你回答问题、
等你点允许某个工具。这种沉默和「它在忙」长得一模一样，而代价完全不同：
前者你不动它就永远不动。

所以这块屏要回答的不是「它在干什么」，是**「我要不要管它」**。

## 二、两条通道，性质不同

| | 走哪 | 为什么 |
|---|---|---|
| 当下状态 | 参与者属性 `cc.bot.state` | **有持久性**。客户端中途进房，立刻看到现况 |
| 滚动流水 | 数据包 `cc.bot.step`（不可靠档） | 过期就没用，丢了无所谓，频率高 |

**判据只有一句：客户端晚来一秒，还需要它吗。**

需要 → 属性。属性是覆盖语义、有持久性，新参与者一进房就能读到当前值。
代价是每改一次走一趟信令，房间里每个人都要收。

不需要 → 数据包。走不可靠档，不重传。「刚才读了哪个文件」晚到一秒就是
废信息，为它重传是在浪费带宽。

服务端实现：`closecrab/voice/livekit_out.py` 的 `publish_state()` /
`publish_step()`。

### ⚠️ 限频只拦中间态

属性最快 0.5 秒发一次（`_STATE_MIN_INTERVAL`），**但这个限频只作用在
`on=true` 的中间态上**：

```python
if payload.get("on") and now - _last_state_at < _STATE_MIN_INTERVAL:
    return
```

turn 结束那一下必须立刻出去。被限掉的话屏幕会停在「还在跑」，
直到下一次状态变化 —— 而那可能是几分钟后的下一轮。
**停在「还在跑」比不显示更糟**，它是一条确定的假信息。

同理，`bot.py` 的 `finally` 块里**无论怎么结束都强制发一次**。正常路径靠
`result` 事件收尾，但被打断、抛异常、或者某个 worker 压根不发 `result`
的时候就到不了。

## 三、`cc.bot.state` 字段全表

一个 JSON 对象，由 `closecrab/core/agent_state.py` 的 `snapshot()` 产出。
**字段名都很短，因为要塞进参与者属性。**

| 字段 | 类型 | 含义 |
|---|---|---|
| `v` | int | 协议版本，当前 `1` |
| `on` | bool | 主 turn 在不在跑 |
| `wait` | string | **在等人**。值就是等什么；空串＝没在等 |
| `act` | string | 主 agent 此刻在干啥（人话，见下） |
| `sec` | float | 这个 turn 跑了多少秒；结束后冻住 |
| `subs` | `{run,done}` | 子 agent：几个在跑、几个完了 |
| `bg` | `{run,done}` | 后台命令：同上，**跟子 agent 分开数** |
| `tasks` | array | 每条任务的明细，见下表 |
| `unlinked` | int | 挂不到任何任务上的动作数。**不藏** |

`tasks[]` 每一条：

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | string | 任务号前 8 位 |
| `sub` | bool | `true`＝真子 agent，`false`＝后台命令 |
| `kind` | string | 子 agent 类型（`general-purpose` 之类）；后台命令为空 |
| `what` | string | 派它去干什么，截 60 字符 |
| `act` | string | 它此刻在干啥 |
| `st` | string | `running` / `completed` / `failed` / `unknown` |
| `sec` | float | 跑了多久 |
| `sum` | string | 干完那句摘要，截 120 字符 |

`cc.bot.step` 数据包：

| 字段 | 类型 | 含义 |
|---|---|---|
| `v` | int | `1` |
| `sub` | string | 哪个子 agent 发的（`parent_tool_use_id` 前 12 位），空＝主 agent |
| `lines` | string[] | 几行文字，每行截 180 字符 |

### `act` 说的是人话，不是工具名

`describe_tool()` 把一次工具调用翻成一句话：「在读 `xxx.py`」「在跑
`<命令>`」「在搜 `<词>`」「在派活」「在查 wiki」。

**不认识的工具如实说「在用 `<工具名>`」，不给它编一个动词。**
屏幕上编出来的动词看着比工具名自然，但它是假的。

## 四、三条必须知道的设计判断

### 1. `task_started` 不只发给子 agent

后台 Bash 也发。判别依据是事件里带不带 `subagent_type`：真子 agent 带，
后台任务不带。

> **这条是实测抓出来的，不是约定。** 上线第一分钟屏幕就显示「1 个子 agent
> 已完成」，而那其实是一条后台命令。混在一起的话，随手跑条后台命令，
> 屏幕上就多一个假的子 agent。

所以 `subs` 和 `bg` 是两组数，界面上也分开显示。

### 2. `unknown` 不等于 `completed`

turn 都结束了还挂着 `running` 的任务，说明我们**漏收了收尾事件**。
这时候标 `unknown`，不标 `completed`。

界面照着显示 —— 问号，不是绿勾。实测两条后台任务里就有一条是
`unknown`，**这不是罕见情况**。

### 3. `unlinked` 不能藏

Claude 给的事件里，`task_started` **没有** tool_use_id，`user` 事件
**没有** task_id —— 这两个标识符官方没连起来。要把「某个子 agent 此刻在
干啥」挂到具体哪条任务上，只能靠「`Agent` 工具调用之后紧跟的那条
`task_started` 就是它」这个顺序假设。

**这个假设我们自己盯着**：连不上就 `unlinked += 1`，不假装连上了。

这个数不为 0，就说明 `tasks[]` 那份列表是不完整的。藏起来的话屏幕会
安静地少一块，而看的人完全不知道少了。

## 五、事件是怎么来的

2026-09-20 实测抓的原始流，不是推的。派一个子 agent，CLI 依次吐出：

```
assistant   tool_use name=Agent          id=toolu_xxx
system      subtype=task_started         task_id=af70… subagent_type=general-purpose
user        parent_tool_use_id=toolu_xxx subagent_type=… task_description=…
system      subtype=task_updated         task_id=af70…
system      subtype=task_notification    task_id=af70… status=completed summary=…
result      subtype=success              num_turns=2
```

**有专门的任务生命周期事件**，不用从 `parent_tool_use_id` 硬推。

## 六、代码在哪

| 这一半 | 仓库 | 文件 |
|---|---|---|
| 攒状态（纯逻辑，无 IO） | CloseCrab | `closecrab/core/agent_state.py` |
| 测试（16 例，杀 5 个变异体） | CloseCrab | `test_agent_state.py` |
| 发出去 | CloseCrab | `closecrab/voice/livekit_out.py` |
| 接进事件流 | CloseCrab | `closecrab/core/bot.py` 的 `_on_step` ＋ `finally` |
| 收 | agent-starter-swift | `VoiceAgent/CloseCrab/CCBotStatus.swift` |
| 画 | agent-starter-swift | `VoiceAgent/CloseCrab/CCBotStatusPanel.swift` |

**状态机单独一个模块、不 import 任何 IO**，所以能在开发机上直接跑测试。
状态机错一格的后果是「屏幕上显示的和实际不符」—— 那种错只能靠测试拦，
靠真机看是看不出来的。

## 七、客户端侧三个坑（都踩过）

改 Swift 那半边之前先看这三条，省一轮编译：

1. **计时必须客户端自己驱动。** `sec` 是服务端发那一刻的值，属性限频 0.5 秒
   而且没变就不发 —— 一条跑三分钟的命令期间一条都不会来。不本地补时的话，
   计时会停在那儿，**而那正是你最想知道「它卡了多久」的时候**。

2. **`struct Task` 会遮蔽 `_Concurrency.Task`。** 同文件里的
   `Task { @MainActor in … }` 会被解析成「构造一个 Codable 的 Task」，
   报错写的是「trailing closure passed to parameter of type 'any Decoder'」，
   **完全不提重名**。结构体改名叫 `Job`。

3. **`SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor` 下，裸 enum 也是隔离的。**
   属性名常量要**逐个**标 `nonisolated`，光从 `@MainActor` 类里搬出来不够 ——
   `RoomDelegate` 的回调是 `nonisolated`，读不到被隔离的常量。

## 八、要改协议的时候

两个仓库都要动，而且**服务端先上、客户端后上**：旧客户端遇到多出来的字段
会忽略（`JSONDecoder` 默认行为），遇到少掉的字段会整条解不出来。

客户端解不出来时**保持上一份，不清空** —— 屏幕闪一下变空比停在旧值更糟，
旧值至少是真的发生过的。

字段语义变了（不是新增）就把 `v` 加一。
