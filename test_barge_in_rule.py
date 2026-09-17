#!/usr/bin/env python3
"""barge-in 的触发条件 —— 2026-09-17 现场报的 bug 的回归。

⛔ 原来的判据是「私聊里真人发了消息」，**打字也被当成抢麦**：
   上一轮回复念到一半，用户在飞书敲一行字，那段语音就被当场掐断。
   现场原话：「这东西得排队输出，你不能把上一个正在输出的突然给停。」

⭐⭐⭐ 判据：**barge-in 的条件是「同一个通道被抢占」，不是「有新输入」。**
   用嘴抢麦 → 停；用手打字 → 排队。

⚠️ 这份测试**不导入整个 feishu 模块**（它拖 lark SDK），
   直接从源码里把那个纯函数抠出来 exec —— 它没有任何外部依赖。
"""
import io, re, sys, types

SRC = "closecrab/channels/feishu.py"
src = io.open(SRC, encoding="utf-8").read()
m = re.search(r'^def should_barge_in\(.*?(?=\n\S)', src, re.S | re.M)
assert m, "should_barge_in 不见了 —— 是不是被改名或内联回去了？"
ns: dict = {}
exec(m.group(0), ns)
should_barge_in = ns["should_barge_in"]

ok = fail = 0
def check(name, cond):
    global ok, fail
    if cond:
        ok += 1; print("  ✅", name)
    else:
        fail += 1; print("  ❌", name)


def _item(mt):
    msg = types.SimpleNamespace(message_type=mt)
    return types.SimpleNamespace(event=types.SimpleNamespace(message=msg))


print("── barge-in 触发条件 ──")
check("用户在私聊里**开口**（audio）→ 停播",
      should_barge_in("user", "p2p", "audio") is True)
check("⭐ 用户在私聊里**打字**（text）→ 不停，排队",
      should_barge_in("user", "p2p", "text") is False)
check("发图 / 发文件也不算抢麦",
      should_barge_in("user", "p2p", "image") is False
      and should_barge_in("user", "p2p", "file") is False)
check("富文本（post）同样不算",
      should_barge_in("user", "p2p", "post") is False)
check("群里有人开口 → 不掐正念给本人听的内容",
      should_barge_in("user", "group", "audio") is False)
check("机器人发的消息 → 永远不触发",
      should_barge_in("app", "p2p", "audio") is False)
check("合并消息里**只要有一条语音**就算开口",
      should_barge_in("user", "p2p", "text",
                      [_item("image"), _item("audio")]) is True)
check("合并消息里一条语音都没有 → 不算",
      should_barge_in("user", "p2p", "text",
                      [_item("image"), _item("text")]) is False)
check("merged_items 为 None 不炸",
      should_barge_in("user", "p2p", "text", None) is False)
check("merged_items 里有畸形项（没有 event）不炸",
      should_barge_in("user", "p2p", "text",
                      [types.SimpleNamespace()]) is False)

print("\n%d/%d 通过" % (ok, ok + fail))
sys.exit(1 if fail else 0)
