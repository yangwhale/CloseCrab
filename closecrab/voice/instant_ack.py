"""Instant ack 词库 + 选择器 — 独立模块，不依赖 livekit。

Zello (子线程) 和 livekit_io (主线程) 都 import 这里，
避免 Zello import livekit_io 触发 silero 主线程注册限制。
"""

import random

_GREETINGS = [
    "嗨！你好你好！", "哟！来了来了！", "嘿，在呢在呢，说吧！",
    "诶！又见面啦！", "Roger roger，我在线！",
    "收到收到，Jarvis 已上线！", "哟呵，今天精神不错啊！",
    "Copy that，说吧老板！",
]
_GENERIC = [
    "Roger，收到，让我想想！", "Copy that，马上整！",
    "收到收到，这就安排！", "10-4，容我三秒！",
    "Roger that，有点东西啊！", "Copy，等我查查看！",
    "收到，这个我知道，稍等！", "Roger，来活了来活了！",
    "收到收到，搁这等着！", "Copy that，冲了冲了！",
    "10-4，让我捋一捋！", "Roger，脑子已经转起来了！",
    "收到，大脑加载中！", "Copy copy，马上出活！",
    "Roger that，问对人了！", "收到，这题我会！",
    "10-4 老板，安排上了！", "Roger，整起来整起来！",
]
_JOKE = [
    "Roger，我的快乐回来了！", "Copy that，你说我听着！",
    "10-4 老板，遵命遵命！", "Roger，随叫随到！",
    "Copy that，你可算来了！", "Roger roger，来活了，兴奋！",
    "收到收到，不嘻嘻，认真给你整！",
    "10-4，Jarvis 待命中，说吧！",
]
_LONG = [
    "Roger，这个得想想，你等一下下！",
    "Copy that，让我翻翻资料！",
    "收到收到，有点复杂，我捋一捋！",
    "Roger，这个烧脑，仔细想想再回你！",
    "10-4，内容有点多，整理一下！",
    "Copy that，含金量太高了，好好想想！",
    "Roger，这题硬控我了，等我缓缓！",
    "收到，让我翻翻 wiki 再回你！",
]

_GREETING_TRIGGERS = frozenset((
    "嗨", "hi", "hey", "嘿", "你好", "早", "早上好", "晚上好",
    "嗨嗨", "嗨嗨嗨", "hello", "哈喽",
))


def pick_instant_ack(user_text: str = "") -> str:
    """根据用户输入选一句即时应答。空文本 = 通用 ack (PTT 松手时 STT 还没出来)。"""
    t = user_text.strip().lower()
    if not t:
        return random.choice(_GENERIC)
    if t in _GREETING_TRIGGERS:
        return random.choice(_GREETINGS)
    if len(t) > 30:
        return random.choice(_LONG)
    return random.choice(_GENERIC + _JOKE)
