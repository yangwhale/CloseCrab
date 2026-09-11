"""「连着但聋」自愈的判定测试。

两类故障，都在 2026-09-11 实测到过，都表现为「只能说不能听」而日志里一句 ERROR 都没有：

**① DAVE 掉出 MLS 树**（02:34:03 与 02:42:25 两次）。Discord 下发
session_description → py-cord 把 MLS 会话整个 reset 并重发 key package →
**对端再没回 proposals**。树是空的，`dave.ready` 从此钉死 False。Chris 那边的
表现是「必须退出语音频道再进来才听得到」。

**② 守护攥着一条已经断掉的 VoiceClient**（02:50:27）。每次重连都新造一个
VoiceClient 挂到 `guild.voice_client`，而 `_voice_heartbeat` 的 rejoin
**从不回写 `_listen_vc`** —— 守护于是每 0.3 秒对着一具尸体 `continue` 一次，
而心跳看 `guild.voice_client` 觉得一切正常。**两边都没错，但没人负责把新连接
交给守护。**

所以这里测的是**判定**：什么时候该动手、什么时候绝对不许动。真正的重连动作
`_force_dave_rejoin` 在测试里被替换成记录器 —— 它要碰真的 Discord 连接，
不是单测能覆盖的东西。
"""
import asyncio
import logging

import pytest

import closecrab.voice.discord_voice_sidecar as s


class _FakeMember:
    def __init__(self, mid, bot=False):
        self.id = mid
        self.bot = bot


class _FakeGuild:
    def __init__(self, members):
        self.me = _FakeMember(1)
        self._members = {m.id: m for m in members}
        self.voice_client = None

    def get_member(self, uid):
        return self._members.get(uid)


class _FakeChannel:
    def __init__(self, guild, members):
        self.guild = guild
        self.members = members
        self.voice_states = {m.id: object() for m in members}


class _FakeDave:
    def __init__(self, ready):
        self.ready = ready
        self.epoch = None


class _FakeConn:
    def __init__(self, dave):
        self.dave_session = dave
        self.ssrc_user_map = {}


class _FakeVC:
    def __init__(self, dave, connected=True, humans=(2,), recording=True):
        self._connection = _FakeConn(dave)
        self._connected = connected
        self._recording = recording
        self.started = []
        members = [_FakeMember(1, bot=True)] + [_FakeMember(h) for h in humans]
        self.guild = _FakeGuild(members)
        self.channel = _FakeChannel(self.guild, members)

    def is_recording(self):
        return self._recording

    def is_connected(self):
        return self._connected

    def start_recording(self, sink, cb):
        self.started.append(sink)
        self._recording = True


class _FakeSink:
    def __init__(self, vc=None):
        self.vc = vc

    def hits(self):
        return 0


class _FakeBot:
    def __init__(self, guild):
        self.guilds = [guild]


async def _drive(seconds=0.4):
    """把守护循环跑一小会儿。"""
    task = asyncio.create_task(s._ssrc_infer_loop(period=0.01))
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _run_loop(monkeypatch, *, ready, connected=True, grace=0.05,
                    cooldown=60.0, listen_active=True, humans=(2,),
                    reconnecting=False, seconds=0.4):
    """跑守护，返回 `_force_dave_rejoin` 被调用的理由列表。"""
    dave = _FakeDave(ready)
    vc = _FakeVC(dave, connected=connected, humans=humans)

    monkeypatch.setattr(s, "_listen_active", listen_active)
    monkeypatch.setattr(s, "_listen_vc", vc)
    monkeypatch.setattr(s, "_stt_sink", _FakeSink(vc))
    monkeypatch.setattr(s, "_voice_reconnecting", reconnecting)
    monkeypatch.setattr(s, "_DAVE_UNREADY_GRACE_S", grace)
    monkeypatch.setattr(s, "_DAVE_REJOIN_COOLDOWN_S", cooldown)
    monkeypatch.setattr(s, "_dave_unready_since", None)
    monkeypatch.setattr(s, "_dave_last_rejoin", 0.0)
    # 认领分支只在手里的句柄是死的时才走；这些用例给的是活句柄，但仍然把
    # _sidecar_bot 置空，免得它误入。
    monkeypatch.setattr(s, "_sidecar_bot", None)

    calls = []

    async def _fake_rejoin(reason):
        calls.append(reason)
        return True

    monkeypatch.setattr(s, "_force_dave_rejoin", _fake_rejoin)

    await _drive(seconds)
    return calls


