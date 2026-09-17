#!/usr/bin/env python3
"""`[whispers]` 不许进 TTS —— 2026-09-17 Chris 报的「声特别小听不见」。

## 为什么不是改个 prompt 就完事

`[whispers]` 是 Gemini 官方标签，模型**会老老实实照做**，照做的结果就是
音量小到手机外放/车里/地铁上听不见。而它失效的样子是**静默的**：
不报错，只是那一段用户没听到。

光从 system prompt 的标签清单里删掉只能降低概率 —— 这个标签同时躺在
Gemini 官方文档、模型训练分布、和本仓库一堆历史台词里。
跟 `channels/web.py` 的 `sanitize_outbound()` 同一个判断：**prompt 压不住 prompt**，
要在出口做确定性替换。

## 三条通往 TTS 的路，一条都不能漏

    gemini     → _clean_text_for_tts → normalize_tts_tags  ✅
    cloud_tts  → _clean_text_for_tts → normalize_tts_tags  ✅
    qwen3      → _do_speak 直接拿原始 text 喂 _split_by_emotion
                 ⚠️ **不经过 _clean_text_for_tts**，所以 _do_speak 开头单独挂一道

漏掉 qwen3 那条的话，表现是「换了后端又开始小声」—— 而没人会想到去查标签。

跑法：`python3 test_tts_inaudible_tag.py`（只 import gemini_tts，不拖 discord）
"""
import io
import re
import sys

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ✅", name)
    else:
        fail += 1
        print("  ❌", name, f"— {detail}" if detail else "")


# ── 把 normalize_tts_tags 抠出来（不 import 整个模块，它拖 google-genai）──
SRC_GT = "closecrab/voice/gemini_tts.py"
gsrc = io.open(SRC_GT, encoding="utf-8").read()
ns: dict = {"re": re}
for _name in ("_RE_INAUDIBLE_TAG", "_AUDIBLE_REPLACEMENT"):
    _m = re.search(rf"^{_name} = (.+)$", gsrc, re.M)
    assert _m, f"{_name} 不见了"
    exec(f"{_name} = {_m.group(1).strip()}", ns)
_m = re.search(r"^def normalize_tts_tags\(.*?\n(?=\S)", gsrc, re.M | re.S)
assert _m, "normalize_tts_tags 不见了"
exec(_m.group(0), ns)
norm = ns["normalize_tts_tags"]
REPL = ns["_AUDIBLE_REPLACEMENT"]

print("\n── 替换本身 ──")
check("基本替换", norm("[whispers] 悄悄说") == f"{REPL} 悄悄说")
check("大小写不敏感", "whisper" not in norm("[Whispers] 你好").lower())
check("单数写法也认（[whisper]）", "whisper" not in norm("[whisper] 你好").lower())
check("全大写也认", "whisper" not in norm("[WHISPERS] 你好").lower())

multi = norm("[casually] 一。[whispers] 二。[amused] 三。[whispers] 四。")
check("一段里多处全换", "whisper" not in multi.lower(), multi)
check("多处替换后其余标签原样",
      multi.count("[casually]") == 3 and "[amused]" in multi, multi)

check("不碰其他标签", norm("[casually] 你好") == "[casually] 你好")
check("没有标签时原样", norm("就一句话") == "就一句话")
check("空串不炸", norm("") == "")
# 文本里出现「whispers」这个词但不是标签形式 → 不该动
check("不误伤正文里的 whispers 一词",
      norm("这个 API 叫 whispers，不是标签") == "这个 API 叫 whispers，不是标签")

check("⭐ 替换成的标签不能又是个听不见的",
      "whisper" not in REPL.lower(), REPL)

print("\n── 三条后端路径都挂上了 ──")

check("⭐ _clean_text_for_tts 第一步就 normalize",
      re.search(r"def _clean_text_for_tts\(text: str\) -> str:\s*\n\s*\"\"\".*?\"\"\"\s*\n"
                r"\s*text = normalize_tts_tags\(text\)", gsrc, re.S) is not None,
      "gemini / cloud_tts 两条路靠它")

SRC_DVS = "closecrab/voice/discord_voice_sidecar.py"
dsrc = io.open(SRC_DVS, encoding="utf-8").read()
_m = re.search(r"async def _do_speak\(.*?\n(?=\S)", dsrc, re.S)
check("找得到 _do_speak", _m is not None)
if _m:
    body = _m.group(0)
    check("⭐ _do_speak 里也 normalize 了（qwen3 那条路不走 cleaner）",
          "normalize_tts_tags(text)" in body,
          "漏了这一处 = 换成 qwen3 后端又开始小声")
    # 必须在喂给 _split_by_emotion **之前**
    if "normalize_tts_tags(text)" in body and "_split_by_emotion(text)" in body:
        check("⭐ normalize 发生在 _split_by_emotion 之前",
              body.index("normalize_tts_tags(text)") < body.index("_split_by_emotion(text)"),
              "顺序反了 = 标签已经被切走了才替换，等于没做")

print("\n── 不该再有人主动产出它 ──")

msrc = io.open("closecrab/main.py", encoding="utf-8").read()
# 语音指令里那份「常用标签」清单不能再把它列为可用
_m = re.search(r"每 1-3 句切换情感标签.*?技术术语保留英文", msrc, re.S)
check("找得到语音标签清单段", _m is not None)
if _m:
    seg = _m.group(0)
    # 允许出现在「不要用」的警告里，但不能出现在推荐行上
    rec_lines = [l for l in seg.split("\\n")
                 if "- 思考:" in l or "- 友好:" in l or "- 兴奋:" in l or "- 建议/特效:" in l]
    check("⭐ 推荐清单里不再有 [whispers]",
          all("whispers" not in l for l in rec_lines),
          [l for l in rec_lines if "whispers" in l])
    check("清单里留了一句明确的「不要用」", "不要用 `[whispers]`" in seg)

lsrc = io.open("closecrab/voice/livekit_io.py", encoding="utf-8").read()
check("⭐ 广播开场白里没有硬编码的 [whispers]",
      "[whispers]" not in lsrc,
      [l for l in lsrc.split("\n") if "[whispers]" in l])

check("⭐ Qwen3 指令表里那条「音量: 较小」已删",
      not re.search(r'^\s*"whispers":', dsrc, re.M),
      "留着就是个把音量调小的陷阱")

ssrc = io.open("skills/tts-generator/SKILL.md", encoding="utf-8").read()
_m = re.search(r"^\*\*特效类\*\*：(.+)$", ssrc, re.M)
check("skill 的特效类清单里也去掉了",
      _m is not None and "whispers" not in _m.group(1), _m.group(1) if _m else None)

print(f"\n{'='*52}\n通过 {ok} 条，失败 {fail} 条")
sys.exit(1 if fail else 0)
