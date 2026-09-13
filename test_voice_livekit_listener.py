"""LiveKit 听众判据 + TTS 闸门：常驻音轨在线 ≠ 有人在听。

这两件事以前是混着的，代价各在一头：

- 闸门只认 Discord ⇒ iOS 那头用 `ask_<bot>` 派活出去，本体答完一个字也传不回来。
- 闸门放宽之后拿它当「有人听见了」用 ⇒ `<bot>-speaker` 那条**常驻**音轨会让它
  恒为 True，飞书那条 ogg 兜底被永久静音，而日志里一切正常。

所以每组都带反例：光测「该响的响了」证明不了「不该响的没响」，
而这次真正会咬人的恰恰是后半边。
"""

import sys
import types
import unittest
from unittest import mock


# ── 造一个够用的假 livekit.rtc ────────────────────────────────────────
# 真的 livekit 包在 bot 的 venv 里，测试不该依赖它装没装。只需要
# ParticipantKind 那个枚举 —— 数值抄自 protobuf 定义（AGENT = 4）。

_AGENT = 4
_STANDARD = 0
_SIP = 3


def _fake_rtc_module():
    rtc = types.SimpleNamespace()
    rtc.ParticipantKind = types.SimpleNamespace(
        PARTICIPANT_KIND_STANDARD=_STANDARD,
        PARTICIPANT_KIND_SIP=_SIP,
        PARTICIPANT_KIND_AGENT=_AGENT,
    )
    return rtc


class _P:
    def __init__(self, identity, kind):
        self.identity = identity
        self.kind = kind


class _Room:
    def __init__(self, *participants):
        self.remote_participants = {p.identity: p for p in participants}


def _install_fake_livekit():
    """把假的 livekit.rtc 塞进 sys.modules，函数里那句 import 才拿得到。"""
    pkg = sys.modules.get("livekit")
    if pkg is None:
        pkg = types.ModuleType("livekit")
        sys.modules["livekit"] = pkg
    rtc = _fake_rtc_module()
    sys.modules["livekit.rtc"] = rtc
    return mock.patch.object(pkg, "rtc", rtc, create=True)


class TestHasListener(unittest.TestCase):
    """`has_listener()` 数的是房间里有没有**非 agent** 的参与者。"""

    def setUp(self):
        self._patch = _install_fake_livekit()
        self._patch.start()
        self.addCleanup(self._patch.stop)
        from closecrab.voice import livekit_out
        self.lko = livekit_out

    def _set(self, *, connected, room):
        self.addCleanup(setattr, self.lko, "_connected", self.lko._connected)
        self.addCleanup(setattr, self.lko, "_room", self.lko._room)
        self.lko._connected = connected
        self.lko._room = room

    # -- 反例：这几种都不算有人在听 --------------------------------

    def test_not_connected_is_no_listener(self):
        self._set(connected=False, room=_Room(_P("someone", _STANDARD)))
        self.assertFalse(self.lko.has_listener(),
                         "断开时房间快照再热闹也不算 —— 我们根本发不出去")

    def test_no_room_is_no_listener(self):
        self._set(connected=True, room=None)
        self.assertFalse(self.lko.has_listener())

    def test_empty_room_is_no_listener(self):
        self._set(connected=True, room=_Room())
        self.assertFalse(self.lko.has_listener())

    def test_only_agents_is_no_listener(self):
        """**这条是这个函数存在的理由。**

        空房间里也常驻着两个 agent：我们自己那条只说不听的轨，和语音助手
        worker。它们都不是听众 —— 判成有人听，飞书的 ogg 就没了。
        """
        self._set(connected=True, room=_Room(
            _P("bunny-speaker", _AGENT), _P("gemini-live", _AGENT)))
        self.assertFalse(self.lko.has_listener())

    # -- 正例 -------------------------------------------------------

    def test_human_among_agents(self):
        self._set(connected=True, room=_Room(
            _P("bunny-speaker", _AGENT),
            _P("gemini-live", _AGENT),
            _P("voice_assistant_user_1234", _STANDARD)))
        self.assertTrue(self.lko.has_listener())

    def test_non_standard_non_agent_counts(self):
        """排除 agent 而不是只认 STANDARD —— 以后多一种接入方式不用再改这里。"""
        self._set(connected=True, room=_Room(
            _P("bunny-speaker", _AGENT), _P("sip-caller", _SIP)))
        self.assertTrue(self.lko.has_listener())

    def test_broken_room_object_is_no_listener(self):
        """读房间出错按「没人听」处理：多发一条 ogg，比静音强。"""
        class _Boom:
            @property
            def remote_participants(self):
                raise RuntimeError("SFU 抽风")
        self._set(connected=True, room=_Boom())
        self.assertFalse(self.lko.has_listener())


