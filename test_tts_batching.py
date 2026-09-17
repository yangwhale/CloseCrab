#!/usr/bin/env python3
"""TTS 分批 / 直播重播 / 进度显示 —— 2026-09-17 那条播报丢了 125 秒的回归。

## 那天发生了什么（三个 bug 串成一条链）

一条 827 字的语音播报，用户只听到前 16 秒，后面全没了。日志复盘：

    TTS 分批: 827c → 6 批 (首批 27c)
    批 #1  27c  1780ms → 3.5s 音频
    批 #2  27c  1945ms → 3.6s
    批 #3  45c  3108ms → 8.8s          ← 到这里一共 15.9s
    批 #4 299c 13689ms → 53.6s         ← 生成要 13.7s，而余量只有 ~10.9s
    ...
    [voice-progress] patch note='15/16s'          ← 看着像播完了
    Card action: voice_replay                     ← 用户按了重播
    [voice-progress] 播放结束退出 15.9/15.9199375  ← 整条只剩 16 秒

1. **分批一步从 45 跳到 299** → 生成追不上播放，第 16 秒起卡住干等。
2. **进度条拿「已落盘」当总长** → 显示 `15/16s`，看着就是播完了。
3. **`replay()` 无条件把目标重建成定长轨** → 写句柄被关、`live` 翻假，
   后续 `feed()` 第一行就 return，**剩下的音频连磁盘都没写进去**。

三条单独看都像小毛病，串起来是「整段回复静默消失，日志无一行报错」。

⚠️ 这份测试不导入 `discord_voice_sidecar` / `player`（它们拖 discord、genai
   和一条播放线程），直接从源码抠纯函数 exec —— 被测的都没有外部依赖。
"""
import io
import os
import re
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Callable

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ✅", name)
    else:
        fail += 1
        print("  ❌", name, f"— {detail}" if detail else "")


# ── 把分批那几个纯函数抠出来 ──────────────────────────────────────────
SRC_DVS = "closecrab/voice/discord_voice_sidecar.py"
src = io.open(SRC_DVS, encoding="utf-8").read()
ns: dict = {"re": re}
for _name in ("_SENT_SPLIT_RE", "_SOLO_UNTIL_CHARS", "_MAX_BATCH_CHARS",
              "_MIN_BATCH_CHARS", "_GEN_FIXED_S", "_GEN_PER_CHAR_S",
              "_AUDIO_PER_CHAR_S", "_LEAD_SAFETY"):
    _m = re.search(rf"^{_name} = ([^\n#]+)", src, re.M)
    assert _m, f"{_name} 不见了 —— 是不是被改名了？"
    exec(f"{_name} = {_m.group(1).strip()}", ns)
for _fn in ("_batch_cap_for_lead", "_plan_tts_batches"):
    _m = re.search(rf"^def {_fn}\(.*?\n(?=\S)", src, re.M | re.S)
    assert _m, f"{_fn} 不见了"
    exec(_m.group(0), ns)

plan = ns["_plan_tts_batches"]
cap_for = ns["_batch_cap_for_lead"]
GEN_FIXED, GEN_PC = ns["_GEN_FIXED_S"], ns["_GEN_PER_CHAR_S"]
AUDIO_PC, MAXB, MINB = ns["_AUDIO_PER_CHAR_S"], ns["_MAX_BATCH_CHARS"], ns["_MIN_BATCH_CHARS"]


def underrun_seconds(sizes):
    """按经验模型模拟播放，返回累计欠载秒数。

    这就是那天出事的判据本身：生成第 k+1 批耗时若超过当前余量，中间那段
    时间播放器没东西可放 —— 用户听到的是「说着说着没声了」。
    """
    lead = total_under = 0.0
    for i, n in enumerate(sizes):
        gen, audio = GEN_FIXED + n * GEN_PC, n * AUDIO_PC
        if i == 0:
            lead = audio          # 第一批生成完才起播，不消耗余量
            continue
        if gen > lead:
            total_under += gen - lead
        lead = max(0.0, lead - gen) + audio
    return total_under


print("\n── 分批：真实事故文本不再欠载 ──")

