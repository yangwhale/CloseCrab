"""RTCP 账本的单测。

**为什么要有这段代码**：这个探针的第一版（124c590）跑在生产上、每句话都
打一行账、字段全填满 —— 而记下来的每一个数字都是垃圾。原因是 py-cord 的
收包分发对 RTCP **根本不解密**，探针挂在包类构造函数上，拿到的是密文。

密文照样能被 `struct.unpack` 解析成功。所以「探针没报错」「字段有值」
「格式好看」三样全占，结论依然全错。这类 bug 只能靠**外部锚点**发现：
一个几分钟的通话，服务器不可能发了 41 亿个包。

于是这份单测盯的就是那一件事 —— **解析的是解密后的字节，不是原始字节**。
构造上把两者的 ssrc 写成不同的已知值：漏了解密这一步，记进去的就是
另一个号，测试立刻红。
"""
from __future__ import annotations

import struct

import pytest

import closecrab.voice.discord_voice_sidecar as sc


# ── 手搓 RTCP 包 ──────────────────────────────────────────────────────────

def sr_packet(ssrc: int, packet_count: int, octet_count: int = 4096,
              reports: tuple = ()) -> bytes:
    """按 RFC 3550 §6.4.1 拼一个 Sender Report。

    reports 里每项 (ssrc, perc_loss_8bit, total_lost, last_seq)。
    """
    head = 0x80 | (len(reports) & 0x1F)          # version=2, report_count
    body = struct.pack(">BBH", head, 200, 0)
    body += struct.pack(">I", ssrc)
    body += struct.pack(">5I", 0, 0, 0, packet_count, octet_count)
    for rs, perc, lost, seq in reports:
        # 丢包那个字段是「1 字节比例 + 3 字节累计」挤在一个 32 位里
        body += struct.pack(">I", rs)
        body += struct.pack(">I", (perc << 24) | (lost & 0xFFFFFF))
        body += struct.pack(">4I", seq, 0, 0, 0)
    return body


def rr_packet(ssrc: int, reports: tuple = ()) -> bytes:
    """Receiver Report。跟 SR 的差别不只是少了 sender info —— py-cord 里
    它的报告块 namedtuple 字段名也不一样（total_loss vs total_lost）。"""
    head = 0x80 | (len(reports) & 0x1F)
    body = struct.pack(">BBH", head, 201, 0) + struct.pack(">I", ssrc)
    for rs, perc, lost, seq in reports:
        body += struct.pack(">I", rs)
        body += struct.pack(">I", (perc << 24) | (lost & 0xFFFFFF))
        body += struct.pack(">4I", seq, 0, 0, 0)
    return body


@pytest.fixture(autouse=True)
def _clean():
    def _reset():
        sc._rtcp_sr.clear()
        sc._rtcp_rr.clear()
        sc._rtcp_ok = 0
        sc._rtcp_fail = 0
    _reset()
    yield
    _reset()


# ── 记账本身 ──────────────────────────────────────────────────────────────

def test_SR_记下服务器自称发了多少包():
    from discord.voice.packets.rtp import decode
    sc._rtcp_note(decode(sr_packet(15175, packet_count=4321,
                                   octet_count=700000)))
    assert sc._rtcp_sr == {15175: (4321, 700000)}
    assert sc._rtcp_ok == 1


def test_SR_报告块里的丢包被记下():
    from discord.voice.packets.rtp import decode
    sc._rtcp_note(decode(sr_packet(
        15175, 4321, reports=((15100, 26, 137, 9000),))))
    assert sc._rtcp_rr == {15100: (26, 137, 9000)}


def test_RR_的字段名不一样也得记得下来():
    """py-cord 的 RR 报告块叫 total_loss，SR 叫 total_lost。

    写死任何一个，另一类包就 AttributeError —— 而调用处包着 try，
    抛了不会有人喊，只会安安静静少记一半的账。
    """
    from discord.voice.packets.rtp import decode
    pkt = decode(rr_packet(15214, reports=((15100, 51, 999, 12345),)))
    assert hasattr(pkt.reports[0], "total_loss")          # 前提成立才有意义
    assert not hasattr(pkt.reports[0], "total_lost")
    sc._rtcp_note(pkt)
    assert sc._rtcp_rr == {15100: (51, 999, 12345)}
    assert sc._rtcp_ok == 1


def test_记账中途抛了不算成功():
    """成功计数必须记在最后。记在开头的话，一个记到一半就崩的包会同时
    +1 成功 +1 失败 —— 失败率被粉饰，而失败率正是判断探针死没死的唯一信号。"""
    class _半个包:
        info = None
        reports = (object(),)        # 缺 ssrc/perc_loss，遍历时必炸

    with pytest.raises(AttributeError):
        sc._rtcp_note(_半个包())
    assert sc._rtcp_ok == 0


