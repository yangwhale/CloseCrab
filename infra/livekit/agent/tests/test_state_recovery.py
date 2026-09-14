"""`_arm_state_recovery`：被硬取消的一轮结束后，把 `lk.agent.state` 拨回 listening。

跑法（**必须用 agent 那个 venv**，agent.py 顶上要 import livekit）：

    cd ~/lk-gemini-agent && .venv/bin/python -m pytest \
        ~/CloseCrab/infra/livekit/agent/tests/test_state_recovery.py -v

  或者不装 pytest：`.venv/bin/python <这个文件>`。

这里真正值钱的是**反例**。「卡住了能拨回来」很容易测过，而这段代码会咬人的
三种方式全在另一边：抢了下一轮的状态、正常收尾时多播一次、以及最隐蔽的
那条 —— 同步判断必然看见「还有人在说话」于是一次都不生效，日志里干干净净。
"""

import asyncio
import os
import pathlib
import sys
import unittest

# GEMINI_API_KEY 只在 _build_session() 里用，import 不需要；但别让缺它的机器
# 在别处炸出来掩盖真正的失败。
os.environ.setdefault("GEMINI_API_KEY", "test-only-not-a-real-key")
sys.path.insert(0, str(pathlib.Path.home() / "lk-gemini-agent"))

import agent  # noqa: E402


class _Handle:
    """够用的 SpeechHandle 替身：只需要 add_done_callback。"""

    def __init__(self):
        self._cbs = []

    def add_done_callback(self, cb):
        self._cbs.append(cb)

    def fire(self):
        """模拟 `_mark_done()` —— **同步**把回调调起来。"""
        for cb in self._cbs:
            cb(self)


class _BareSession:
    """**没有** `_update_agent_state` —— 模拟上游把这个私有方法改名或删掉。"""

    def __init__(self, state="thinking", current_speech=None):
        self.agent_state = state
        self.current_speech = current_speech
        self._handlers = {}
        self.calls = []

    def on(self, event, cb):
        self._handlers.setdefault(event, []).append(cb)

    def emit_speech_created(self, handle):
        for cb in self._handlers.get("speech_created", []):
            cb(type("Ev", (), {"speech_handle": handle})())


class _Session(_BareSession):
    def _update_agent_state(self, state):
        self.calls.append(state)
        self.agent_state = state


async def _drain():
    """让 call_soon 排的那个回调真的跑一遍。"""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class TestStateRecovery(unittest.IsolatedAsyncioTestCase):

    # -- 正例：这就是 2026-09-14 那次卡死 -----------------------------

    async def test_hard_cancelled_speech_restores_listening(self):
        s = _Session(state="thinking")
        agent._arm_state_recovery(s)
        h = _Handle()
        s.emit_speech_created(h)
        h.fire()                      # 死线到点，task 全 cancel，speech 被判 done
        await _drain()
        self.assertEqual(s.calls, ["listening"])

    async def test_speaking_also_restored(self):
        """卡在 speaking 一样要拨 —— 死法不止 thinking 一种。"""
        s = _Session(state="speaking")
        agent._arm_state_recovery(s)
        h = _Handle()
        s.emit_speech_created(h)
        h.fire()
        await _drain()
        self.assertEqual(s.calls, ["listening"])

    async def test_check_is_deferred_not_synchronous(self):
        """**最容易写错的一条，也是唯一会让整段代码静默失效的一条。**

        done 回调是 `_mark_done()` 同步调起来的，那一刻 activity 还没清
        `_current_speech`。当场判断就必然看见「还有人在说话」直接 return ——
        一次都不生效，日志里还一行异常都没有。

        所以这里故意在 fire() **之后**才清掉 current_speech：只有把判断推到
        下一个 loop turn 的实现才能过。
        """
        h = _Handle()
        s = _Session(state="thinking", current_speech=h)
        agent._arm_state_recovery(s)
        s.emit_speech_created(h)
        h.fire()
        s.current_speech = None       # activity 在同一轮稍后才清
        await _drain()
        self.assertEqual(s.calls, ["listening"])

    # -- 反例：这几种都不许动状态 -------------------------------------

    async def test_next_speech_already_queued_is_left_alone(self):
        """正常收尾时下一轮往往已经排上了。这时候拨成 listening 会把真实的
        speaking 盖掉，前端的状态指示开始乱跳。"""
        s = _Session(state="speaking", current_speech=_Handle())
        agent._arm_state_recovery(s)
        h = _Handle()
        s.emit_speech_created(h)
        h.fire()
        await _drain()
        self.assertEqual(s.calls, [], "下一轮已经接上了，不该抢它的状态")

    async def test_already_listening_is_not_republished(self):
        """上游自己拨回来了就别再播一次 —— 多余的属性更新前端也要处理。"""
        s = _Session(state="listening")
        agent._arm_state_recovery(s)
        h = _Handle()
        s.emit_speech_created(h)
        h.fire()
        await _drain()
        self.assertEqual(s.calls, [])

    async def test_initializing_is_not_touched(self):
        """会话还没起来，拨成 listening 等于对前端谎报已就绪。"""
        s = _Session(state="initializing")
        agent._arm_state_recovery(s)
        h = _Handle()
        s.emit_speech_created(h)
        h.fire()
        await _drain()
        self.assertEqual(s.calls, [])

    async def test_missing_private_api_is_loud_and_harmless(self):
        """`_update_agent_state` 是上游私有方法。它哪天改名了，要**当场喊出来**，

        而不是安安静静什么都不做 —— 否则下次卡死又得从头查一遍，
        才发现自愈压根没挂上。
        """
        s = _BareSession(state="thinking")
        with self.assertLogs(agent.logger, level="ERROR") as log:
            agent._arm_state_recovery(s)
        self.assertIn("_update_agent_state", log.output[0])
        self.assertEqual(s._handlers, {}, "挂不上就别注册回调，留个假象更糟")


if __name__ == "__main__":
    unittest.main(verbosity=2)
