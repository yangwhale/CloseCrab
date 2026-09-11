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
    assert "无扩展头/无填充/尾fafa/成功×1" in line, line
    assert "有扩展头/无填充/尾0001/失败×1" in line, line


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


# ─── dave-py 账本转接（DaveSessionAdapter.get_decryption_stats） ─────────────
#
# 换回 dave-py 后 `_probed` 侧零失败，但这只说明 decrypt() 返回了非 None ——
# **分不清是真解开了还是当明文透传了**。这两件事对「怎么修官方 davey」的结论
# 完全相反，而 dave-py 的 passthrough_count 正好把它们分开。所以这个转接层
# 唯一的价值就是**把 passthroughs 单独拎出来**，混进 successes 等于白做。

class _PyStats:
    """dave-py 的 DecryptorStats 形状（字段名跟 davey 完全不同）。"""
    def __init__(self, ok, bad, pt):
        self.decrypt_success_count = ok
        self.decrypt_failure_count = bad
        self.passthrough_count = pt
        self.decrypt_attempts = ok + bad + pt
        self.decrypt_duration = 0


class _PyDecryptor:
    def __init__(self, stats):
        self._stats = stats

    def get_stats(self, media_type):
        return self._stats


def _adapter(table):
    """绕开 __init__（它要真去 import dave 建 Session），只装这个方法要的两个字段。"""
    a = s.DaveSessionAdapter.__new__(s.DaveSessionAdapter)
    a._decryptors = table
    a._MT_AUDIO = "audio"
    return a


def test_pystats_are_translated_to_davey_shape():
    a = _adapter({"7": _PyDecryptor(_PyStats(400, 0, 45))})
    st = a.get_decryption_stats(7)
    assert (st.successes, st.failures, st.passthroughs) == (400, 0, 45)


def test_passthrough_is_not_folded_into_successes():
    """**护栏**：透传数不许并进成功数。

    并进去的话账本会打出「成功 445 / 透传 0」—— 跟「真解开 445 帧」长得一模一样，
    而这两种情况指向完全相反的修法。这个转接层存在的唯一理由就是区分它俩。
    """
    a = _adapter({"7": _PyDecryptor(_PyStats(400, 0, 45))})
    st = a.get_decryption_stats(7)
    assert st.successes == 400, "透传被算进成功了，这个测量就废了"
    assert st.passthroughs == 45


def test_lookup_uses_string_key():
    """**护栏**：decrypt() 存的是 str(user_id)，账本传进来的是 int。

    不转字符串就永远查不到 —— 而失败形式是 ValueError，
    在日志里跟「这人还没进 MLS 组」一模一样，会被当成正常现象忽略掉。
    """
    a = _adapter({"7": _PyDecryptor(_PyStats(1, 0, 0))})
    assert a.get_decryption_stats(7).successes == 1      # int 也要能查到
    assert a.get_decryption_stats("7").successes == 1


def test_missing_decryptor_raises_not_zeros():
    """**护栏**：查不到这个人时必须抛，不能返回一排 0。

    返回 0 会在账本里显示成「成功0/失败0/透传0」，看着像「这人没说话」，
    实际是我们根本没在测量他。宁可打一行异常。
    """
    a = _adapter({})
    try:
        a.get_decryption_stats(7)
    except ValueError:
        return
    raise AssertionError("查不到 decryptor 时静默返回了，不许这样")


# ─── RTP 尾部填充 ────────────────────────────────────────────────────────────
#
# 病因：DAVE 的「我是加密帧」标记在**帧尾**，RTP 填充盖在它上面，marker 就
# 找不着了 → UnencryptedWhenPassthroughDisabled → 换成静音帧 → 咯楞。
# py-cord 解析了 packet.padding 却从不切，因为它那条路直接喂 Opus，Opus 忍得了。

def test_padding_is_stripped_when_p_bit_set():
    """P 位置起 + 合法长度 → 按最后一个字节切掉。

    观测到的真实形态：末 17 字节全是 0x11（0x11 = 17，含它自己）。
    """
    body = b"DAVEFRAME\xfa\xfa"
    pkt = body + bytes([0x11]) * 0x11
    assert s._strip_rtp_padding(True, pkt) == body


def test_no_strip_when_p_bit_clear():
    """**护栏**：P 位没置起就一个字节都不许动。

    这是「不猜」那条线。尾部有一串相同字节的合法密文是可能存在的，
    按形状猜着切会把好帧切坏 —— 而切坏的表现同样是一帧静音，
    跟没切时一模一样，等于给自己埋一个查不出来的雷。
    """
    pkt = b"CIPHER" + bytes([0x11]) * 0x11
    assert s._strip_rtp_padding(False, pkt) == pkt


def test_pad_len_zero_does_not_wipe_payload():
    """**护栏**：pad_len=0 是非法值，而且踩 Python 的 `x[:-0] == b''` 陷阱。

    不挡这一下，一个畸形包会让整帧变成空字节 —— 下游看到的是「解密出 0 字节」，
    完全不像「填充算错了」。
    """
    pkt = b"CIPHERTEXT\x00"
    assert s._strip_rtp_padding(True, pkt) == pkt