# ─────────────────────────── ① DAVE 掉出 MLS 树 ───────────────────────────

@pytest.mark.asyncio
async def test_rejoin_fires_after_grace(monkeypatch):
    """真人在场 + ready 连续为 False 超过余量 → 必须强制重连。"""
    calls = await _run_loop(monkeypatch, ready=False)
    assert calls, "MLS 树塌了却没人管 —— 这就是「必须退出重进才听得到」的成因"
    assert "DAVE ready" in calls[0]


@pytest.mark.asyncio
async def test_no_rejoin_while_ready(monkeypatch):
    """**负向用例**：ready 正常时一次都不许动。

    没这条的话，把判定写成「无条件重连」也能让上面那条全绿 —— 而无条件重连
    会每 25 秒把语音掐断一次，比原来的病还糟。
    """
    calls = await _run_loop(monkeypatch, ready=True)
    assert calls == [], f"连接健康却被重连了 {len(calls)} 次"


@pytest.mark.asyncio
async def test_no_rejoin_when_nobody_else_in_channel(monkeypatch):
    """**负向用例**：房里只有 bot 自己时，ready=False 是正常的空闲态。

    没有第二个人就没有 MLS 群要建。实测 Chris 不在的那一小时里（诊断 #620 /
    #4610 / #8610）ready 一直是 False —— 少了这条判据，bot 会在没人的时候每
    60 秒自残一次重连，而且永远不会停。
    """
    calls = await _run_loop(monkeypatch, ready=False, humans=())
    assert calls == [], "房里没人却把空闲当成了故障"


@pytest.mark.asyncio
async def test_no_rejoin_before_grace(monkeypatch):
    """**负向用例**：余量没到就不能动。

    正常的 MLS 握手中途 ready 本来就是 False（实测 <100ms），沉不住气就会
    把每一次正常换钥匙都打断成一次重连。
    """
    calls = await _run_loop(monkeypatch, ready=False, grace=10.0, seconds=0.3)
    assert calls == [], "余量还没到就重连了"


@pytest.mark.asyncio
async def test_no_rejoin_when_disconnected(monkeypatch):
    """**负向用例**：已经断线的连接不归这里管（断线自愈是心跳和认领分支的活）。"""
    calls = await _run_loop(monkeypatch, ready=False, connected=False)
    assert calls == [], "断线状态被误判成「连着但聋」"


@pytest.mark.asyncio
async def test_no_rejoin_while_another_healer_reconnects(monkeypatch):
    """**负向用例**：已经有人在重建连接时，这里必须让开。

    2026-09-11 02:50:27 实测：守护刚断开准备重连，30 秒一轮的心跳撞上这个窗口，
    看见「掉线」也去重连，把守护那条已经握完手的连接 Terminating 掉，报
    `ClientConnectionResetError: Cannot write to closing transport`。
    """
    calls = await _run_loop(monkeypatch, ready=False, reconnecting=True)
    assert calls == [], "别人正在重连，这里还去插一脚"


@pytest.mark.asyncio
async def test_cooldown_limits_rejoin_rate(monkeypatch):
    """冷却期内只许重连一次 —— 它是对端一直不搭理时唯一的刹车。"""
    calls = await _run_loop(monkeypatch, ready=False, grace=0.02,
                            cooldown=30.0, seconds=0.4)
    assert len(calls) == 1, f"冷却没生效，0.4 秒里重连了 {len(calls)} 次"


@pytest.mark.asyncio
async def test_rejoin_failure_does_not_kill_the_loop(monkeypatch, caplog):
    """重连动作本身抛异常时，守护必须活着进下一轮。

    这是 2026-09-11 四小时失聪的教训的延伸：新加的分支同样跑在那个循环里，
    它抛异常一样会把整个守护带走。
    """
    dave = _FakeDave(False)
    vc = _FakeVC(dave)
    monkeypatch.setattr(s, "_listen_active", True)
    monkeypatch.setattr(s, "_listen_vc", vc)
    monkeypatch.setattr(s, "_stt_sink", _FakeSink(vc))
    monkeypatch.setattr(s, "_voice_reconnecting", False)
    monkeypatch.setattr(s, "_sidecar_bot", None)
    monkeypatch.setattr(s, "_DAVE_UNREADY_GRACE_S", 0.02)
    monkeypatch.setattr(s, "_DAVE_REJOIN_COOLDOWN_S", 0.0)
    monkeypatch.setattr(s, "_dave_unready_since", None)
    monkeypatch.setattr(s, "_dave_last_rejoin", 0.0)
    caplog.set_level(logging.ERROR, logger="closecrab.discord_voice_sidecar")

    async def _boom(reason):
        raise RuntimeError("重连炸了")

    monkeypatch.setattr(s, "_force_dave_rejoin", _boom)

    task = asyncio.create_task(s._ssrc_infer_loop(period=0.01))
    await asyncio.sleep(0.2)
    alive = not task.done()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert alive, "重连失败把守护带走了"
    assert any("守护本轮出错" in r.getMessage() for r in caplog.records)


