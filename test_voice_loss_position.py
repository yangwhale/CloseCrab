"""丢包位置分段统计的单测。

**为什么要有这段代码**：2026-09-12 Chris 报了一个很具体的现象 ——
「句子说长了，前半句还行，后半句开始着急、草草发完」，并猜是 5G 上行
发着发着就堵了。这个猜测有个能被证伪的预言：丢包应该**往后半句堆**。

而总丢包率恰好把这个信号平均掉了：19% 这个数不管丢在句首还是句尾都一样。
所以要按位置分四段记。这个单测的作用是保证「分段数字能反映真实趋势」——
它一旦算反，我们会拿着一个方向相反的图去否掉一个正确的猜测。

**负例是重点**（比正例更重要）：
  - 短句（帧数不够）不能出数字。四段里每段十来帧，一个缺口就能让某段
    看起来 50%，纯噪音。
  - 上一句的位置记录必须清干净，否则跨句累积 → 越到后面越像「后半句丢」，
    正好伪造出我们要找的那个结论。
"""
from __future__ import annotations

import re

import pytest

import closecrab.voice.discord_voice_sidecar as sc


@pytest.fixture(autouse=True)
def _clean():
    sc._utt_opus.clear()
    sc._utt_gaps.clear()
    sc._utt_loss_pos.clear()
    sc._utt_opus_bytes = 0
    sc._utt_lost = 0
    yield
    sc._utt_opus.clear()
    sc._utt_gaps.clear()
    sc._utt_loss_pos.clear()
    sc._utt_opus_bytes = 0
    sc._utt_lost = 0


def feed(recv_then_gap: list[tuple[int, int]]) -> None:
    """按 [(收 n 帧, 然后丢 g 帧), ...] 喂一句话。g=0 表示只收不丢。"""
    for n, g in recv_then_gap:
        for _ in range(n):
            sc._utt_opus[15100] += 1
            sc._utt_opus_bytes += 120
        if g:
            sc._utt_loss_note(g)


def feed_quarters(losses: list[int], per: int = 25) -> None:
    """每段收 `per` 帧，丢包落在**段中间**，四段各丢 losses[q] 帧。

    为什么要刻意落在段中间：分段是按「已收帧数」切的，丢包正好发生在
    第 25 帧那一刻时，它记的位置是 25 —— 落进**第二段**。这不是 bug
    （损失确实发生在两段交界后），但拿它构造测试数据会让第一段恒为 0，
    把趋势测歪。测趋势就把样本放段中央，边界行为另有一条专门的用例。
    """
    for g in losses:
        half = per // 2
        feed([(half, g), (per - half, 0)])


def seg(note: str) -> list[int]:
    """从账单文本里把四段百分比抠出来。"""
    m = re.search(r"分段(\S+)", note)
    assert m, note
    return [int(x) for x in m.group(1).replace("%", "").split("/")]


# ── 正例：趋势必须原样保留 ────────────────────────────────────────────

def test_丢包堆在后半句_分段必须递增():
    """Chris 猜的那个形状：越说到后面丢得越多。"""
    feed_quarters([0, 2, 5, 10])
    s = seg(sc._utt_opus_take())
    assert s == sorted(s), s
    assert s[0] == 0 and s[3] > s[1], s


def test_丢包堆在前半句_分段必须递减():
    """反过来也得测 —— 只测递增的话，一个恒递增的 bug 会全绿。"""
    feed_quarters([10, 5, 2, 0])
    s = seg(sc._utt_opus_take())
    assert s == sorted(s, reverse=True), s
    assert s[3] == 0 and s[0] > s[2], s


def test_均匀丢包_四段应当接近():
    feed_quarters([4, 4, 4, 4])
    s = seg(sc._utt_opus_take())
    assert max(s) - min(s) <= 1, s


def test_分母是本段本该有的帧数_不是整句平均():
    """丢得狠的那段分母要跟着涨，否则趋势会被压平。

    构造：前 90 帧一个不丢，第四段中间丢 30。第四段收到 25 帧、丢 30，
    应是 30/(25+30) = 55%。若错用整句丢包率的分母（100+30=130），
    会算成 23% —— 一个把「这段丢惨了」说成「还好」的数。
    """
    feed([(90, 0), (5, 30), (5, 0)])
    s = seg(sc._utt_opus_take())
    assert s == [0, 0, 0, 55], s


def test_丢在最末尾归入第四段_不越界():
    """句子最后一帧之后还丢了一串 —— 位置等于总帧数，下标会算出 4。

    `min(3, ...)` 就是为这个写的。没有它这里直接 IndexError，
    而这条路径跑在每一句话的收尾上。
    """
    feed([(100, 0)])
    sc._utt_loss_note(8)               # at == n == 100
    s = seg(sc._utt_opus_take())
    assert s == [0, 0, 0, 24], s       # 8/(25+8) = 24.2%


# ── 负例：不该出数字的时候一个字都不能出 ──────────────────────────────

def test_短句不出分段():
    feed([(10, 2), (10, 3)])          # 20 帧 < 40
    assert "分段" not in sc._utt_opus_take()


def test_一帧不丢不出分段():
    feed([(100, 0)])
    note = sc._utt_opus_take()
    assert "分段" not in note and "丢0" in note


def test_一个包都没有不炸():
    assert sc._utt_opus_take() == "无包"


# ── 负例中的负例：跨句必须清零 ────────────────────────────────────────

def test_上一句的丢包不能漏进下一句():
    feed([(50, 20), (50, 0)])
    first = sc._utt_opus_take()
    assert "分段" in first

    feed([(100, 0)])                   # 第二句干干净净
    second = sc._utt_opus_take()
    assert "分段" not in second, f"上一句的位置记录漏过来了: {second}"
    assert "丢0" in second, second
    assert sc._utt_loss_pos == []


def test_缺口分布与总丢包也一并清零():
    feed([(60, 1), (60, 3)])
    sc._utt_opus_take()
    assert sc._utt_lost == 0
    assert not sc._utt_gaps
    assert not sc._utt_loss_pos