def test_pad_len_longer_than_payload_is_ignored():
    """**护栏**：越界说明 P 位不可信，原样放行比切坏强。"""
    pkt = b"\x05\xff"          # 说填了 255 字节，实际只有 2 字节
    assert s._strip_rtp_padding(True, pkt) == pkt


def test_empty_payload_is_safe():
    assert s._strip_rtp_padding(True, b"") == b""


def test_shape_key_records_padding_flag():
    """填充这一维要进分桶 —— 不然改完还是不知道是不是它治好的。"""
    _reset_reasons()
    s._record_dave_result(True, True, b"\xfa\xfa", padded=True)
    s._record_dave_result(True, True, b"\xfa\xfa", padded=False)
    line = s._dave_shape_summary()
    assert "有扩展头/有填充/尾fafa/成功×1" in line, line
    assert "有扩展头/无填充/尾fafa/成功×1" in line, line


# ── 「连着但聋」第二条判据 ────────────────────────────────────────────────
# 现场（2026-09-11 18:10）：ready=True epoch=1，双向不通，ssrc_map 里的门牌号
# 一个都没实收到，hits=0。老判据只看 ready，永远不触发。


class _Member:
    def __init__(self, bot=False):
        self.bot = bot


class _State:
    """SpeakingState 的替身。注意 **标准 Enum 成员恒真**，所以 none(0) 也是
    truthy —— 这正是 `_note_speaking` 必须取 int 而不能 `if state` 的原因。"""
    def __init__(self, value):
        self.value = value

    def __int__(self):
        return self.value

    def __bool__(self):
        return True


def _clear_speak():
    s._speak_start_ts = 0.0
    s._speak_start_hits = None


def test_no_speaking_event_is_never_deaf():
    """**护栏**：安静的房间不算故障。

    没有这条，判据退化成「hits 长时间不涨」，会在没人说话时每分钟把语音连接
    掐断重连一次 —— 比原来的病还糟。
    """
    _clear_speak()
    assert s._deaf_verdict(now=1e9, hits=0) is False


def test_speaking_then_no_hits_is_deaf():
    _clear_speak()
    assert s._note_speaking(_Member(), _State(1), hits=100, now=1000.0) is True
    assert s._deaf_verdict(now=1000.0 + s._DEAF_GRACE_S - 0.1, hits=100) is False
    assert s._deaf_verdict(now=1000.0 + s._DEAF_GRACE_S + 0.1, hits=100) is True


def test_hits_growing_clears_the_case():
    """声音进来了就销案 —— 说一句话不该留下一个待判的故障。"""
    _clear_speak()
    s._note_speaking(_Member(), _State(1), hits=100, now=1000.0)
    assert s._deaf_verdict(now=1000.1, hits=101) is False
    # 销案之后哪怕过了宽限期也不该再判
    assert s._deaf_verdict(now=1000.0 + s._DEAF_GRACE_S + 5, hits=101) is False


def test_bot_speaking_is_ignored():
    """**护栏**：bot 自己发 TTS 也会被服务端通报。

    不滤掉的话，bunny 每说一句话就给自己记一笔「有人在说话」，而自己的声音
    永远不会进 hits —— 于是在一条完全健康的连接上每 5 秒重连一次。
    """
    _clear_speak()
    assert s._note_speaking(_Member(bot=True), _State(1), hits=0, now=1000.0) is False
    assert s._deaf_verdict(now=1e9, hits=0) is False


def test_speaking_stop_is_ignored():
    """**护栏**：speaking=0 是「说完了」，不是「开始说」。

    `bool(SpeakingState.none)` 是 True，用 `if state` 判会把每次说话结束都
    当成新的说话开始，宽限期后必然误判聋。
    """
    _clear_speak()
    assert s._note_speaking(_Member(), _State(0), hits=0, now=1000.0) is False
    assert s._deaf_verdict(now=1e9, hits=0) is False


def test_unknown_member_is_ignored():
    """member 解析不出来时无从判断是不是 bot，宁可漏判不可误判。"""
    _clear_speak()
    assert s._note_speaking(None, _State(1), hits=0, now=1000.0) is False
    assert s._deaf_verdict(now=1e9, hits=0) is False


def test_bitfield_speaking_state_is_accepted():
    """Discord 下发的是位域，voice|priority=5 不是枚举成员，try_enum 会原样
    返回裸 int —— 认不出就漏判，那次真聋了也不会自愈。"""
    _clear_speak()
    assert s._note_speaking(_Member(), 5, hits=0, now=1000.0) is True


def test_verdict_fires_only_once_per_event():
    """判过一次就销案，限流交给冷却 —— 否则守护每 0.3 秒一轮会连着判几十次。"""
    _clear_speak()
    s._note_speaking(_Member(), _State(1), hits=0, now=1000.0)
    late = 1000.0 + s._DEAF_GRACE_S + 1
    assert s._deaf_verdict(now=late, hits=0) is True
    assert s._deaf_verdict(now=late + 1, hits=0) is False
