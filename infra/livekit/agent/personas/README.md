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
