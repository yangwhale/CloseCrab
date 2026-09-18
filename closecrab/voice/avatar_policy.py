"""数字人判定的**转发层** —— 真身在 `closecrab-avatar/closecrab_avatar/policy.py`。

## 为什么这里不再有逻辑

2026-09-18 之前这个文件**就是**契约本身，产品仓库那份是从这儿搬过去的。
搬完之后两份并存了几个小时，产品那份加了「每角色一个开关」，这份没加 ——
于是同一个开关在两个进程里能算出不同的答案。这正是契约文件开头那句
「别在别处复刻」要防的事，而复刻者是我自己。

现在三方都照着产品仓库那一份实现：

    iOS            VoiceAgent/CloseCrab/CCAvatarRoles.swift
    本进程          本文件转发过去
    语音助手进程     ~/lk-gemini-agent/cc_avatar.py

这里**只转发，不再有自己的判断**。

## ⚠️ 找不到产品仓库时直接抛，不给降级默认值

判定错了是**静默**的：该开没开，用户以为坏了；不该开却开了，白占一路 GPU
而屏幕上根本看不见。而「这台机器装没装 closecrab-avatar」是个部署事实，
该在部署时暴露，不该用一个兜底的 OFF 把它盖住。

调用方 `avatar_link` 会接住这个 ImportError 并把数字人功能整体关掉，
语音本身不受影响 —— 那是**正确的降级**：没装产品仓库的机器本来也没有网关。
"""

from __future__ import annotations

import os
import sys

_REPO = os.environ.get("CC_AVATAR_REPO", os.path.expanduser("~/closecrab-avatar"))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# ⚠️ 逐个显式转发，**不用 `import *`**：上游哪天改了名字，这里立刻 ImportError，
#    而 `import *` 只会让符号悄悄消失，报错点落在几百行之外的调用处。
from closecrab_avatar.audio_sink import AvatarAudioSink  # noqa: E402
from closecrab_avatar.policy import (  # noqa: E402  (sys.path 必须先垫好)
    ALLOC_PRIORITY,
    ATTR_STATE,
    ATTR_STATE_BY_ROLE,
    ATTR_VISIBLE,
    ATTR_WANT,
    ATTR_WANT_BY_ROLE,
    AvatarRole,
    AvatarState,
    allocate,
    any_visible,
    avatar_identity,
    decide,
    decide_for_room,
    decide_from_attributes,
    is_user_visible_problem,
    parse_flag,
    role_of_identity,
    should_generate,
    wanted_roles,
)

__all__ = [
    "ALLOC_PRIORITY",
    "AvatarAudioSink",
    "ATTR_STATE",
    "ATTR_STATE_BY_ROLE",
    "ATTR_VISIBLE",
    "ATTR_WANT",
    "ATTR_WANT_BY_ROLE",
    "AvatarRole",
    "AvatarState",
    "allocate",
    "any_visible",
    "avatar_identity",
    "decide",
    "decide_for_room",
    "decide_from_attributes",
    "is_user_visible_problem",
    "parse_flag",
    "role_of_identity",
    "should_generate",
    "wanted_roles",
]
