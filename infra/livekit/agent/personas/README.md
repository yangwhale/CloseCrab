# `personas/` —— 按房间名选人格

文件名就是房间名，房间名就是 bot 名：浏览器开 `https://<域名>/?room=bunny`
→ 进房间 `bunny` → agent 读 `personas/bunny.md`。

一个 worker 进程伺候所有房间。LiveKit 给**每个房间**派一个独立的 job 进程，
job 进来读 `ctx.room.name` 自己认人格 —— 六个 bot 不需要六个 systemd unit。

## 文件格式

第一行可选 `voice: <名字>`，空一行，剩下全是 system instructions：

```
voice: Puck

你是 ...
```

只认 `voice:` 这一个头部字段。想加别的配置项之前先想清楚 ——
别在这儿长出第二套 YAML。

没有对应文件（或文件是空的）就退回 `agent.py` 里那份内置默认人格，日志会说。
房间名不合 `[a-z0-9][a-z0-9_-]{0,31}` 也直接走默认 —— 随机房间名
（`voice_assistant_room_1234`，也就是不带 `?room=` 进来的）走的就是这条。

## 正文写什么（六个文件共用一套骨架）

六份人格是从**同一个模板**生成的，只换 bot 名和声音。改哪一份之前，
先想清楚这条是「只有这个 bot 这样」还是「六个都该这样」——
后者要六份一起改，别让它们悄悄分叉。`agent.py` 里那份内置 `INSTRUCTIONS`
也是同一套骨架，改了骨架记得连它一起改，否则退回默认人格时行为就变了。

| 段落 | 管什么 |
|---|---|
| `# 你是谁` | **你是 {bot} 的语音助手，你自己不叫 {bot}** —— 不许应它的名，不许把它干的事说成自己干的 |
| `# 每一轮先判断：这一句该怎么接` | 分流规则，见下 |
| `# 说话方式` | 对方在「听」不是在「看」：不念列表 / 路径 / markdown 符号；结论先行；不播报工具调用 |
| `# 底线` | 不确定就说不确定；搜索顺序；**绝对不要 kill 跟 bot.py 有关的进程** |

分流那段是从 `CloseCrab/closecrab/voice/personas/_default.md` **改写**过来的，
不是照抄 —— 那边那句「这个答案在你脑子里，还是在这台机器上？」在这里不成立：
AutoMod 只有一个 `ask_{bot}` 工具、碰不到机器，而这个 agent 自己就能跑 bash。
所以线划在别处：

1. 张嘴就能答 → 直接答，不要为了显得严谨先搜一圈
2. 要看 / 要查 / 要算 → **自己动手，不用问任何人**
3. 有后果（删、kill、重启、改配置、push、往外发消息）→ 先说打算干什么再等点头
4. 大活儿 → 说「该由 {bot} 本体做」，**但不要假装已经派出去了**

第 4 条那个「不要假装」是有原因的：**这个 agent 手上没有派活的通道。**
它没有 `ask_bot` 工具，写 Firestore inbox 技术上能通，但 bot 的回复会落在飞书，
不会回到语音房间里 —— 那是个要设计的功能，不是随手能补的。

## 工具那边的约定（跟人格配套，改一头要看另一头）

- 工具描述写在 `agent.py` 里各个 `@function_tool` 的 **docstring** ——
  它就是下发给模型的 schema：`Args:` 之前那段是工具描述，`Args:` 里每一条
  变成对应参数的 `description`。写得含糊模型就选得含糊。
  （想确认实际下发成什么样：`llm.utils.build_legacy_openai_schema(tool, internally_tagged=True)`）
- **搜索顺序：`search_web`（Jina）在前，内置 `GoogleSearch` 垫底。**
  理由是可观测性，不是效果实测 —— Jina 那条是普通函数工具，查询词和四条结果
  都进我们自己的日志；`GoogleSearch` 的检索发生在 Google 服务端，这个进程
  一个字都看不到，出问题时「搜过了没搜着」和「压根没搜、凭记忆答的」长得一样。
- **没有查时间的工具**，那只是 `run_bash` 跑一条 `TZ=Asia/Hong_Kong date`。
  每多一个工具，模型每轮都要多读一份描述、多做一次选择 ——
  能被已有工具一行做掉的，就别单占一个 schema 槽位。

## 可用的 voice

30 个，抄自 plugin 源码里那个 `Voice` Literal
（`livekit/plugins/google/realtime/api_proto.py`），别照着博客写：

```
Achernar  Achird    Algenib   Algieba   Alnilam   Aoede
Autonoe   Callirrhoe Charon   Despina   Enceladus Erinome
Fenrir    Gacrux    Iapetus   Kore      Laomedeia Leda
Orus      Pulcherrima Puck    Rasalgethi Sadachbia Sadaltager
Schedar   Sulafat   Umbriel   Vindemiatrix Zephyr Zubenelgenubi
```

现在这几个文件的声音是**随手分的**，不是谁定下来的，觉得不对直接改第一行。

## 加一个新房间要动两处

1. 这里放一个 `<名字>.md`。
2. 前端 `.env.local` 的 `ALLOWED_ROOMS` 里加上这个名字 ——
   没在白名单里的 `?room=` 会被 token 端点直接 400。

白名单**故意在两个进程里各校验一次**：前端那份决定谁能签到 token，
这边那个正则决定房间名能不能拼进文件路径。前端松了，这边还挡着。

## 为什么人格不放 Firestore

`bots/{name}` 里根本没有人格字段（实测键只有 `active_channel` /
`description` / `model` / `worker_type` 这些），CloseCrab 的 system prompt 是
`main.py` 在运行时拼出来的。为了一个 description 把
`google-cloud-firestore` 拖进这个 venv 不划算。

⚠️ 改完人格要 `sudo systemctl restart lk-gemini-agent`，而且**还得重新进房间**
—— instructions 和工具都在会话建立时冻结（`mutable_tools=False`）。
