#!/usr/bin/env python3
"""客户端遥控播放器的 RPC —— 把飞书卡片上那五个按钮搬到 app 里。

Chris 2026-09-17：「你就想办法把那个控制开关搬过来就行」。
背景是飞书卡片上有一整套播放控制（暂停/继续/重播/快进退），
而 iOS app 里一个都没有 —— 人在手机上听，却要切到飞书去按暂停。

⚠️ 这份测试不导入 livekit_out 整个模块（它拖 livekit SDK 和一条 asyncio 循环），
   直接把 `_register_playback_rpc` 的源码抠出来 exec，喂一个假 room 和假 playback。
   被测的是 handler 里的判断，那部分没有外部依赖。
"""
import asyncio
import io
import json
import re
import sys
import types

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ✅", name)
    else:
        fail += 1
        print("  ❌", name, f"— {detail}" if detail else "")


SRC = "closecrab/voice/livekit_out.py"
src = io.open(SRC, encoding="utf-8").read()

_m = re.search(r"^_RPC_PREFIX = (.+)$", src, re.M)
assert _m, "_RPC_PREFIX 不见了"
RPC_PREFIX = eval(_m.group(1).strip())

_m = re.search(r"^def _register_playback_rpc\(room\).*?\n(?=\S)", src, re.M | re.S)
assert _m, "_register_playback_rpc 不见了"


class FakeParticipant:
    def __init__(self):
        self.methods = {}

    def register_rpc_method(self, name, fn):
        self.methods[name] = fn


class FakeRoom:
    def __init__(self):
        self.local_participant = FakeParticipant()


class FakePlayback:
    """假播放器。每个控制记一次调用，返回值可控。"""

    def __init__(self):
        self.calls = []
        self.ret = True
        self.prog = None
        self.replayed = []

    def pause(self):
        self.calls.append("pause"); return self.ret

    def resume(self):
        self.calls.append("resume"); return self.ret

    def stop(self):
        self.calls.append("stop"); return self.ret

    def replay(self, fid):
        self.calls.append("replay"); self.replayed.append(fid); return self.ret

    def seek(self, frac):
        self.calls.append(("seek", frac)); return self.ret

    def progress(self):
        self.calls.append("progress"); return self.prog


def build():
    """装出一套 handler。把 `from . import playback` 换成我们的假货。"""
    pb = FakePlayback()
    fake_pkg = types.ModuleType("fakepkg")
    fake_pkg.playback = pb
    ns = {"_json": json, "log": types.SimpleNamespace(
        info=lambda *a, **k: None, exception=lambda *a, **k: None)}
    body = _m.group(0).replace("from . import playback", "playback = _PB")
    ns["_PB"] = pb
    ns["_RPC_PREFIX"] = RPC_PREFIX
    exec(body, ns)
    room = FakeRoom()
    ns["_register_playback_rpc"](room)
    return room.local_participant.methods, pb


def call(methods, name, payload=None):
    data = types.SimpleNamespace(payload=(json.dumps(payload) if payload is not None else None))
    return json.loads(asyncio.run(methods[RPC_PREFIX + name](data)))


print("\n── 六个方法都注册上了 ──")
methods, pb = build()
want = {"pause", "resume", "stop", "replay", "seek", "progress"}
got = {n[len(RPC_PREFIX):] for n in methods}
check("六个方法齐全", got == want, f"缺 {want - got}，多 {got - want}")
check("⭐ 方法名带 cc. 前缀（别跟 LiveKit 自己的 lk.* 撞）",
      all(n.startswith("cc.") for n in methods), sorted(methods))

print("\n── 三个直通的控制 ──")
for name in ("pause", "resume", "stop"):
    methods, pb = build()
    pb.ret = True
    r = call(methods, name)
    check(f"{name} 转发到播放器并回 ok=true", r == {"ok": True} and pb.calls == [name], f"{r} {pb.calls}")
    methods, pb = build()
    pb.ret = False
    r = call(methods, name)
    # ⭐ 播放器说没做成就要如实回 false。静默回 true 的话，app 上按钮会
    #    变成「按了像是生效了」，而声音照旧 —— 那是最难查的一类 UI 撒谎。
    check(f"⭐ {name} 播放器返回 false 时不粉饰", r == {"ok": False}, str(r))

