"""打断之后 `lk.agent.state` 怎么回到 listening —— 两道锁，一前一后。

- `_arm_interrupt_cutoff`：**打断那一瞬间**就拨（挂在 `_interrupt_fut` 上）。
- `_arm_state_recovery`：一轮说话结束后兜底，管前者没挂上 / 别的路卡住的情况。

两个必须分开测。只测后者会让「迟到 5 秒」这个 bug 一路全绿 ——
它当初就是这么活下来的：状态确实拨回来了，只是晚了整整一个 `INTERRUPTION_TIMEOUT`。

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


class _IHandle:
    """带 `_interrupt_fut` 的 SpeechHandle 替身。"""

    def __init__(self):
        self._interrupt_fut = asyncio.get_running_loop().create_future()

    def interrupt(self):
        """模拟 `_cancel()`：把 `_interrupt_fut` 置上（**不**等那 5 秒死线）。"""
        if not self._interrupt_fut.done():
            self._interrupt_fut.set_result(None)


class TestInterruptCutoff(unittest.IsolatedAsyncioTestCase):
    """打断的那一瞬间就把状态拨回来，不等 `INTERRUPTION_TIMEOUT`。

    **注意这里一条工具相关的测试都没有，是对的。** 2026-09-14 定的：被打断时
    在跑的工具让它自己跑完（by design，理由见 `_arm_interrupt_cutoff` 的
    docstring）。状态跟工具已经彻底解耦 —— 这一组要守住的就是这个解耦。

    反例比正例值钱：抢了下一轮的状态、正常情况下多播一次、以及最隐蔽的那条 ——
    上游改了私有字段名之后它一声不吭什么都不做，现象跟「补丁没部署」一模一样。
    """

    # -- 正例 -------------------------------------------------------

    async def test_state_restored_without_waiting_for_the_deadline(self):
        """**这就是要修的那条。** 实测原来要等满 5.0 秒。

        这个测试里连一次 sleep 都没有，而且**没有任何工具**在跑 ——
        它证明的正是「状态不再需要等这一轮收尾」。
        """
        s = _Session(state="thinking")
        h = _IHandle()
        s.current_speech = h
        agent._arm_interrupt_cutoff(s)
        s.emit_speech_created(h)
        h.interrupt()
        await _drain()
        self.assertEqual(s.calls, ["listening"])

    async def test_speaking_is_restored_too(self):
        """插话打断的多数是正在播的那一句，卡在 speaking 一样要拨。"""
        s = _Session(state="speaking")
        h = _IHandle()
        s.current_speech = h
        agent._arm_interrupt_cutoff(s)
        s.emit_speech_created(h)
        h.interrupt()
        await _drain()
        self.assertEqual(s.calls, ["listening"])

    # -- 反例 -------------------------------------------------------

    async def test_next_speech_already_took_over(self):
        """下一轮顶上来了，那时候的 thinking 是真的，拨掉会让前端乱跳。"""
        s = _Session(state="thinking")
        h = _IHandle()
        s.current_speech = _IHandle()      # 已经换人了
        agent._arm_interrupt_cutoff(s)
        s.emit_speech_created(h)
        h.interrupt()
        await _drain()
        self.assertEqual(s.calls, [])

    async def test_already_listening_is_not_republished(self):
        s = _Session(state="listening")
        h = _IHandle()
        s.current_speech = h
        agent._arm_interrupt_cutoff(s)
        s.emit_speech_created(h)
        h.interrupt()
        await _drain()
        self.assertEqual(s.calls, [])

    async def test_initializing_is_not_touched(self):
        """会话还没起来，拨成 listening 等于对前端谎报已就绪。"""
        s = _Session(state="initializing")
        h = _IHandle()
        s.current_speech = h
        agent._arm_interrupt_cutoff(s)
        s.emit_speech_created(h)
        h.interrupt()
        await _drain()
        self.assertEqual(s.calls, [])

    async def test_no_interrupt_means_no_state_change(self):
        """**光开一轮不算打断。** 不置 `_interrupt_fut` 就一个字都不许动 ——

        否则每轮说话一开始就把自己拨成 listening，前端永远看不到 thinking。
        """
        s = _Session(state="thinking")
        h = _IHandle()
        s.current_speech = h
        agent._arm_interrupt_cutoff(s)
        s.emit_speech_created(h)
        await _drain()
        self.assertEqual(s.calls, [])

    async def test_missing_interrupt_fut_is_loud(self):
        """`_interrupt_fut` 是上游私有字段。它改名了要当场喊 ——

        否则现象是「又卡了 5 秒」，而日志里一行异常都没有，
        跟这个补丁根本没部署长得一模一样。
        """
        s = _Session(state="thinking")
        agent._arm_interrupt_cutoff(s)
        with self.assertLogs(agent.logger, level="ERROR") as log:
            s.emit_speech_created(_Handle())   # 这个替身没有 _interrupt_fut
        self.assertIn("_interrupt_fut", log.output[0])

    async def test_missing_private_api_is_loud_and_harmless(self):
        """`_update_agent_state` 没了：喊出来，并且**不要**注册回调，
        免得留一个「挂上了」的假象。"""
        s = _BareSession(state="thinking")
        with self.assertLogs(agent.logger, level="ERROR") as log:
            agent._arm_interrupt_cutoff(s)
        self.assertIn("_update_agent_state", log.output[0])
        self.assertEqual(s._handlers, {})


class _Err:
    """`ErrorEvent` 替身：只用到 `.error.recoverable` 和 `.created_at`。"""

    def __init__(self, at, recoverable=True):
        self.error = type("E", (), {"recoverable": recoverable})()
        self.created_at = at


class _RTSession:
    def __init__(self, retries=0):
        self._num_retries = retries


class _ConnSession(_BareSession):
    """带 `_activity.realtime_llm_session` 的 AgentSession 替身。"""

    def __init__(self, rt=None, has_activity=True):
        super().__init__()
        if has_activity:
            self._activity = type("A", (), {"realtime_llm_session": rt})()

    def _update_agent_state(self, state):
        pass

    def emit_error(self, at, recoverable=True):
        for cb in self._handlers.get("error", []):
            cb(_Err(at, recoverable))


class TestReconnectBudget(unittest.TestCase):
    """重连预算：例行断线不扣账，真故障照常见底。

    这一组的反例是全部价值所在。「清零了」很容易测过 —— 无脑每次都清零也能过，
    而那样等于把 `max_retry` 变成无限，Gemini 真挂了的时候 agent 会永远假装活着。
    真正要守的是**两种断线分得开**。
    """

    def _armed(self, retries=0, **kw):
        rt = _RTSession(retries)
        s = _ConnSession(rt, **kw)
        agent._arm_reconnect_budget(s)
        return s, rt

    # -- 正例：这就是 2026-09-14 09:59 那次聋掉 -----------------------

    def test_routine_170s_drops_never_exhaust_the_budget(self):
        """**要修的就是这条。** 原来第 9 次例行断线就宣判死亡。

        真实顺序是 `检查上限 → emit → sleep → +=1`，所以这里先 +=1 再 emit。
        """
        s, rt = self._armed()
        t = 1000.0
        for i in range(50):          # 50 × 151 秒 ≈ 两小时闲置
            rt._num_retries += 1     # plugin 在上一轮末尾加的那一下
            t += 151.0
            s.emit_error(t)
            # 第一次没有「上一次」可比，账不动；之后每次都该清回 0。
            self.assertEqual(rt._num_retries, 1 if i == 0 else 0)

    def test_exactly_at_the_threshold_counts_as_healthy(self):
        """边界取「够 30 秒就算服役过」，别让临界值掉进真故障那一档。"""
        s, rt = self._armed(retries=3)
        s.emit_error(1000.0)
        rt._num_retries = 3
        s.emit_error(1030.0)
        self.assertEqual(rt._num_retries, 0)

    # -- 反例 -------------------------------------------------------

    def test_instant_failures_still_exhaust_the_budget(self):
        """**最重要的反例。** 连上就掉是真出事了，预算必须照常见底 ——

        否则 Gemini 整个挂掉的时候，这个 agent 会永远重试、永远不报死，
        而用户看到的还是「说话没反应」，只是再也没有那行 unrecoverable 能查。
        """
        s, rt = self._armed()
        t = 1000.0
        for _ in range(8):
            rt._num_retries += 1
            t += 0.5                 # retry_interval，两个数量级之外
            s.emit_error(t)
        self.assertEqual(rt._num_retries, 8, "真故障时计数不该被抹掉")

    def test_unrecoverable_error_is_ignored(self):
        """recoverable=False 是已经宣判了，这里插手只会掩盖死因。"""
        s, rt = self._armed(retries=8)
        s.emit_error(1000.0, recoverable=False)
        self.assertEqual(rt._num_retries, 8)

    def test_first_error_judges_nothing(self):
        """开机后第一次报错没有「上一次」可比 —— 不许炸，也**不许凭空清零**。

        无脑清零会把 8 次预算悄悄放宽成 9 次。一格不多，但它是编出来的。
        """
        s, rt = self._armed(retries=2)
        s.emit_error(1000.0)
        self.assertEqual(rt._num_retries, 2)

    def test_missing_rt_session_is_loud_but_only_once(self):
        """拿不到就喊，但**别每 170 秒喊一遍** —— 刷屏等于没喊。"""
        s, _ = self._armed()
        s._activity.realtime_llm_session = None
        with self.assertLogs(agent.logger, level="ERROR") as log:
            s.emit_error(1000.0)
            s.emit_error(1151.0)
        self.assertEqual(len(log.output), 1)
        self.assertIn("_num_retries", log.output[0])

    def test_missing_activity_is_loud_and_harmless(self):
        s = _ConnSession(has_activity=False)
        with self.assertLogs(agent.logger, level="ERROR") as log:
            agent._arm_reconnect_budget(s)
        self.assertIn("_activity", log.output[0])
        self.assertEqual(s._handlers, {}, "挂不上就别注册回调，留个假象更糟")


class _FakePresence:
    def __init__(self, empty_after):
        self._empty_after = empty_after

    async def wait_until_empty(self, grace):
        await asyncio.sleep(self._empty_after)


class TestServeUntilEmptyOrClosed(unittest.IsolatedAsyncioTestCase):
    """房间空了 **或** 会话死了，谁先到算谁。"""

    async def test_close_wins_without_waiting_for_the_room_to_empty(self):
        """**这是聋掉的后半段。** 原来只等房间空 —— 人还在，就永远等不到。"""
        closed = asyncio.Event()
        closed.set()
        pres = _FakePresence(empty_after=30)
        await asyncio.wait_for(
            agent._serve_until_empty_or_closed(pres, closed, 60.0), timeout=1.0
        )

    async def test_empty_wins_when_the_session_stays_alive(self):
        """老路径不能因为这次加料而变窄。"""
        closed = asyncio.Event()
        pres = _FakePresence(empty_after=0)
        await asyncio.wait_for(
            agent._serve_until_empty_or_closed(pres, closed, 60.0), timeout=1.0
        )

    async def test_the_loser_is_cancelled_not_leaked(self):
        """输的那个必须收干净。会话每死一次就漏一个协程，日志里看不出来，

        直到「Task was destroyed but it is pending」开始刷屏。
        """
        before = len(asyncio.all_tasks())
        closed = asyncio.Event()
        closed.set()
        # timeout 不是装饰 —— 少了它，`closed` 那一路被删掉时这个 case 会安静地
        # 等满一小时，变异测试跑不完只会看见「超时」，不会看见「被杀掉」。
        await asyncio.wait_for(
            agent._serve_until_empty_or_closed(_FakePresence(3600), closed, 60.0),
            timeout=1.0,
        )
        await asyncio.sleep(0)
        self.assertEqual(len(asyncio.all_tasks()), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