class TestStreamSpeakGate(unittest.TestCase):
    """`stream_speak_text` 的闸门：Discord **或** LiveKit，不是只认 Discord。"""

    def setUp(self):
        self._patch = _install_fake_livekit()
        self._patch.start()
        self.addCleanup(self._patch.stop)
        from closecrab.voice import discord_voice_sidecar as dvs
        self.dvs = dvs
        # sidecar 那三个前置条件先满足，免得测出来的 False 其实来自别处。
        for name, val in (("_sidecar_loop", mock.Mock()),
                          ("_sidecar_bot", mock.Mock()),
                          ("_speak_queue", mock.Mock())):
            self.addCleanup(setattr, dvs, name, getattr(dvs, name))
            setattr(dvs, name, val)

    def _run(self, *, discord, livekit):
        from closecrab.voice import livekit_out
        with mock.patch.object(self.dvs, "is_voice_connected", return_value=discord), \
             mock.patch.object(livekit_out, "is_connected", return_value=livekit), \
             mock.patch.object(self.dvs.asyncio, "run_coroutine_threadsafe") as sched, \
             mock.patch.object(self.dvs, "_notify_feishu_voice_card"):
            ok = self.dvs.stream_speak_text("念一句", fid="abc")
        return ok, sched.call_count

    def test_no_outlet_is_rejected(self):
        """反例。两个出口都没有还排队，等于凭空生成一段没人听的 TTS。"""
        ok, scheduled = self._run(discord=False, livekit=False)
        self.assertFalse(ok)
        self.assertEqual(scheduled, 0, "被拒的这条不该进队列")

    def test_livekit_only_is_admitted(self):
        """**这条就是这次要修的 bug。** 以前它返回 False，

        于是 iOS 房间里派出去的活，本体答完一个字也回不来。
        """
        ok, scheduled = self._run(discord=False, livekit=True)
        self.assertTrue(ok)
        self.assertEqual(scheduled, 1)

    def test_discord_only_still_works(self):
        """老路径不能因为这次放宽而变窄。"""
        ok, scheduled = self._run(discord=True, livekit=False)
        self.assertTrue(ok)
        self.assertEqual(scheduled, 1)

    def test_empty_text_is_rejected(self):
        with mock.patch.object(self.dvs, "is_voice_connected", return_value=True), \
             mock.patch.object(self.dvs.asyncio, "run_coroutine_threadsafe") as sched:
            self.assertFalse(self.dvs.stream_speak_text("   "))
            self.assertEqual(sched.call_count, 0)


class TestHeardLive(unittest.TestCase):
    """飞书 ogg 兜底的真值表。跳过 ogg 的门槛是「有人真在流式听」。"""

    @staticmethod
    def _f(**kw):
        from closecrab.channels.feishu import _heard_live
        args = dict(streaming=False, zello_took=False,
                    discord_connected=False, livekit_listener=False)
        args.update(kw)
        return _heard_live(**args)

    # -- 这些必须发 ogg（反例，每条都对应一种「回复凭空消失」）----------

    def test_nothing_online(self):
        self.assertFalse(self._f())

    def test_livekit_track_connected_but_room_empty(self):
        """**最关键的一条。**

        `<bot>-speaker` 是常驻音轨，房间空着它也连着，所以 streaming 恒为 True。
        照 streaming 判就等于把飞书语音永久关掉 —— 而用户多数时候没开那个 app。
        """
        self.assertFalse(self._f(streaming=True, livekit_listener=False))

    def test_listener_but_nothing_queued(self):
        """房间里有人，但这个 bot 压根没把音频交出去（没跑 Discord sidecar）。

        只看「有没有人」会把 ogg 也跳掉，于是两头都不出声。
        """
        self.assertFalse(self._f(streaming=False, livekit_listener=True))
        self.assertFalse(self._f(streaming=False, discord_connected=True))

    # -- 这些跳过 ogg ------------------------------------------------

    def test_discord_listening(self):
        self.assertTrue(self._f(streaming=True, discord_connected=True))

    def test_livekit_has_human(self):
        self.assertTrue(self._f(streaming=True, livekit_listener=True))

    def test_zello_owns_its_own_answer(self):
        """Zello 不经统一播放器，所以它自己认账、不看 streaming。"""
        self.assertTrue(self._f(streaming=False, zello_took=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
