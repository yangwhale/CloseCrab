"""Model ID → 卡片上显示的短名。

⭐ 四个 channel（feishu / discord / dingtalk / web）都要显示当前模型，
   这份映射只能有一份 —— 2026-09-24 之前它私有在 feishu.py 里，
   于是只有飞书的**回复卡片**走了映射，`/status` 卡片和另外三个 channel
   全在打印 `claude-opus-5-5[1m]@default` 这种 30 字符的原始 ID。
⛔ 判据：**一个格式化函数只被一处调用、而同样的值在别处也要显示**，
   那不是「还没用上」，是已经漂了。
"""

import re

def shorten_model_name(raw: str) -> str:
    """将原始 model ID 转为简短显示名 (chris 的简写规则: G/C/O + 版本数 + 后缀)."""
    if not raw:
        return ""
    name = raw.rsplit("/", 1)[-1] if "/" in raw else raw
    name = name.split("@")[0]
    # 剥掉 [1m] 长上下文标记 (CC 内部后门, 不该出现在 user-facing 显示)
    name = name.replace("[1m]", "")
    _MAP = {
        # Claude
        "claude-opus-4-6": "Opus 4.6",
        "claude-opus-4-7": "Opus 4.7",
        "claude-opus-4-8": "Opus 4.8",
        "claude-opus-5": "Opus 5.0",
        "claude-opus-5-5": "Opus 5.5",
        "claude-sonnet-4-6": "Sonnet 4.6",
        "claude-sonnet-4-5": "Sonnet 4.5",
        "claude-haiku-4-5": "Haiku 4.5",
        "claude-sonnet-5": "Sonnet 5.0",
        # Gemini
        "gemini-3.8-flash": "G38F",
        "gemini-3.7-flash": "G37F",
        "gemini-3.7-pro": "G37P",
        "gemini-3.6-flash": "G36F",
        "gemini-3.5-flash": "G35F",
        "gemini-3.1-pro": "G31P",
        "gemini-3.1-flash": "G31F",
        "gemini-3.1-flash-lite": "G31FL",
        "gemini-3-flash": "G3F",
        "gemini-3-pro": "G3P",
        "gemini-2.5-pro": "G25P",
        "gemini-2.5-flash": "G25F",
        "gemini-2.5-flash-lite": "G25FL",
    }
    hit = _MAP.get(name)
    if hit:
        return hit
    # Fall back by normalizing the decorations providers keep adding, so a model
    # the table has not caught up with still renders short instead of dumping a
    # 30-character id onto the card. gemini-3.7-flash arrived as
    # "litellm/gemini-3.7-flash" and showed raw for exactly this reason.
    suffix = ""
    base = name
    if base.endswith("-preview"):
        base, suffix = base[: -len("-preview")], "-prev"
    base = re.sub(r"-\d{8}$", "", base)          # dated pins: -20251001
    hit = _MAP.get(base)
    return (hit + suffix) if hit else name
