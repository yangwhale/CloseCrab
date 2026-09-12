#!/usr/bin/env python3
"""接线回归 —— `playback.py` 把统一播放器接到三个真出口上，这里测那层接线。

播放逻辑本身在 `test_voice_player.py` 里测过了（假出口、离线、10 倍速）。这份测的
是**只有接上真东西才会坏的那些地方**：

- Discord 和 Zello 的互斥（人的耳朵只在一边）
- 追帧垫：py-cord 那边空了就先垫静音，不空一帧都不加
- 暂停时 Discord 仍要收到帧，否则 py-cord 的 idle 自停会把播放掐了
- Zello 的收尾发在**播完**那一刻，不是 TTS 生成完那一刻
- 位置写回 sidecar 那份 `_progress`，飞书进度条照旧读得到

三个外部模块用假的，塞进 `sys.modules` —— 连 `from . import xxx` 那条导入路径
一起测到，不是 monkeypatch 掉函数了事。
"""
import os
import struct
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FRAME = 3840
SILENCE = b"\x00" * FRAME


# ── 假的 py-cord 持久 source：只记账，不真发包 ──────────────────────────
class FakeSource:
    def __init__(self):
        self.buf = bytearray()
        self.frames = []          # 每次 write 的内容，顺序保留
        self.cleared = 0

    def write(self, pcm):
        self.buf.extend(pcm)
        self.frames.append(pcm)

    def buffered(self):
        return len(self.buf)

    def clear(self):
        self.buf.clear()
        self.cleared += 1

    def drain_one(self):
        """模拟 py-cord 播放线程每 20ms 取走一帧。"""
        if len(self.buf) >= FRAME:
            del self.buf[:FRAME]


def fake_modules():
    """造三个假模块并注册进 sys.modules，让 playback 的懒加载真的能 import 到。"""
    import closecrab.voice as pkg

    dvs = types.ModuleType("closecrab.voice.discord_voice_sidecar")
    dvs.progress = {"fid": "", "played": 0, "total": 0, "active": False}
    dvs._persistent_source = None
    dvs._dc_connected = False
    dvs._sidecar_loop = None
    dvs._sidecar_bot = None
    dvs.is_voice_connected = lambda: dvs._dc_connected

    def _set_progress(fid=None, *, played=None, total=None, active=None):
        if fid is not None:
            dvs.progress["fid"] = fid
        if played is not None:
            dvs.progress["played"] = played
        if total is not None:
            dvs.progress["total"] = total
        if active is not None:
            dvs.progress["active"] = active
    dvs._set_progress = _set_progress
    dvs._get_persistent_source = lambda: dvs._persistent_source

    zsv = types.ModuleType("closecrab.voice.zello_voice_sidecar")
    zsv._online = False
    zsv.frames = []
    zsv.done_calls = 0
    zsv.is_connected = lambda: zsv._online
    zsv.zello_buf_write_threadsafe = zsv.frames.append

    def _done():
        zsv.done_calls += 1
    zsv.zello_signal_done_threadsafe = _done

    lko = types.ModuleType("closecrab.voice.livekit_out")
    lko._online = False
    lko.frames = []
    lko.cleared = 0
    lko.is_connected = lambda: lko._online
    lko.write_threadsafe = lko.frames.append

    def _clear():
        lko.cleared += 1
        lko.frames.clear()
    lko.clear = _clear

    for name, mod in (("discord_voice_sidecar", dvs),
                      ("zello_voice_sidecar", zsv), ("livekit_out", lko)):
        sys.modules[f"closecrab.voice.{name}"] = mod
        setattr(pkg, name, mod)
    return dvs, zsv, lko


DVS, ZSV, LKO = fake_modules()

from closecrab.voice import playback  # noqa: E402

BUF = "/tmp/playback-wiring-test"


