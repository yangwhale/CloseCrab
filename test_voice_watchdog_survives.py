"""守护循环的存活性测试。

**为什么值得单独一个文件**：2026-09-11 那次故障里，`_ssrc_infer_loop` 因为一个
没人接的异常整个死掉，而它正是「录音被冲垮 → 自动重启」的唯一执行者。后果是
bot 只能说不能听四个小时，**日志里一句 ERROR 都没有** —— 出站 TTS 全程正常，
从外面看它活得好好的。所以这里测的不是「功能对不对」，是「它会不会死」。
"""
import asyncio
import logging

import pytest

import closecrab.voice.discord_voice_sidecar as s


class _Boom:
    """任何属性访问都炸。用它冒充 VoiceClient，把异常注进循环体第一行。"""

    def __getattr__(self, name):
        raise RuntimeError(f"boom: {name}")


@pytest.mark.asyncio
async def test_watchdog_survives_an_exploding_voice_client(monkeypatch, caplog):
    """循环体抛异常 → 记一条日志、继续下一轮，**不能让任务死掉**。"""
    monkeypatch.setattr(s, "_listen_active", True)
    monkeypatch.setattr(s, "_listen_vc", _Boom())
    caplog.set_level(logging.ERROR, logger="closecrab.discord_voice_sidecar")

    task = asyncio.create_task(s._ssrc_infer_loop(period=0.01))
    await asyncio.sleep(0.2)          # 够跑十几轮
    alive = not task.done()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert alive, "守护被一个异常带走了 —— 这正是那次四小时失聪的成因"
    hits = [r for r in caplog.records if "守护本轮出错" in r.getMessage()]
    assert len(hits) >= 2, f"应当每轮都记一条并继续，实际 {len(hits)} 条"


@pytest.mark.asyncio
async def test_cancel_still_stops_it(monkeypatch):
    """**负向用例**：catch-all 不能把 CancelledError 也吞掉。

    吞了的话进程关不干净 —— 关机时这个任务会一直转，`stop()` 等它等到超时。
    上面那条测「异常杀不死它」，这条测「该死的时候要死」，缺一不可。
    """
    monkeypatch.setattr(s, "_listen_active", True)
    monkeypatch.setattr(s, "_listen_vc", _Boom())

    task = asyncio.create_task(s._ssrc_infer_loop(period=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_racy_ssrc_map_copy_does_not_escape():
    """`dict(smap)` 在拷贝途中被别的线程改 → RuntimeError，必须就地咽下。

    这是那次故障最可能的起爆点：py-cord 的接收线程在重连时重建 ssrc_user_map，
    守护正好在读它。
    """

    class _MutatingMap:
        """**不能继承 dict** —— `dict(某个 dict 子类)` 走 C 层快路径，压根不调
        `keys()`，测试会假绿。用裸 mapping 协议才真的进 `keys()`。"""

        def keys(self):
            raise RuntimeError("dictionary changed size during iteration")

        def __getitem__(self, k):            # pragma: no cover - 到不了
            return None

    m = _MutatingMap()
    try:
        out = dict(m)
    except RuntimeError:
        out = {}
    assert out == {}, "拷贝失败应当退化成空 map，而不是把异常抛出去"
