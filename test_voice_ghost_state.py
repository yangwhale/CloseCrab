"""连语音频道之前，必须先清掉**服务端**残留的语音状态。

2026-09-11 实测事故：bunny 重启后连续 6 次握手超时，堆栈全停在

    discord/voice/state.py _wait_for_state(ConnectionFlowState.got_both_voice_updates)

即 py-cord 发出 VOICE_STATE_UPDATE 后，Discord 一直不回 VOICE_SERVER_UPDATE。
原因是服务端还记着上个进程留下的语音状态（exit-42 重启没走完优雅下线），
新进程发的 join 在 Discord 看来「状态没变化」，于是不回 server update。

旧代码只在 `guild.voice_client` 非 None 时 disconnect —— 那是**本地**对象，
全新进程里它就是 None，所以幽灵永远没人清，心跳重试多少次都撞同一堵墙。

下面第二个用例是护栏：进程刚起来（本地无 voice_client）时，也必须先发一次
channel=None。只测「本地有僵尸时会清」是测不出这个 bug 的。
"""
import asyncio

import pytest

import closecrab.voice.discord_voice_sidecar as s


class _FakeVC:
    def __init__(self, connected):
        self._connected = connected
        self.disconnected = False

    def is_connected(self):
        return self._connected

    async def disconnect(self, force=False):
        self.disconnected = True


class _FakeChannel:
    def __init__(self, calls, result_connected=True):
        self._calls = calls
        self._result = result_connected

    async def connect(self, timeout=None, reconnect=None):
        self._calls.append("connect")
        return _FakeVC(self._result)


class _FakeGuild:
    def __init__(self, calls, voice_client=None):
        self._calls = calls
        self.voice_client = voice_client

    async def change_voice_state(self, *, channel, self_mute=False, self_deaf=False):
        self._calls.append(f"change_voice_state:{channel}")


class _FakeBot:
    def __init__(self, guild, channel):
        self.guilds = [guild]
        self._channel = channel

    def get_channel(self, _id):
        return self._channel


def _setup(monkeypatch, local_vc):
    calls = []
    ch = _FakeChannel(calls)
    guild = _FakeGuild(calls, voice_client=local_vc)
    monkeypatch.setattr(s, "_sidecar_bot", _FakeBot(guild, ch))
    monkeypatch.setattr(s, "_target_voice_channel_id", 12345)
    return calls


def test_fresh_process_clears_server_state_before_connecting(monkeypatch):
    """**回归护栏**：本地没有 voice_client（进程刚起）时也必须先清服务端状态。

    旧代码在这里的调用序列只有 ['connect']，于是撞上幽灵、20s 超时、无限重试。
    """
    calls = _setup(monkeypatch, local_vc=None)
    vc = asyncio.run(s._ensure_connected())
    assert vc is not None
    assert calls == ["change_voice_state:None", "connect"], (
        f"必须先清服务端状态再 connect，实际顺序 {calls}")


def test_local_zombie_is_also_disconnected(monkeypatch):
    """本地有个没连上的僵尸 vc 时，本地清 + 服务端清都要做。"""
    zombie = _FakeVC(connected=False)
    calls = _setup(monkeypatch, local_vc=zombie)
    asyncio.run(s._ensure_connected())
    assert zombie.disconnected, "本地僵尸 vc 没被 disconnect"
    assert calls == ["change_voice_state:None", "connect"]


def test_already_connected_is_untouched(monkeypatch):
    """已经连着的时候一个动作都不许做 —— 否则每次心跳都会把自己踢下线。"""
    live = _FakeVC(connected=True)
    calls = _setup(monkeypatch, local_vc=live)
    vc = asyncio.run(s._ensure_connected())
    assert vc is live
    assert calls == [], f"已连接时不该有任何动作，实际 {calls}"
    assert not live.disconnected


def test_change_voice_state_failure_does_not_block_connect(monkeypatch):
    """清状态失败（网关抖动等）不许阻断后面的连接尝试。"""
    calls = _setup(monkeypatch, local_vc=None)
    guild = s._sidecar_bot.guilds[0]

    async def _boom(**_kw):
        calls.append("change_voice_state:raised")
        raise RuntimeError("gateway hiccup")

    guild.change_voice_state = _boom
    vc = asyncio.run(s._ensure_connected())
    assert vc is not None, "清状态失败后仍然应该尝试 connect"
    assert calls == ["change_voice_state:raised", "connect"]
