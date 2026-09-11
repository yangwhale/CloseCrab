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


# ─── 解密账本 ───────────────────────────────────────────────────────────────

class _Stats:
    def __init__(self, ok, bad, pt):
        self.successes, self.failures, self.passthroughs = ok, bad, pt
        self.attempts, self.duration = ok + bad, 0


class _Dave:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def get_decryption_stats(self, uid, media_type=None):
        self.calls.append(uid)
        return self.table[uid]


def test_ledger_reports_failures():
    d = _Dave({7: _Stats(300, 21, 0)})
    assert s._decryption_ledger(d, [7]) == "7:成功300/失败21/透传0"


def test_ledger_needs_user_id():
    """**护栏**：davey 的账本是 per-user 的，不传 uid 会 TypeError。

    第一版就是这么写错的 —— 日志里只留下一句「<读取失败>」，等于什么都没测到，
    而它看上去像是「这个 API 用不了」，会让人放弃这条最有用的线索。
    """
    d = _Dave({7: _Stats(1, 0, 0)})
    s._decryption_ledger(d, [7])
    assert d.calls == [7], "必须带着 user_id 调用"


def test_ledger_survives_a_throwing_session():
    """账本读不出来不许拖垮守护线程 —— 它只是诊断，不是主路径。"""
    class _Boom:
        def get_decryption_stats(self, uid, media_type=None):
            raise RuntimeError("no group")
    line = s._decryption_ledger(_Boom(), [7])
    assert "RuntimeError" in line
    # **异常正文必须一起打**：只有类型名的话，`<ValueError>` 分不清
    # 「这人不在 MLS 组里」和「参数传错了」，而这两件事下一步动作相反。
    assert "no group" in line, f"异常正文丢了: {line}"


def test_ledger_without_dave():
    assert s._decryption_ledger(None, [7]) == "-"


# ─── 解密失败原因 ────────────────────────────────────────────────────────────
#
# davey 的账本只说「失败了多少次」，说不出为什么。而「密钥轮换没跟上」和
# 「包本身损坏」这两种原因，下一步动作完全不同 —— 所以原因必须单独记。

def _reset_reasons():
    with s._dave_fail_lock:
        s._dave_fail_reasons.clear()
        s._dave_shape_stats.clear()


def _fail(exc):
    s._record_dave_result(False, False, b"\x00\x00", exc)


def test_no_failures_prints_nothing_noisy():
    """**护栏**：一次没失败时必须是 '-'，不能打一串空壳。

    诊断行每 3 秒一条，长期挂着。这里多几个字符，日志里就多几万行噪音。
    """
    _reset_reasons()
    assert s._dave_fail_summary() == "-"


def test_reasons_are_bucketed_by_message():
    """按原因分桶，不是只留最后一条。

    失败常常是混合的：偶发一条 CorruptPacket 混在大量 KeyRatchet 里。
    只留最后一条会把偶发的那个当成主因，正好指错方向。
    """
    _reset_reasons()
    for _ in range(5):
        _fail(ValueError("no key ratchet for generation 3"))
    _fail(RuntimeError("bad tag"))
    line = s._dave_fail_summary()
    assert "ValueError: no key ratchet for generation 3×5" in line
    assert "RuntimeError: bad tag×1" in line
    # 多的排前面 —— 主因要一眼看到
    assert line.index("ValueError") < line.index("RuntimeError")


def test_summary_is_capped():
    """原因种类可能很多，诊断行不许无限长。"""
    _reset_reasons()
    for i in range(10):
        _fail(ValueError(f"reason {i}"))
    assert s._dave_fail_summary(top=3).count("×") == 3


# ─── 包形状（成败 × 扩展头 × 帧尾） ──────────────────────────────────────────
#
# 失败计数单独看没有信息量：「165 帧失败」既可能是全体失败也可能是一半失败。
# 只有把成功帧和失败帧的形状并排放着，才判得出差异到底在哪一维。

def test_shape_separates_success_from_failure():
    """成功和失败必须分桶，不能混成一个总数。"""
    _reset_reasons()
    s._record_dave_result(True, False, bytes.fromhex("dead" "fafa"))
    s._record_dave_result(False, True, bytes.fromhex("dead" "0001"), ValueError("x"))
    line = s._dave_shape_summary()
    assert "无扩展头/尾fafa/成功×1" in line, line
    assert "有扩展头/尾0001/失败×1" in line, line


def test_shape_summary_keeps_both_sides_visible():
    """**护栏**：失败占绝对多数时，成功那栏不许被 top-N 挤掉。

    这个测量的全部意义就是两栏对比 —— 只剩失败栏的话，等于退回到那个
    没有信息量的失败计数，还白白让人以为自己在看对比。
    """
    _reset_reasons()
    for i in range(5):                       # 5 种不同形状的失败，数量都更多
        for _ in range(10):
            s._record_dave_result(False, True, bytes([i, i]), ValueError("x"))
    s._record_dave_result(True, False, b"\xfa\xfa")   # 唯一一次成功
    assert "成功" in s._dave_shape_summary(), "成功桶被挤掉了，对比就没了"


def test_shape_tolerates_short_payload():
    """**护栏**：payload 可能是 None 或不足两字节（解密前就崩了）。

    诊断工具自己抛异常会连累 `_probed`，那是主收音路径 —— 宁可打 '??'。
    """
    _reset_reasons()
    s._record_dave_result(False, False, None, ValueError("x"))
    s._record_dave_result(False, False, b"\x01", ValueError("x"))
    assert "尾??" in s._dave_shape_summary()