# 那天那条播报的形状：一段情绪标签开头的短句 + 一串长段落。
REAL = (
    "[cheerfully] 四条全修完了，已经提交推送。\n\n"
    "[thinking] 最值钱的收获是我差点写反的那条。查完 Apple 文档才发现，"
    "自定义字体那条路是会跟系统字号缩放的，反而系统字体那条不跟。\n\n"
    "[realization] 所以那个手写体开关，其实偷偷改变了整个界面跟不跟用户的字号设置。"
    "关着的时候用户把字调大我们纹丝不动，开着的时候字会长但方块是写死的五十四点，"
    "名字直接被截掉。两头都错，方向还相反。\n\n"
    "[casually] 现在两条路统一关掉字体自带的缩放，改成方块和字用同一个系数一起长。"
    "方块行本来就能横滑，长出去可以滚，所以不用卡上限。\n\n"
    "[focus] 减弱动态效果那条，原来十二个文件里只有极光一个真做了。"
    "我加了一层统一拦截，替换了二十个调用点。\n\n"
    "[contemplative] 这里有个讲究，不能一刀切全关。那个开关针对的是头晕，"
    "淡入淡出不在其列，直接不动反而会让切换变成硬跳，看着像卡了。"
    "所以分成两档，状态切换退化成淡出，纯装饰的转圈流光才彻底停。\n\n"
    "[amused] 转圈那个还得单独处理，停下来看着像死机，我给它换成了呼吸。"
    "音量柱保留，因为柱子的抖动本身就是内容，删了等于把信息删了。\n\n"
    "[seriously] VoiceOver 那条问题最实在。双击静音和长按换图标这两个手势被"
    "VoiceOver 自己接管了，根本传不到我们代码里。也就是说这两个功能对他们是缺失的，"
    "而界面上看不出任何异样。补了两个命名动作。\n\n"
    "[thinking] 验证做了三层。四十九个文件语法全过；给新加的动画修饰符建了个"
    "最小类型桩做真类型检查；判定逻辑抽成纯函数，五十六条测试，变异全杀。"
)

sizes = [len(b) for b in plan(REAL)]
u_new = underrun_seconds(sizes)
u_old = underrun_seconds([27, 27, 45, 299, 290, 119])   # 日志里实际那次
print(f"     新分批 {sizes}")
check("真实事故文本零欠载", u_new == 0, f"欠载 {u_new:.1f}s")
check("旧分批在同一模型下确实会欠载（说明这个判据有分辨力）", u_old > 1,
      f"旧只欠载 {u_old:.1f}s，判据可能失效")

print("\n── 分批：不变量 ──")

check("不丢字", "".join(plan(REAL)) == "".join(
    s for s in ns["_SENT_SPLIT_RE"].split(REAL) if s and s.strip()))
check("没有空批", all(b.strip() for b in plan(REAL)))
check("首批足够小（首字延迟）", len(plan(REAL)[0]) <= ns["_SOLO_UNTIL_CHARS"] + 20,
      f"首批 {len(plan(REAL)[0])}c")

# 长度递增：余量滚雪球，批就该越来越大（最后一批是收尾，不参与）
mid = sizes[:-1]
check("批长单调不减（余量滚雪球）", all(a <= b for a, b in zip(mid, mid[1:])),
      f"{mid}")

print("\n── 分批：边界输入 ──")
check("空串 → 空列表", plan("") == [])
check("纯空白 → 空列表", plan("   \n  ") == [])
check("单句不切", len(plan("就一句话。")) == 1)
long_one = "啊" * 900 + "。"      # 一个句子就超上限：不能死循环，也不能丢
out = plan(long_one)
check("超长单句不死循环且不丢字", "".join(out) == long_one, f"{[len(b) for b in out]}")
no_punct = "没有任何句末标点的一长串文字" * 40
check("完全没有标点也能出批", len(plan(no_punct)) >= 1)

print("\n── cap：夹在上下限之间 ──")
check("余量为 0 时取下限（不切碎）", cap_for(0.0) == MINB)
check("余量为负也取下限", cap_for(-5.0) == MINB)
check("余量极大时封顶", cap_for(10_000.0) == MAXB)
check("cap 对余量单调不减", all(
    cap_for(x) <= cap_for(x + 1) for x in range(0, 200, 7)))

# ── replay：正在生成的那一段只能倒带 ────────────────────────────────
print("\n── replay：直播段倒带而不是冻结 ──")

SRC_PL = "closecrab/voice/player.py"
psrc = io.open(SRC_PL, encoding="utf-8").read()

_m = re.search(r"^@dataclass\nclass _Track:.*?\n(?=\n*(?:@|class |def ))", psrc, re.M | re.S)
assert _m, "_Track 不见了"
pns: dict = {"dataclass": dataclass, "field": field, "threading": threading,
             "Callable": Callable, "os": os}
exec("from dataclasses import dataclass, field\nimport threading, os\n" + _m.group(0), pns)

_m = re.search(r"^    def replay\(self, fid: str\) -> bool:.*?\n(?=    def )", psrc, re.M | re.S)
assert _m, "replay 不见了"

PLAYING, IDLE = "playing", "idle"


