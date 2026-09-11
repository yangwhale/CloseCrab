"""开机自启验证器：绝对不许因为「一时没连上」去改持久化配置。

2026-09-11 的实测事故。原来这段是：

    time.sleep(15)
    if not is_voice_connected():
        _persist_sidecar_enabled(bot_name, False)

三处叠加，把一次偶发的握手超时变成了永久失踪：

1. **15s 比 py-cord 自己的语音握手超时 (20s) 还短。** 第一次握手还没跑完就判死刑。
2. **心跳 30s 才发起第二次重试。** 15s 那一刻必然是「没连上」，跟实际能不能连无关。
3. **清标记不影响本进程，只影响下次重启。** 那次重启 sidecar 线程压根不会被拉起，
   日志里一行错都没有 —— 表现就是「Discord 语音这个功能不存在了」。

当天 jarvis 和天猫精灵同时中招，`channels.discord.voice_sidecar` 双双被写成
False，而同一批重启里握手成功的小爱和 bunny 安然无恙 —— 判据完全取决于那 15 秒
的运气。

所以下面三个用例里，**第二个（一直连不上）才是回归护栏**：它断言的是「即使真的
一直没连上，也不许碰配置」。只测第一个（连上了）任何写法都能过。
"""
import logging
import threading
import time

import pytest

import closecrab.voice.discord_voice_sidecar as s


def _run_verify(monkeypatch, connected_after: float, timeout: float = 0.4):
    """跑一次 maybe_start_discord_voice_sidecar 的验证线程，返回 (是否写过配置, 写入值)。

    `connected_after` 是「几秒后语音才连上」，float('inf') 表示永远连不上。
    """
    t0 = time.monotonic()
    writes = []

    monkeypatch.setattr(s, "_BOOT_VERIFY_TIMEOUT", timeout)
    monkeypatch.setattr(s, "_BOOT_VERIFY_POLL", 0.02)
    monkeypatch.setattr(s, "_load_sidecar_config", lambda _b: {
        "token": "tok", "enabled": True, "guild_id": "g", "voice_channel_id": "c",
    })
    # 真 sidecar 要连 Discord，测不了；这里只需要它返回一个非 None 的东西让验证线程起来
    monkeypatch.setattr(s, "_spawn_sidecar_thread",
                        lambda *a, **k: threading.Thread(target=lambda: None))
    monkeypatch.setattr(s, "is_voice_connected",
                        lambda: (time.monotonic() - t0) >= connected_after)
    monkeypatch.setattr(s, "_persist_sidecar_enabled",
                        lambda bot, val: writes.append((bot, val)))

    s.maybe_start_discord_voice_sidecar("testbot")

    for th in threading.enumerate():
        if th.name == "sidecar-boot-verify":
            th.join(timeout + 2)
            assert not th.is_alive(), "验证线程没在窗口内退出"
    return writes


def test_connected_immediately_does_not_touch_config(monkeypatch):
    """连上了 —— 什么都不该写。"""
    assert _run_verify(monkeypatch, connected_after=0.0) == []


def test_never_connects_must_not_disable(monkeypatch, caplog):
    """**回归护栏**：一直连不上也绝不许把 voice_sidecar 写成 False。

    旧代码在这里会写 (bot, False)，下次重启 Discord 语音就整个消失。
    现在只许大声报错，重试交给心跳。
    """
    with caplog.at_level(logging.ERROR, logger="closecrab.discord_voice_sidecar"):
        writes = _run_verify(monkeypatch, connected_after=float("inf"))

    assert writes == [], f"连不上时不许改配置，实际写了 {writes}"
    assert any("仍未连上语音频道" in r.message or "仍未连上语音频道" in r.getMessage()
               for r in caplog.records), "连不上必须留一条 ERROR，不能静默"


def test_slow_connect_past_old_window_survives(monkeypatch):
    """握手比旧的 15s 窗口慢 —— 配置必须原样保留。

    这条是杀变异用的：把窗口改回「等一下就判死刑」的任何写法都会在这里翻车。
    窗口 0.4s、0.25s 才连上，对应真实世界的「第 2~3 次心跳重试才成功」。
    """
    assert _run_verify(monkeypatch, connected_after=0.25, timeout=0.4) == []


def test_boot_verify_window_exceeds_handshake_timeout():
    """默认窗口必须真的大于一次握手超时 + 两轮心跳，否则上面的修法只是搬了个数字。

    py-cord 语音握手超时 20s，心跳 30s 一轮 —— 至少要 20 + 30*2 = 80s 才谈得上
    「量到的是连不上」而不是「还没轮到重试」。
    """
    assert s._BOOT_VERIFY_TIMEOUT >= 80, (
        f"_BOOT_VERIFY_TIMEOUT={s._BOOT_VERIFY_TIMEOUT} 太短，"
        "会在心跳第二次重试之前就下结论"
    )
    assert 0 < s._BOOT_VERIFY_POLL <= 10