print("\n── replay：不带 fid 要能自己找到当前段 ──")
methods, pb = build()
pb.prog = (10.0, 70.0, True, "abc123")
r = call(methods, "replay")
check("⭐ 不带 fid 时自动取当前在播的段", r["ok"] and pb.replayed == ["abc123"],
      f"{r} {pb.replayed}")

methods, pb = build()
pb.prog = (10.0, 70.0, True, "abc123")
r = call(methods, "replay", {"fid": "other"})
check("显式给 fid 时用给的那个", pb.replayed == ["other"], str(pb.replayed))

methods, pb = build()
pb.prog = None
r = call(methods, "replay")
check("⭐ 没有可重播的段时报错，不去调 replay(空)",
      r["ok"] is False and "replay" not in pb.calls, f"{r} {pb.calls}")

methods, pb = build()
pb.prog = (1.0, 2.0, True, "x")
r = call(methods, "replay", "这不是 json")   # payload 是坏的
check("payload 坏掉也不炸，回落到当前段", r["ok"] and pb.replayed == ["x"], str(r))

print("\n── seek：范围必须夹住 ──")
for d, should in ((0.1, True), (-0.1, True), (1.0, True), (-1.0, True),
                  (1.5, False), (-1.5, False), (99, False)):
    methods, pb = build()
    r = call(methods, "seek", {"delta": d})
    passed = (r["ok"] is True) if should else (r["ok"] is False and "seek" not in str(pb.calls))
    check(f"delta={d} → {'放行' if should else '拒绝且不调播放器'}", passed, str(r))

methods, pb = build()
r = call(methods, "seek", {"delta": "abc"})
check("⭐ delta 不是数字时拒绝而不是当 0", r["ok"] is False and not pb.calls, f"{r} {pb.calls}")

print("\n── progress：绝不编造总长 ──")
methods, pb = build()
pb.prog = None
r = call(methods, "progress")
check("没在播时 active=false", r == {"ok": True, "active": False}, str(r))

methods, pb = build()
pb.prog = (15.0, 0.0, True, "fid1")      # 还在生成，总长未知
r = call(methods, "progress")
# ⭐ 这一条直接对应 2026-09-17 那次事故：卡片显示 "15/16s" 看着像播完了，
#    用户按了重播，连锁触发 replay 冻结直播段，整条回复只剩前 16 秒。
#    分母是假的就必须传 null，让客户端显示「生成中」。
check("⭐ 总长未知时 total 传 null，不编分母",
      r["total"] is None and r["played"] == 15.0, str(r))

methods, pb = build()
pb.prog = (15.0, 141.0, True, "fid1")
r = call(methods, "progress")
check("总长已知时如实传", r["total"] == 141.0 and r["fid"] == "fid1", str(r))

methods, pb = build()
pb.prog = (141.0, 141.0, False, "fid1")
r = call(methods, "progress")
check("播完了 active=false 但仍给得出 fid（供重播）",
      r["active"] is False and r["fid"] == "fid1", str(r))

print("\n── token 必须放开 data，否则 RPC 回不去 ──")
# RPC 架在数据通道上，响应也是一次 data publish。can_publish_data=False 的话
# 现象是「客户端一直等到超时」而服务端毫无动静 —— 看着像没注册上。
check("⭐ can_publish_data=True", re.search(r"can_publish_data=True", src) is not None)
check("can_subscribe 仍然是 False（「只说不听」没破）",
      re.search(r"can_subscribe=False", src) is not None)

print(f"\n{'=' * 52}\n通过 {ok} 条，失败 {fail} 条")
sys.exit(1 if fail else 0)