class _FakePlayer:
    """只搭 replay 摸得到的那几个成员。"""

    def __init__(self, track, tmpdir):
        self._track = track
        self._lock = threading.RLock()
        self._state = PLAYING
        self._tmpdir = tmpdir
        self.closed_handles = False
        self.reported = 0

    def _buf_path(self, fid):
        return os.path.join(self._tmpdir, f"{fid}.pcm")

    def _close_handles(self):
        self.closed_handles = True
        t = self._track
        for h in (t.fh, t._w):
            if h is not None:
                h.close()
        t.fh = t._w = None

    def _report(self):
        self.reported += 1


_log_stub = type("L", (), {"info": staticmethod(lambda *a, **k: None),
                           "warning": staticmethod(lambda *a, **k: None)})()
exec("class _P:\n" + _m.group(0),
     {"_Track": pns["_Track"], "os": os, "log": _log_stub,
      "PLAYING": PLAYING, "IDLE": IDLE}, pns)
_FakePlayer.replay = pns["_P"].replay

with tempfile.TemporaryDirectory() as td:
    fid = "abc123"
    path = os.path.join(td, f"{fid}.pcm")
    with open(path, "wb") as f:
        f.write(b"\x01" * 4000)

    # 场景一：重播**正在生成**的这一段
    w = open(path, "r+b")
    w.seek(0, os.SEEK_END)
    r = open(path, "rb")
    r.read(2000)                      # 已经播了一半
    live = pns["_Track"](fid=fid, path=path, pos=2000, live=True, total=4000, fh=r, _w=w)
    p = _FakePlayer(live, td)
    res = p.replay(fid)

    check("直播段重播返回 True", res is True)
    check("⭐ live 保持为真（不冻结）", p._track.live is True,
          "翻成 False 后续 feed() 会静默丢弃全部音频")
    check("⭐ 写句柄还活着", p._track._w is not None and not p._track._w.closed,
          "写句柄被关 → 剩下的音频连磁盘都写不进去")
    check("⭐ 没有走 _close_handles", p.closed_handles is False)
    check("位置倒回开头", p._track.pos == 0)
    check("读句柄也倒回开头", p._track.fh.tell() == 0)
    check("total 未被文件当前大小覆盖", p._track.total == 4000)
    check("上报了一次进度", p.reported == 1)
    w.close(); r.close()

    # 场景二：重播一段**已经生成完**的旧录音 —— 旧行为必须原样保留
    old = pns["_Track"](fid="other", path=os.path.join(td, "other.pcm"),
                        live=False, total=0, fh=None, _w=None)
    with open(os.path.join(td, "other.pcm"), "wb") as f:
        f.write(b"\x02" * 800)
    p2 = _FakePlayer(old, td)
    check("定长段重播返回 True", p2.replay(fid) is True)
    check("定长段重建为非 live", p2._track.live is False)
    check("定长段 total 取文件大小", p2._track.total == 4000)
    check("定长段位置归零", p2._track.pos == 0)

    # 场景三：文件不存在
    p3 = _FakePlayer(pns["_Track"](), td)
    check("不存在的 fid 返回 False", p3.replay("nope") is False)

# ── 进度显示：总长未知时不许编分母 ──────────────────────────────────
print("\n── 进度文案：生成中不显示假分母 ──")

fsrc = io.open("closecrab/channels/feishu.py", encoding="utf-8").read()
_m = re.search(r"^    def _fmt_progress_note\(.*?\n(?=    (?:@|def |async def ))",
               fsrc, re.M | re.S)
assert _m, "_fmt_progress_note 不见了"
fns: dict = {}
exec("class _C:\n" + _m.group(0).replace("    @staticmethod\n", ""), fns)
fmt = fns["_C"]._fmt_progress_note
if not callable(fmt):
    fmt = fmt.__func__

check("⭐ 总长未知时不出现斜杠（那是假分母）", "/" not in fmt(15.0, 0.0, True),
      f"得到 {fmt(15.0, 0.0, True)!r}")
check("总长未知但仍显示已播秒数", "15" in fmt(15.0, 0.0, True))
check("刚开始（不足 1 秒）也不报假分母", "/" not in fmt(0.2, 0.0, True))
check("总长已知时照旧显示分数", fmt(15.0, 141.0, True) == "15/141s")
check("播完显示相等分数", fmt(141.0, 141.0, False) == "141/141s")

# ── _report 侧：live 时上报 total=0 ─────────────────────────────────
print("\n── _report：live 阶段上报 total=0 ──")
_m = re.search(r"total = 0 if t\.live else t\.total", psrc)
check("⭐ _report 里 live 时把 total 归零", _m is not None,
      "没有这一行，卡片又会显示「已落盘长度」当总长")

print(f"\n{'='*52}\n通过 {ok} 条，失败 {fail} 条")
sys.exit(1 if fail else 0)
