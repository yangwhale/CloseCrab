"""语料推送：只推「已经写完」的 wav。

判据是同名 .json 存在 —— sidecar 先写 wav 再写 json。只盯 wav 会推出半截文件，
而半截文件在飞书里听起来就是一句被截断的话，人会以为是录音坏了、去查错的地方。
下面第二个用例就是护这条的。
"""
import importlib.util
import os
import pathlib

_spec = importlib.util.spec_from_file_location(
    "voice_corpus_push",
    pathlib.Path(__file__).parent / "scripts" / "voice-corpus-push.py")
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)


def _wav(d, name, with_json=True):
    (d / name).write_bytes(b"RIFF----WAVE")
    if with_json:
        (d / (name[:-4] + ".json")).write_text("{}")


def test_finds_completed_wavs(tmp_path):
    _wav(tmp_path, "001.wav")
    _wav(tmp_path, "002.wav")
    got = [os.path.basename(p) for p in push.find_new(str(tmp_path), set())]
    assert got == ["001.wav", "002.wav"]


def test_wav_without_json_is_not_pushed_yet(tmp_path):
    """**核心不变量**：json 还没落地 = wav 还在写，这一轮不许推。"""
    _wav(tmp_path, "001.wav")
    _wav(tmp_path, "002.wav", with_json=False)
    got = [os.path.basename(p) for p in push.find_new(str(tmp_path), set())]
    assert got == ["001.wav"], f"推了还没写完的文件: {got}"


def test_seen_is_not_repushed(tmp_path):
    """已推过的不许再推 —— 否则每秒一轮会把同一句刷满整个聊天窗口。"""
    _wav(tmp_path, "001.wav")
    seen = set(push.find_new(str(tmp_path), set()))
    assert push.find_new(str(tmp_path), seen) == []


def test_walks_session_subdirs(tmp_path):
    """录音按轮次分子目录，扫描必须递归。"""
    sub = tmp_path / "20260911-143000"
    sub.mkdir()
    _wav(sub, "001.wav")
    got = [os.path.basename(p) for p in push.find_new(str(tmp_path), set())]
    assert got == ["001.wav"]