def ramp(nframes):
    return b"".join(struct.pack("<h", i % 30000 + 1) * (FRAME // 2)
                    for i in range(nframes))


def write_buf(fid, data):
    os.makedirs(BUF, exist_ok=True)
    p = os.path.join(BUF, f"{fid}.pcm")
    with open(p, "wb") as f:
        f.write(data)
    return p


def reset(*, discord=False, zello=False, livekit=False):
    """每个用例开始前把三个出口和播放器都归零。"""
    p = playback.get_player()
    p.stop_playback()
    p.buf_dir = BUF
    DVS._dc_connected = discord
    DVS._persistent_source = FakeSource() if discord else None
    DVS.progress.update(fid="", played=0, total=0, active=False)
    ZSV._online = zello
    ZSV.frames.clear()
    ZSV.done_calls = 0
    LKO._online = livekit
    LKO.frames.clear()
    LKO.cleared = 0
    playback._last_active = False
    return p


def drain_discord(seconds):
    """一边等一边模拟 py-cord 每 20ms 取一帧 —— 不取的话 buffer 只涨不落，
    追帧垫那条用例会看不出区别。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        src = DVS._persistent_source
        if src is not None:
            src.drain_one()
        time.sleep(0.02)


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


# ── 1. 互斥：Discord 在就不占 Zello，LiveKit 并联 ──────────────────────
def t_outlets():
    reset(discord=True, zello=True, livekit=True)
    outs = playback.outlets()
    check("Discord 在线时 Zello 让位（人的耳朵只在一边）",
          outs["discord"] and not outs["zello"], str(outs))
    check("LiveKit 并联，不跟前两个抢", outs["livekit"], str(outs))

    reset(discord=False, zello=True, livekit=True)
    outs = playback.outlets()
    check("Discord 不在时 Zello 顶上", outs["zello"] and not outs["discord"], str(outs))

    reset()
    check("三路全离线时 any_online() 为 False", not playback.any_online())


# ── 2. 追帧垫：空了才垫，不空一帧都不加 ────────────────────────────────
def t_lead():
    reset(discord=True)
    src = DVS._persistent_source
    write_buf("lead", ramp(20))
    playback.replay("lead")
    time.sleep(0.06)                       # 让它写进头两三帧，先不取走
    first = src.frames[0] if src.frames else b""
    check("第一次写之前先垫了静音（py-cord 那边本来是空的）",
          first == SILENCE * 5, f"首次写入 {len(first)}B，期望 {5 * FRAME}B 静音")
    n_before = len(src.frames)
    time.sleep(0.06)
    # buffer 里还压着垫的 5 帧，不该再垫
    padded = sum(1 for f in src.frames[n_before:] if f == SILENCE * 5)
    check("buffer 没空就一帧都不再垫", padded == 0,
          f"又垫了 {padded} 次")
    playback.stop()


# ── 3. 暂停：Discord 仍收帧（防 idle 自停），LiveKit 真安静 ────────────
def t_pause_keeps_discord_alive():
    reset(discord=True, livekit=True)
    src = DVS._persistent_source
    write_buf("pz", ramp(100))
    playback.replay("pz")
    drain_discord(0.2)
    playback.pause()
    n_dc, n_lk = len(src.frames), len(LKO.frames)
    pos = playback.progress()[0]
    drain_discord(0.4)                      # 暂停期间持续 20 帧左右
    got_dc = len(src.frames) - n_dc
    got_lk = len(LKO.frames) - n_lk
    check("暂停时 Discord 仍在收帧（否则 py-cord 2 秒后自停）", got_dc >= 10,
          f"暂停 0.4s 收到 {got_dc} 帧")
    # 只验「全是静音」不验「恰好一帧」—— buffer 万一被取空，追帧垫会合法地
    # 一次写 5 帧，那也还是静音。
    check("暂停时给 Discord 的是静音，不是往下播",
          all(not f.strip(b"\x00") for f in src.frames[n_dc:]), f"{got_dc} 帧")
    check("暂停时 LiveKit 真安静", got_lk == 0, f"LiveKit 多收了 {got_lk} 帧")
    check("暂停期间位置不动", abs(playback.progress()[0] - pos) < 1e-9,
          f"{pos:.3f} → {playback.progress()[0]:.3f}")
    playback.stop()


# ── 4. 进度桥接：飞书那条读进度的路不能断 ──────────────────────────────
def t_progress_bridge():
    reset(livekit=True)
    write_buf("pg", ramp(30))
    playback.replay("pg")
    time.sleep(0.2)
    check("位置写回了 sidecar 的 _progress", DVS.progress["fid"] == "pg"
          and DVS.progress["played"] > 0 and DVS.progress["active"],
          str(DVS.progress))
    time.sleep(0.8)
    check("播完后 active 翻 False、fid 保留（卡片靠它显示已播完）",
          not DVS.progress["active"] and DVS.progress["fid"] == "pg",
          str(DVS.progress))


# ── 5. Zello 收尾发在播完那一刻，不是生成完那一刻 ──────────────────────
def t_zello_done_edge():
    reset(zello=True)
    playback.begin("zd")
    playback.feed(ramp(25))
    playback.end()                          # 生成完了，但还差半秒才播完
    time.sleep(0.1)
    check("生成完还没播完时不发收尾", ZSV.done_calls == 0,
          f"已发 {ZSV.done_calls} 次")
    t0 = time.time()
    while time.time() - t0 < 3 and playback.progress()[2]:
        time.sleep(0.02)
    time.sleep(0.1)
    check("播完了才发收尾，而且只发一次", ZSV.done_calls == 1,
          f"发了 {ZSV.done_calls} 次")
    check("Zello 收到了全部实音",
          b"".join(f for f in ZSV.frames if f.strip(b"\x00")) == ramp(25),
          f"实音 {sum(1 for f in ZSV.frames if f.strip(bytes(1)))} 帧")

    # 直接测回调的契约：**只在 True→False 那一下发**，不是「只要没在播就发」。
    # 今天播放器的每个 _report() 调用点都有状态守卫，走不出连着两次 inactive
    # 的路 —— 所以这条只能直接调 _on_progress 来测。留着它是因为哪天谁加一个
    # 没守卫的汇报点，坏法是 Zello 的 PTT 松开键被反复按，很难从现象倒查回来。
    playback._on_progress("zd", 100, 100, False)
    check("收尾只认播完那一下，重复汇报不再发", ZSV.done_calls == 1,
          f"发了 {ZSV.done_calls} 次")


# ── 6. barge-in：三路一起闭嘴，队列一起清 ──────────────────────────────
def t_interrupt():
    reset(discord=True, livekit=True)
    src = DVS._persistent_source
    write_buf("bi", ramp(200))
    playback.replay("bi")
    drain_discord(0.2)
    playback.stop()
    n_dc, n_lk = len(src.frames), len(LKO.frames)
    time.sleep(0.2)
    check("打断后 Discord 一帧不再写", len(src.frames) == n_dc,
          f"又写了 {len(src.frames) - n_dc} 帧")
    check("打断后 LiveKit 一帧不再写", len(LKO.frames) == n_lk,
          f"又写了 {len(LKO.frames) - n_lk} 帧")
    check("打断时两路排队的音频都清了", src.cleared >= 1 and LKO.cleared >= 1,
          f"discord={src.cleared} livekit={LKO.cleared}")


def main():
    import shutil
    try:
        for fn in (t_outlets, t_lead, t_pause_keeps_discord_alive,
                   t_progress_bridge, t_zello_done_edge, t_interrupt):
            print(f"\n── {fn.__name__} ──")
            fn()
    finally:
        playback.get_player().close()
        shutil.rmtree(BUF, ignore_errors=True)
    bad = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} 通过"
          + ("" if not bad else f" —— 失败: {bad}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
