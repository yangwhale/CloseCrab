"""收音侧要能数出「全零帧」—— 这是「有声但咯楞」唯一的可观测抓手。

背景：2026-09-11 录到的语料听着一卡一卡，逐帧量下来 61 帧里 21 帧是**精确的零**，
而且帧接缝没有任何突跳（最大 1419，比帧内正常起伏还小），说明不是拼接造成的。

精确零只可能来自两处：
  ① 对端真发了 opus 静音帧（人说完一句话时客户端会连发几帧）——正常；
  ② py-cord `voice/receive/reader.py:315`：DAVE 解密抛异常被 `except Exception`
     吞掉，塞一帧 `OPUS_SILENCE` 进去，只打一条 DEBUG——**完全静默的故障**。

真丢包**不会**产生精确零：py-cord 走 FEC/PLC 补偿（`opus.py` 里 FakePacket 那条
分支），补出来的是有能量的近似音。所以「精确零的占比」正好把这三种情况分开。

下面第二个用例是护栏：有声音的帧一个都不许被记成零，否则这个刻度会恒等于 hits，
看上去「全都在丢」，把人引到完全错误的方向去。
"""
import struct

import closecrab.voice.discord_voice_sidecar as s


class _Data:
    def __init__(self, pcm):
        self.pcm = pcm


class _User:
    display_name = "chris"
    id = 1


def _stereo(samples):
    """把一串 mono 采样铺成 48kHz/16bit/stereo 的 bytes（左右同值）。"""
    return b"".join(struct.pack("<hh", v, v) for v in samples)


def _sink():
    return s._get_stt_sink_class()()


def test_silent_frames_are_counted(monkeypatch):
    monkeypatch.setattr(s, "_utterance_feed", lambda _p: None)
    monkeypatch.setattr(s, "_stt_ab_record_pcm", lambda _p: None)
    monkeypatch.setattr(s, "_funasr_ab_feed", lambda _p: None)
    sink = _sink()
    for _ in range(3):
        sink.write(_Data(_stereo([0] * 960)), _User())
    assert sink.hits() == 3
    assert sink.zeros() == 3, f"三帧纯零应该全被数到，实际 {sink.zeros()}"


def test_audible_frames_are_not_counted_as_zero(monkeypatch):
    """**护栏**：只要有一个采样非零就不算零帧。

    没有这条，`zeros()` 会退化成 `hits()` 的别名——每次都报「全丢」，
    而那个结论会把排查引向网络，实际问题在解密。
    """
    monkeypatch.setattr(s, "_utterance_feed", lambda _p: None)
    monkeypatch.setattr(s, "_stt_ab_record_pcm", lambda _p: None)
    monkeypatch.setattr(s, "_funasr_ab_feed", lambda _p: None)
    sink = _sink()
    sink.write(_Data(_stereo([0] * 959 + [1])), _User())   # 只有最后一个采样有值
    sink.write(_Data(_stereo([-3000] * 960)), _User())     # 负值也算有声
    assert sink.hits() == 2
    assert sink.zeros() == 0, f"有声帧被误判成零帧: {sink.zeros()}"


def test_mixed_ratio(monkeypatch):
    """真实场景是混着来的，看的是占比不是绝对值。"""
    monkeypatch.setattr(s, "_utterance_feed", lambda _p: None)
    monkeypatch.setattr(s, "_stt_ab_record_pcm", lambda _p: None)
    monkeypatch.setattr(s, "_funasr_ab_feed", lambda _p: None)
    sink = _sink()
    for i in range(10):
        sink.write(_Data(_stereo([0 if i % 5 == 0 else 500] * 960)), _User())
    assert (sink.hits(), sink.zeros()) == (10, 2)
