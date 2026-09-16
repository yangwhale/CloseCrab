#!/usr/bin/env python3
"""TTS 两条优先级规则的回归 —— 2026-09-16 现场提的两件事。

    ① 工具调用那种「填空」话是 low priority：**正式输出在播 / 在排队时，
       它不该出声**（老实现只是让它排在后面，回复一播完照样冒出来）。
    ② 正式输出之间**严格排队**：上一条没播完，下一条不许顶掉它
       （老 `wait_playout` 只有一个总时限，实测抓到 181 秒的回复在 191 秒
        被放行，正播着就被下一条盖掉）。

⛔ 每条规则都配一个**反例**。只测「该丢的丢了」是不够的 ——
   一个永远返回 False 的闸门也能全绿，而它会把所有提示音都吞掉。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (("  —— " + detail) if detail and not ok else ""))


# ── 规则①：hint 的优先级 ───────────────────────────────────────────────
def _fresh_queue(dvs):
    dvs._speak_queue = asyncio.Queue()
    dvs._reply_in_flight = False
    return dvs._speak_queue


def t_hint_priority():
    from closecrab.voice import discord_voice_sidecar as dvs

    async def run():
        q = _fresh_queue(dvs)

        # 反例先跑：什么都没在播的时候，hint **必须**念得出来。
        await dvs._enqueue_speak("填空一", "")
        check("反例：没有正式输出时，hint 照常入队", q.qsize() == 1,
              f"qsize={q.qsize()}（闸门写死成 False 的话这里是 0）")

        # ① 正式输出正在播 → hint 直接丢
        _fresh_queue(dvs)
        dvs._reply_in_flight = True
        await dvs._enqueue_speak("填空二", "")
        check("正式输出正在播时，hint 被丢掉", dvs._speak_queue.qsize() == 0,
              f"qsize={dvs._speak_queue.qsize()}")

        # ② 正式输出还在队列里排着 → hint 也丢
        q = _fresh_queue(dvs)
        await dvs._enqueue_speak("正经回复", "fid-1")
        await dvs._enqueue_speak("填空三", "")
        check("正式输出在队列里排着时，hint 被丢掉", q.qsize() == 1,
              f"qsize={q.qsize()}，应只剩那条 reply")

        # ③ reply 永远不被丢 —— 哪怕另一条 reply 正在播
        q = _fresh_queue(dvs)
        dvs._reply_in_flight = True
        await dvs._enqueue_speak("第二条正经回复", "fid-2")
        check("reply 永不被丢（即使另一条正在播）", q.qsize() == 1,
              f"qsize={q.qsize()}")

        # ④ reply 入队时，队列里排着的旧 hint 要被清掉
        q = _fresh_queue(dvs)
        await dvs._enqueue_speak("填空四", "")
        await dvs._enqueue_speak("正经回复", "fid-3")
        items = list(q._queue)
        check("reply 入队会清掉排着的旧 hint",
              len(items) == 1 and items[0].is_reply,
              f"剩下 {[(i.text, i.is_reply) for i in items]}")

    asyncio.run(run())
    dvs = sys.modules["closecrab.voice.discord_voice_sidecar"]
    dvs._speak_queue = None
    dvs._reply_in_flight = False


# ── 规则②：wait_playout 不许提前放行 ───────────────────────────────────
class FakeP:
    """可编程的假播放器：位置走不走、暂停没暂停、还忙不忙，全可控。"""

    def __init__(self, busy_ticks=0, advancing=True, paused=False):
        self.n = 0
        self.busy_ticks = busy_ticks      # 还要「忙」多少次问询
        self.advancing = advancing        # 位置到底动不动
        self.paused = paused

    def is_busy(self):
        self.n += 1
        return self.n <= self.busy_ticks

    def is_paused(self):
        return self.paused

    def progress(self):
        return ((self.n * 0.1) if self.advancing else 1.0, 99.0, True, "f")


def t_wait_playout():
    from closecrab.voice import playback

    saved_player = playback._player
    saved_stall = playback._STALL_LIMIT
    try:
        # ① 位置一直在往前走 —— 就算远超 timeout 也**必须**接着等。
        #    这就是现场那句「上一个还没说完就被打断」的回归。
        playback._player = FakeP(busy_ticks=12, advancing=True)
        t0 = time.monotonic()
        asyncio.run(playback.wait_playout(0.3))
        dt = time.monotonic() - t0
        check("位置还在走 → 超过 timeout 也继续等（不打断正播的回复）",
              dt > 0.9, f"只等了 {dt:.2f}s，timeout 是 0.3s —— 老实现就是这里放行的")

        # ② 反例：位置卡住不动 → 必须放行，不能把队列焊死。
        playback._STALL_LIMIT = 0.3
        playback._player = FakeP(busy_ticks=10 ** 9, advancing=False)
        t0 = time.monotonic()
        asyncio.run(playback.wait_playout(0.2))
        dt = time.monotonic() - t0
        check("反例：位置卡住不动 → 放行，不焊死队列", dt < 3.0,
              f"等了 {dt:.2f}s，卡死判据是 0.3s")

        # ③ 用户按了暂停 → 立刻放行（他自己按的）
        playback._STALL_LIMIT = saved_stall
        playback._player = FakeP(busy_ticks=10 ** 9, advancing=True, paused=True)
        t0 = time.monotonic()
        asyncio.run(playback.wait_playout(5.0))
        dt = time.monotonic() - t0
        check("用户暂停 → 立刻放行", dt < 1.0, f"等了 {dt:.2f}s")
    finally:
        playback._player = saved_player
        playback._STALL_LIMIT = saved_stall


def main():
    for fn in (t_hint_priority, t_wait_playout):
        print(f"\n── {fn.__name__} ──")
        fn()
    bad = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} 通过")
    if bad:
        for n in bad:
            print("  失败:", n)
        sys.exit(1)


if __name__ == "__main__":
    main()
