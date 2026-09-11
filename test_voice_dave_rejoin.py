"""「连着但聋」自愈的判定测试。

背景（2026-09-11 实测两次，02:34:03 与 02:42:25）：Discord 下发
session_description → py-cord 把 MLS 会话整个 reset 并重发 key package →
**对端再没回 proposals**。树是空的，`dave.ready` 从此钉死 False。这条连接
从那一刻起既解不开也加不了密，但出站 TTS 照发不误 —— 又是一个从外面看
完全正常的静默故障。Chris 那边的表现是「必须退出语音频道再进来才听得到」。

所以这里测的是**判定**：什么时候该强制重连、什么时候绝对不许动。
真正的重连动作 `_force_dave_rejoin` 在测试里被替换成记录器 —— 它要碰
真的 Discord 连接，不是单测能覆盖的东西。
"""
import asyncio
import logging

import pytest

import closecrab.voice.discord_voice_sidecar as s


class _FakeDave:
    def __init__(self, ready):
        self.ready = ready
        self.epoch = None


class _FakeConn:
    def __init__(self, dave):
        self.dave_session = dave
        self.ssrc_user_map = {}


class _FakeVC:
    def __init__(self, dave, connected=True):
        self._connection = _FakeConn(dave)
        self._connected = connected

    def is_recording(self):
        return True

    def is_connected(self):
        return self._connected


class _FakeSink:
    def __init__(self, vc):
        self.vc = vc

    def hits(self):
        return 0


async def _run_loop(monkeypatch, *, ready, connected=True, grace=0.05,
                    cooldown=60.0, listen_active=True, seconds=0.4):
    """把守护循环跑一小会儿，返回 `_force_dave_rejoin` 被调用的理由列表。"""
    dave = _FakeDave(ready)
    vc = _FakeVC(dave, connected=connected)

    monkeypatch.setattr(s, "_listen_active", listen_active)
    monkeypatch.setattr(s, "_listen_vc", vc)
    monkeypatch.setattr(s, "_stt_sink", _FakeSink(vc))
    monkeypatch.setattr(s, "_DAVE_UNREADY_GRACE_S", grace)
    monkeypatch.setattr(s, "_DAVE_REJOIN_COOLDOWN_S", cooldown)
    monkeypatch.setattr(s, "_dave_unready_since", None)
    monkeypatch.setattr(s, "_dave_last_rejoin", 0.0)

    calls = []

    async def _fake_rejoin(reason):
        calls.append(reason)
        return True

    monkeypatch.setattr(s, "_force_dave_rejoin", _fake_rejoin)

    task = asyncio.create_task(s._ssrc_infer_loop(period=0.01))
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return calls


@pytest.mark.asyncio
async def test_rejoin_fires_after_grace(monkeypatch):
    """ready 连续为 False 超过余量 → 必须强制重连。"""
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
async def test_no_rejoin_before_grace(monkeypatch):
    """**负向用例**：余量没到就不能动。

    正常的 MLS 握手中途 ready 本来就是 False（实测 <100ms），沉不住气就会
    把每一次正常换钥匙都打断成一次重连。
    """
    calls = await _run_loop(monkeypatch, ready=False, grace=10.0, seconds=0.3)
    assert calls == [], "余量还没到就重连了"


@pytest.mark.asyncio
async def test_no_rejoin_when_disconnected(monkeypatch):
    """**负向用例**：已经断线的连接不归这里管。

    断线自愈是 `_voice_heartbeat` 的活。两边都抢着重连会互相打断，
    而且断线时 ready 天然是 False，不排除的话这里会一直空转。
    """
    calls = await _run_loop(monkeypatch, ready=False, connected=False)
    assert calls == [], "断线状态被误判成「连着但聋」"


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
