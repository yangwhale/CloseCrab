#!/usr/bin/env python3
"""把每个 bot 的房间**显式**建出来，并让它永不销毁。

为什么需要这个脚本，而不是只改 SFU 的 `room:` 默认段：

- 配置里的 `empty_timeout` 只在房间**已经存在**时管用。房间是「第一个人 join
  的时候自动建」的，所以在没人进去之前，`bunny` 这个房间根本不存在 ——
  agent job 也就没被派出去。用户点开网页那一刻才开始：建房 → 派 job →
  起子进程 → 加载 persona。那几秒就是「启动延迟」。
- CreateRoom 建出来的房间**优先于配置默认值**（config-sample.yaml 原话：
  "If rooms are created explicitly with CreateRoom, they will take precedence
  over defaults"），而且房间从这一刻起就在，agent 立刻被派进去等着。

⚠️ SFU 一重启，房间列表就没了（房间是内存态）。所以这个脚本要周期性跑
（systemd timer），不能只在部署时跑一次。它是幂等的：房间已存在就是更新一遍
超时设置，没有副作用。

凭据的唯一真相是 Firestore `config/livekit`，跟 livekit_out.py 同一份。
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

from google.cloud import firestore
from livekit import api

# 十年。写 0 会被服务端当成「用默认值」(300/20)，不是「不超时」。
_FOREVER = 315360000

# 必须跟 agent.py 里的 _AGENT_NAME 逐字一致 —— worker 用它注册，这里用它派发，
# 对不上就是「派了个没人接的活」，房间永远空着。
_AGENT_NAME = "gemini-live"

_PERSONA_DIR = pathlib.Path(__file__).parent / "personas"


def _rooms() -> list[str]:
    """房间名 = bot 名 = persona 文件名。persona 目录就是这套东西的花名册。"""
    return sorted(p.stem for p in _PERSONA_DIR.glob("*.md") if p.stem != "README")


def _creds() -> tuple[str, str, str]:
    # project 和 database 都必须显式传：默认 project 会跟着 ADC 走，默认
    # database 是 "(default)" —— 那个库是空的，查什么都是「不存在」，不报错。
    db = firestore.Client(
        project=os.getenv("FIRESTORE_PROJECT", "chris-pgp-host"),
        database=os.getenv("FIRESTORE_DATABASE", "closecrab"),
    )
    doc = db.collection("config").document("livekit").get().to_dict() or {}
    url = (doc.get("url") or "").strip()
    key = (doc.get("api_key") or "").strip()
    secret = (doc.get("api_secret") or "").strip()
    if not (url and key and secret):
        sys.exit("Firestore config/livekit 里缺 url / api_key / api_secret")
    # RoomService 走 HTTP，不是 websocket 的信令口。
    return url.replace("wss://", "https://").replace("ws://", "http://"), key, secret


async def _has_agent(lk: api.LiveKitAPI, room: str) -> bool:
    """房间里有没有一个活着的 gemini agent。

    判据是**参与者列表这个事实**，不是派发记录 —— worker 一重启，job 就没了，
    但派发记录不一定跟着消失，照着它判会漏补。

    注意房间里的 AGENT 不止一个：`<bot>-speaker` 是 bot 本体那条只推不收的
    音频流（livekit_out.py），它也是 kind=AGENT。所以按身份前缀区分，
    LiveKit 给 job 起的身份固定是 `agent-<job_id>`。
    """
    res = await lk.room.list_participants(api.ListParticipantsRequest(room=room))
    return any(p.identity.startswith("agent-") for p in res.participants)


async def _reclaim(lk: api.LiveKitAPI, room: str) -> int:
    """把房间里所有 agent 参与者踢掉，返回踢了几个。

    **只在 worker 刚重启时用**（`--reclaim`）。那一刻旧 job 全都已经死了，
    但 SFU 的参与者列表还挂着它们的尸体 —— 实测 15 秒都清不干净。不清掉有两个
    后果：下面的「在岗」判定被骗过去、房间不补派；以及前端进来会订阅到一堆
    已经不会发声的死轨。

    ⚠️ 常规巡检（不带 --reclaim）**绝不能**走这里 —— 那会把正在服务的 agent
    踢下线。
    """
    res = await lk.room.list_participants(api.ListParticipantsRequest(room=room))
    stale = [p.identity for p in res.participants if p.identity.startswith("agent-")]
    for identity in stale:
        await lk.room.remove_participant(
            api.RoomParticipantIdentity(room=room, identity=identity)
        )
    return len(stale)


async def main() -> None:
    # worker 刚重启时旧 agent 的尸体还挂在房间里，参与者列表不可信 ——
    # 先清场再无条件补派。ExecStartPost 走这条，5 分钟的巡检 timer 不走。
    reclaim = "--reclaim" in sys.argv

    url, key, secret = _creds()
    lk = api.LiveKitAPI(url, key, secret)
    try:
        for name in _rooms():
            room = await lk.room.create_room(
                api.CreateRoomRequest(
                    name=name,
                    empty_timeout=_FOREVER,
                    departure_timeout=_FOREVER,
                )
            )
            if reclaim:
                note = f"清掉 {await _reclaim(lk, name)} 个旧 agent，重新派发"
            elif await _has_agent(lk, name):
                print(f"{name}: sid={room.sid} agent 在岗")
                continue
            else:
                note = "缺 agent，补派"
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(agent_name=_AGENT_NAME, room=name)
            )
            print(f"{name}: sid={room.sid} participants={room.num_participants} {note}")
    finally:
        await lk.aclose()


if __name__ == "__main__":
    asyncio.run(main())