# ──────────────────── ② 悬空的 _listen_vc：认领活着的那条 ────────────────────

async def _run_adopt(monkeypatch, *, dead_connected, reconnecting=False,
                     live_present=True, seconds=0.2):
    """摆一个「守护手里的句柄 vs py-cord 手里的句柄」的局，返回 (dead, live)。"""
    dead = _FakeVC(_FakeDave(True), connected=dead_connected, recording=False)
    live = _FakeVC(_FakeDave(True), connected=True, recording=False)
    bot = _FakeBot(live.guild)
    live.guild.voice_client = live if live_present else None

    monkeypatch.setattr(s, "_listen_active", True)
    monkeypatch.setattr(s, "_listen_vc", dead)
    monkeypatch.setattr(s, "_stt_sink", _FakeSink(dead))
    monkeypatch.setattr(s, "_voice_reconnecting", reconnecting)
    monkeypatch.setattr(s, "_sidecar_bot", bot)
    monkeypatch.setattr(s, "_get_stt_sink_class", lambda: _FakeSink)
    monkeypatch.setattr(s, "_on_recording_done", lambda *a, **k: None)

    await _drive(seconds)
    return dead, live


@pytest.mark.asyncio
async def test_adopts_live_client_when_handle_is_dead(monkeypatch):
    """手里的连接断了、py-cord 有一条活的 → 认领它并重新开录。

    少了这一步，重连之后守护就是对着尸体空转，bot 永久只能说不能听 ——
    2026-09-11 02:50 实测钉死在这个状态五分钟，日志里一条 ERROR 都没有。
    """
    dead, live = await _run_adopt(monkeypatch, dead_connected=False)
    assert live.started, "没认领活着的那条连接，守护还攥着尸体"
    assert s._listen_vc is live, "_listen_vc 没更新，下一轮还会走回老路"


@pytest.mark.asyncio
async def test_no_adopt_when_handle_is_healthy(monkeypatch):
    """**负向用例**：手里那条还连着就别动它。

    无条件认领会在每一轮都换 sink 重开录音，等于把好好的收音链路反复推倒。
    """
    dead, live = await _run_adopt(monkeypatch, dead_connected=True)
    assert not live.started, "连接是好的却被换掉了"


@pytest.mark.asyncio
async def test_no_adopt_while_another_healer_reconnects(monkeypatch):
    """**负向用例**：别人正在重连时不许认领。

    握手途中 `guild.voice_client` 会短暂指向一条还没建好的连接，急着认领
    就会在半成品上 start_recording。
    """
    dead, live = await _run_adopt(monkeypatch, dead_connected=False,
                                  reconnecting=True)
    assert not live.started, "重连进行中就去认领半成品连接了"


# ────────────────────────── 真人判定本身 ──────────────────────────

def test_human_ids_excludes_bots_and_self():
    """队友 bot 和自己都不算真人 —— 算进去会让唯一真人的 ssrc 永远绑不上。"""
    vc = _FakeVC(_FakeDave(True), humans=(2, 3))
    vc.channel.members.append(_FakeMember(9, bot=True))   # 队友 bot
    vc.channel.voice_states[9] = object()
    vc.guild._members[9] = _FakeMember(9, bot=True)
    assert s._channel_human_ids(vc) == {2, 3}


def test_human_ids_ignores_unresolvable_members():
    """voice_states 里有、guild 里解析不出来的成员不算数（py-cord "Skipping member"）。"""
    vc = _FakeVC(_FakeDave(True), humans=(2,))
    vc.channel.voice_states[77] = object()                # get_member 会返回 None
    assert s._channel_human_ids(vc) == {2}
