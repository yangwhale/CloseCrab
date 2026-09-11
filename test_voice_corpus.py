"""语音回归语料录制：按「包间隔」断句，一句一个 wav。

为什么不用能量 VAD：Discord 只在有人说话时才发 Opus 包，静默期整个不发包。
所以「多久没收到新包」本身就是断句信号，比在波形上算能量更准也更便宜。
下面第二个用例就是护这条不变量的 —— 中间隔一个静默期必须切成两个文件。
"""
import os
import time
import wave

import pytest

import closecrab.voice.discord_voice_sidecar as s


def _burst(sec: float) -> bytes:
    """sec 秒的 48kHz mono s16 PCM（内容无所谓，测的是切分不是识别）。"""
    return b"\x01\x00" * int(48000 * sec)


@pytest.fixture
def rec(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_UTT_ENABLED", True)
    monkeypatch.setattr(s, "_UTT_GAP", 0.15)
    monkeypatch.setattr(s, "_UTT_MIN_SEC", 0.35)
    monkeypatch.setattr(s, "_utt_dir", str(tmp_path))
    monkeypatch.setattr(s, "_utt_seq", 0)
    monkeypatch.setattr(s, "_utt_thread", None)
    s._utt_buf.clear()
    yield tmp_path


def _wavs(d):
    return sorted(f for f in os.listdir(d) if f.endswith(".wav"))


def test_one_burst_becomes_one_wav(rec):
    s._utterance_feed(_burst(1.0))
    time.sleep(0.6)
    assert _wavs(rec) == ["001.wav"]
    with wave.open(str(rec / "001.wav")) as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 48000
        assert abs(wf.getnframes() / 48000 - 1.0) < 0.05


def test_gap_splits_into_two_utterances(rec):
    """**核心不变量**：中间静默超过 _UTT_GAP 必须切开。

    切不开的话 12 句会连成一个大文件，整套回归语料直接报废。
    """
    s._utterance_feed(_burst(0.6))
    time.sleep(0.5)                     # > GAP，第一句应已落盘
    s._utterance_feed(_burst(0.6))
    time.sleep(0.5)
    assert _wavs(rec) == ["001.wav", "002.wav"]


def test_continuous_feed_does_not_split(rec):
    """连续说话（包一直来）不许被切碎 —— 只有真静默才算说完。"""
    for _ in range(8):
        s._utterance_feed(_burst(0.1))
        time.sleep(0.05)                # 远小于 GAP
    time.sleep(0.5)
    assert _wavs(rec) == ["001.wav"], "连续输入被切成了多段"


def test_too_short_burst_is_dropped(rec):
    """短于 _UTT_MIN_SEC 判为杂音，不落盘 —— 否则每次咳嗽都进语料。"""
    s._utterance_feed(_burst(0.1))
    time.sleep(0.5)
    assert _wavs(rec) == []


def test_disabled_writes_nothing(rec, monkeypatch):
    """开关关掉时一个字节都不许落盘（默认部署不该偷偷录音）。"""
    monkeypatch.setattr(s, "_UTT_ENABLED", False)
    s._utterance_feed(_burst(2.0))
    time.sleep(0.5)
    assert _wavs(rec) == []
    assert len(s._utt_buf) == 0, "关掉时连缓冲都不该攒"


def test_sidecar_json_written_for_correlation(rec):
    """每个 wav 配一份时间戳 json，之后要靠它跟 bridge 的交付日志对齐。"""
    s._utterance_feed(_burst(1.0))
    time.sleep(0.6)
    import json
    meta = json.loads((rec / "001.json").read_text())
    assert meta["seq"] == 1
    assert abs(meta["dur_sec"] - 1.0) < 0.05
    assert meta["epoch"] > 0
