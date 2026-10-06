"""bot 在回复里「举手」的隐藏标记：`<ask-user/>` / `<ask-user>一句摘要</ask-user>`。

## 为什么要有

bot 状态里的「等你回话」（`AgentState.waiting_for` → 快照里的 `wait`）原来只在
ExitPlanMode / AskUserQuestion / 权限请求时置位。但最常见的那种 —— 普通回复末尾
问一句「你看呢？」—— 系统感知不到：iOS 实时活动不提醒、不出快捷回复。
Chris 2026-10-05 批准：让 bot 自己在回复里带隐藏标记，由 BotCore 统一剥掉并置位。

## 只有一份正则

剥离点不止一个（BotCore 的统一出口、后台回复回调、voice 的标签清洗），
**全部调这里**，不各写一份 —— 各写一份迟早有一处漏了「大小写」或「带空格的自闭合」，
用户就会在飞书里看到一个裸标签、或者听见 TTS 念出 "ask user"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["ASK_USER_DEFAULT", "ASK_USER_SUMMARY_MAX", "ASK_USER_OPTION_MAX", "ASK_USER_OPTIONS_MAX",
           "AskUser", "parse_ask_user", "extract_ask_user", "strip_ask_user"]

#: 标记没带摘要时，状态里写的那句。
ASK_USER_DEFAULT = "等你决定"
#: 摘要最多几个字符（超了截断，末尾一个「…」占一位）。要塞进参与者属性和锁屏一行。
ASK_USER_SUMMARY_MAX = 40
#: 推荐答案最多几个（app 主界面和锁屏卡片都只放得下两颗按钮）。
ASK_USER_OPTIONS_MAX = 2
#: 每个推荐答案最多几个字符。点下去**原样发给 bot**，所以截断的那句也就是用户的回答 ——
#: 写长了 bot 自己吃亏，提示词里要求写短。
ASK_USER_OPTION_MAX = 24

# 成对的：<ask-user>摘要</ask-user>（摘要可空、可跨行）
_PAIRED = re.compile(r"<\s*ask-user\s*>(.*?)<\s*/\s*ask-user\s*>", re.IGNORECASE | re.DOTALL)
# 落单的：<ask-user/>、<ask-user />、以及没闭合的 <ask-user> / 多出来的 </ask-user>
_SINGLE = re.compile(r"<\s*/?\s*ask-user\s*/?\s*>", re.IGNORECASE)


def _clip(s: str, n: int = ASK_USER_SUMMARY_MAX) -> str:
    s = " ".join(s.split())          # 跨行 / 多空格压成一个空格
    if len(s) > n:
        s = s[:n - 1] + "…"
    return s


@dataclass
class AskUser:
    """一次举手：剥干净的文本、摘要、推荐答案。"""
    text: str
    #: None ⇒ 没举手（**不置位**）
    summary: str | None
    #: bot 推荐的答案，0~2 个。点了就把这句原样发回给 bot。
    options: list[str] = field(default_factory=list)


def parse_ask_user(text: str) -> AskUser:
    """剥掉所有 ask-user 标记，取出摘要和推荐答案。

    标记写法：`<ask-user>摘要|答案一|答案二</ask-user>` —— 竖线分隔，答案可省。
    Chris 2026-10-06：「除了带问题，还要带推荐的答案，给两个最推荐的，推到屏幕上去选。」

    - 没有标记 ⇒ summary 是 None
    - 有标记 ⇒ 取第一个非空的那个标记：摘要截到 40 字符，都没写就是「等你决定」；
      答案去空、去重、最多 2 个、每个截到 24 字符
    - 摘要为空但带了答案（`<ask-user>|好|不好</ask-user>`）⇒ 摘要用默认那句，答案照取
    - 标记剥掉后留下的行尾空白 / 多余空行顺手收掉，文本首尾 strip
    """
    if not text:
        return AskUser(text, None)
    bodies: list[str] = []
    found = False

    def _take(m: re.Match) -> str:
        nonlocal found
        found = True
        s = m.group(1).strip()
        if s.strip("|").strip():
            bodies.append(s)
        return ""

    out = _PAIRED.sub(_take, text)
    out, n = _SINGLE.subn("", out)
    if n:
        found = True
    if not found:
        return AskUser(text, None)
    # 标记通常独占末尾一行：剥掉后别留一串空行 / 行尾空格
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    summary, options = "", []
    if bodies:
        head, *rest = [p.strip() for p in bodies[0].split("|")]
        summary = _clip(head)
        for o in rest:
            o = _clip(o, ASK_USER_OPTION_MAX)
            if o and o not in options:
                options.append(o)
        options = options[:ASK_USER_OPTIONS_MAX]
    return AskUser(out, summary or ASK_USER_DEFAULT, options)


def extract_ask_user(text: str) -> tuple[str, str | None]:
    """只要 `(干净的文本, 摘要)` 的旧接口。推荐答案见 `parse_ask_user`。"""
    a = parse_ask_user(text)
    return a.text, a.summary


def strip_ask_user(text: str) -> str:
    """只剥不取（给 voice 清洗、后台回复这类不关心摘要的地方）。"""
    return parse_ask_user(text).text