def test_没有报告块的裸SR不炸():
    from discord.voice.packets.rtp import decode
    sc._rtcp_note(decode(sr_packet(15175, 10)))
    assert sc._rtcp_rr == {}
    assert sc._rtcp_ok == 1


# ── 核心：探针必须先解密 ──────────────────────────────────────────────────

class _FakeDecryptor:
    """假解密器：不管喂什么，都吐出事先准备好的明文。

    真解密走 nacl，需要会话密钥，单测里造不出来。但要验的不是密码学 ——
    是**调用顺序**：探针到底把哪一串字节交给了 parser。
    """

    def __init__(self, plain: bytes) -> None:
        self.plain = plain
        self.seen: list[bytes] = []

    def _decryptor_rtcp(self, data: bytes) -> bytes:
        self.seen.append(data)
        return self.plain


class _FakeReader:
    def __init__(self, plain: bytes) -> None:
        self.decryptor = _FakeDecryptor(plain)


def _patched_callback():
    sc._install_receive_probe()
    from discord.voice.receive.reader import AudioReader
    assert getattr(AudioReader, "_cc_rtcp_probed", False), "探针没挂上"
    return AudioReader.callback


def _fire(cb, reader, data: bytes) -> None:
    """把包喂给打过补丁的 callback。

    补丁末尾会去调 py-cord 原本的 callback，那个在假 reader 上必然炸
    （没有 packet_router 之类）—— 那一半不是这份测试的对象，吞掉。
    我们要验的是它**前面**那一半有没有把账记对。
    """
    try:
        cb(reader, data)
    except Exception:
        pass


def test_探针解析的是明文不是原始字节():
    """最要命的那条。密文和明文写成两个不同的已知 ssrc：
    只要探针漏掉解密这一步，记进账的就是 88888888，测试立刻红。"""
    cb = _patched_callback()
    plain = sr_packet(15175, packet_count=4321)
    cipher = sr_packet(88888888, packet_count=4161767572)   # 冒充密文
    reader = _FakeReader(plain)

    _fire(cb, reader, cipher)

    assert reader.decryptor.seen == [cipher], "解密器没拿到原始字节"
    assert sc._rtcp_sr == {15175: (4321, 4096)}
    assert 88888888 not in sc._rtcp_sr, "读的是密文 —— 正是第一版那个 bug"


def test_解不开的包只记失败不记账():
    cb = _patched_callback()

    class _Boom(_FakeReader):
        def __init__(self):
            super().__init__(b"")
            self.decryptor._decryptor_rtcp = self._raise

        @staticmethod
        def _raise(data):
            raise ValueError("bad mac")

    _fire(cb, _Boom(), sr_packet(15175, 4321))
    assert sc._rtcp_sr == {} and sc._rtcp_rr == {}
    assert sc._rtcp_ok == 0 and sc._rtcp_fail == 1


def test_RTP包不走这条路():
    """`is_rtcp` 看的是第 2 个字节。音频包（payload type 120）不该被当 RTCP
    去解 —— 那会白白多一次解密，还会把 _rtcp_fail 刷满。"""
    cb = _patched_callback()
    reader = _FakeReader(sr_packet(15175, 4321))
    _fire(cb, reader, struct.pack(">BBH", 0x80, 120, 1) + b"\x00" * 20)
    assert reader.decryptor.seen == []
    assert sc._rtcp_ok == 0 and sc._rtcp_fail == 0


# ── 摘要：空账的两种成因必须分得开 ────────────────────────────────────────

def test_一个RTCP都没收到():
    assert sc._rtcp_summary() == "RTCP 无"


def test_收到了但全解不开_要说出来():
    """跟上面那条打出来的字必须不一样。两者账都是空的，但一个是
    「没证据」、一个是「探针坏了」—— 混成同一句话，人会对着空账干等。"""
    sc._rtcp_fail = 7
    s = sc._rtcp_summary()
    assert s != "RTCP 无" and "7" in s, s


def test_报告块太多时截断并给出总数():
    """真实会话里报告块就那么几条。一旦刷出几十个陌生 ssrc，那本身就是
    「又在读密文」的信号 —— 要看得见总数，但不能让它把日志撑爆。"""
    for i in range(20):
        sc._rtcp_rr[900000 + i] = (10, 5, 1)
    sc._rtcp_ok = 1
    s = sc._rtcp_summary()
    assert "共20" in s, s
    assert s.count("丢5") == 6, s
