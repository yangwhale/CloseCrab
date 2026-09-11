"""Opus TOC 解析的单测。

这段代码的产出会被直接当成结论用（「Discord 发的是 superwideband」），
所以查错了比不查更糟 —— 它会让人拿一个错误的带宽去调下游。RFC 6716 表 2
的每一段区间边界都在这里钉一遍。

**负例是重点**：空包、None、越界都不能记，也不能抛 —— 这个函数在每一个
音频包上跑，抛一次就是一帧静音。
"""
from __future__ import annotations

import pytest

import closecrab.voice.discord_voice_sidecar as sc


def toc(cfg: int, stereo: bool = False, code: int = 0) -> bytes:
    """按 RFC 6716 §3.1 拼一个 TOC 字节：cccccsff（config 5 位 / stereo 1 位 / code 2 位）。"""
    return bytes([(cfg << 3) | (0x04 if stereo else 0) | code]) + b"\x00" * 8


@pytest.fixture(autouse=True)
def _clean():
    sc._opus_toc_stats.clear()
    yield
    sc._opus_toc_stats.clear()


def only_key() -> str:
    assert len(sc._opus_toc_stats) == 1, dict(sc._opus_toc_stats)
    return next(iter(sc._opus_toc_stats))


# ── RFC 6716 表 2 的九段区间，每段量两个边界 ──────────────────────────────

@pytest.mark.parametrize("cfg,mode,bw", [
    (0, "SILK", "NB 4kHz"), (3, "SILK", "NB 4kHz"),
    (4, "SILK", "MB 6kHz"), (7, "SILK", "MB 6kHz"),
    (8, "SILK", "WB 8kHz"), (11, "SILK", "WB 8kHz"),
    (12, "Hybrid", "SWB 12kHz"), (13, "Hybrid", "SWB 12kHz"),
    (14, "Hybrid", "FB 20kHz"), (15, "Hybrid", "FB 20kHz"),
    (16, "CELT", "NB 4kHz"), (19, "CELT", "NB 4kHz"),
    (20, "CELT", "WB 8kHz"), (23, "CELT", "WB 8kHz"),
    (24, "CELT", "SWB 12kHz"), (27, "CELT", "SWB 12kHz"),
    (28, "CELT", "FB 20kHz"), (31, "CELT", "FB 20kHz"),
])
def test_config_maps_to_rfc_table(cfg, mode, bw):
    sc._record_opus_toc(toc(cfg))
    assert only_key() == f"{mode}/{bw}/单声道/cfg{cfg}"


def test_stereo_bit():
    sc._record_opus_toc(toc(12, stereo=True))
    assert only_key() == "Hybrid/SWB 12kHz/立体声/cfg12"


def test_frame_count_code_does_not_leak_into_config():
    """低 2 位是帧数，绝不能污染 config —— 错了会把 SWB 读成别的带宽。"""
    for code in range(4):
        sc._opus_toc_stats.clear()
        sc._record_opus_toc(toc(12, code=code))
        assert only_key() == "Hybrid/SWB 12kHz/单声道/cfg12"


def test_counts_accumulate():
    for _ in range(5):
        sc._record_opus_toc(toc(12))
    sc._record_opus_toc(toc(15))
    assert sc._opus_toc_stats["Hybrid/SWB 12kHz/单声道/cfg12"] == 5
    assert sc._opus_toc_stats["Hybrid/FB 20kHz/单声道/cfg15"] == 1


# ── 负例：什么都不该记，更不该抛 ──────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, b"", bytearray()])
def test_empty_payload_records_nothing(bad):
    sc._record_opus_toc(bad)
    assert not sc._opus_toc_stats


def test_summary_is_dash_when_empty():
    """没数据时打 '-'，不要打空字符串 —— 日志里空字段看不出是没数据还是字段漏了。"""
    assert sc._opus_toc_summary() == "-"


def test_summary_lists_most_common_first():
    for _ in range(3):
        sc._record_opus_toc(toc(15))
    sc._record_opus_toc(toc(12))
    s = sc._opus_toc_summary()
    assert s.startswith("Hybrid/FB 20kHz/单声道/cfg15×3")
    assert "cfg12×1" in s
