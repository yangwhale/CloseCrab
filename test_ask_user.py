#!/usr/bin/env python3
"""bot 举手标记 `<ask-user/>`：剥离 ＋ 「等你回话」置位与清除。

背景：`AgentState.waiting_for`（快照里的 `wait`）原来只在 ExitPlanMode /
AskUserQuestion / 权限请求时置位。普通回复末尾问一句「你看呢」系统感知不到。
现在 bot 自己在回复末尾带 `<ask-user/>` 或 `<ask-user>摘要</ask-user>`，
BotCore 在统一出口剥掉并置位；用户下一条消息进来时清掉。

跑法：`python3 -m pytest -q test_ask_user.py`
"""
import asyncio
import re
import sys
import types

sys.path.insert(0, ".")
from closecrab.utils.ask_user import (  # noqa: E402
    ASK_USER_DEFAULT, ASK_USER_SUMMARY_MAX, extract_ask_user, strip_ask_user,
)


# ── 剥离 ──────────────────────────────────────────────────────────────

def test_no_marker_untouched():
    for t in ["", "普通回复。", "讲讲 <ask> 和 ask-user 这两个词", "<voice-summary>x</voice-summary>"]:
        assert extract_ask_user(t) == (t, None)


def test_self_closing_variants():
    for tag in ["<ask-user/>", "<ask-user />", "<ASK-USER/>", "<Ask-User   />", "< ask-user/ >"]:
        clean, ask = extract_ask_user(f"要不要先压高度？\n{tag}")
        assert clean == "要不要先压高度？", (tag, clean)
        assert ask == ASK_USER_DEFAULT, tag


def test_with_summary():
    clean, ask = extract_ask_user("方案 A 快、B 省钱。\n\n<ask-user>选 A 还是 B？</ask-user>")
    assert clean == "方案 A 快、B 省钱。"
    assert ask == "选 A 还是 B？"
    # 大小写、空白、跨行摘要
    clean, ask = extract_ask_user("x <ASK-USER>  要不要\n  重启？ </Ask-User >")
    assert clean == "x" and ask == "要不要 重启？"


def test_empty_summary_falls_back():
    assert extract_ask_user("好了吗？<ask-user></ask-user>") == ("好了吗？", ASK_USER_DEFAULT)
    assert extract_ask_user("好了吗？<ask-user>   </ask-user>")[1] == ASK_USER_DEFAULT


def test_multiple_markers_all_removed_first_summary_wins():
    t = "A？<ask-user/>\n中间\n<ask-user>第一句</ask-user> 尾 <ask-user>第二句</ask-user>"
    clean, ask = extract_ask_user(t)
    assert "ask-user" not in clean.lower()
    assert clean.startswith("A？") and "中间" in clean and clean.endswith("尾")
    assert ask == "第一句"


def test_unclosed_or_stray_tags_removed():
    for t in ["要继续吗？<ask-user>", "要继续吗？</ask-user>"]:
        clean, ask = extract_ask_user(t)
        assert clean == "要继续吗？" and ask == ASK_USER_DEFAULT, t


def test_summary_truncated_to_40():
    long = "要" * 100
    _, ask = extract_ask_user(f"<ask-user>{long}</ask-user>")
    assert len(ask) == ASK_USER_SUMMARY_MAX == 40
    assert ask.endswith("…")
    _, ask = extract_ask_user(f"<ask-user>{'好' * 40}</ask-user>")
    assert ask == "好" * 40                      # 恰好 40 不截


def test_whitespace_tidied():
    clean, _ = extract_ask_user("第一段\n\n<ask-user/>\n\n\n")
    assert clean == "第一段"
    clean, _ = extract_ask_user("第一段   <ask-user/>\n\n\n\n第二段")
    assert clean == "第一段\n\n第二段"


def test_strip_helper():
    assert strip_ask_user("嗯？<ask-user>x</ask-user>") == "嗯？"
    assert strip_ask_user("no tag") == "no tag"


def test_voice_strip_uses_shared_helper():
    from closecrab.voice.livekit_io import strip_voice_summary_and_file
    out = strip_voice_summary_and_file(
        "要我继续吗？<voice-summary>[casually] 嗯</voice-summary><ask-user>继续吗</ask-user>")
    assert out == "要我继续吗？"


def test_no_second_copy_of_regex():
    """剥离正则只能有 utils/ask_user 一份；别处只能调它。"""
    import pathlib
    hits = []
    for p in pathlib.Path("closecrab").rglob("*.py"):
        if p.name == "ask_user.py":
            continue
        src = p.read_text(encoding="utf-8")
        if re.search(r"re\.(sub|compile|search|findall)\([^)]*ask-user", src):
            hits.append(str(p))
    assert hits == [], hits


# ── 置位与清除（真跑 BotCore._handle_message_locked，worker 是假的）─────

class FakeWorker:
    session_id = "s1"

    def __init__(self, reply):
        self.reply = reply

    async def send(self, content, on_step=None, **_kw):
        # 跟真 CLI 一样：先来 assistant 文本，再来 result（状态机在 result 时 end_turn）
        await on_step({"type": "assistant", "message": {"content": [{"type": "text", "text": self.reply}]}})
        await on_step({"type": "result", "subtype": "success"})
        return self.reply

    def get_context_usage(self):
        return None


def make_core(monkeypatch, reply, published):
    from closecrab.core import bot as B
    from closecrab.voice import livekit_out
    monkeypatch.setattr(livekit_out, "publish_state", lambda p: published.append(dict(p)))
    core = B.BotCore.__new__(B.BotCore)
    core._db = None
    core.bot_name = "testbot"
    core._recall_seen = {}
    core._user_task_locks = {}
    core._workers = {}
    core._save_active_sessions = lambda: None

    import pathlib
    import tempfile
    core._state_dir = pathlib.Path(tempfile.mkdtemp())
    w = FakeWorker(reply)

    async def _get(uk):
        return w
    core._get_or_create_worker = _get
    return core, w


def run_turn(core, text="短问题"):
    msg = types.SimpleNamespace(content=text, metadata={}, user_id="u1", channel_type="feishu")
    return asyncio.run(core._handle_message_locked(msg, "u1", None, None))


def test_marker_sets_wait_and_returns_clean(monkeypatch):
    pub = []
    core, _ = make_core(monkeypatch, "两个方案。要先压高度吗？\n<ask-user>要不要先压高度？</ask-user>", pub)
    out = run_turn(core)
    assert out == "两个方案。要先压高度吗？"
    assert pub[-1]["wait"] == "要不要先压高度？"
    assert pub[-1]["on"] is False                     # 回合结束了，只是在等
    assert "ask-user" not in pub[-1]["sum"]           # 摘要（回复第一句）也是干净的


def test_marker_without_summary_uses_default(monkeypatch):
    pub = []
    core, _ = make_core(monkeypatch, "你看呢？<ask-user/>", pub)
    assert run_turn(core) == "你看呢？"
    assert pub[-1]["wait"] == ASK_USER_DEFAULT


def test_no_marker_no_wait(monkeypatch):
    pub = []
    core, _ = make_core(monkeypatch, "已经部署好了。", pub)
    assert run_turn(core) == "已经部署好了。"
    assert all(p["wait"] == "" for p in pub)


def test_next_user_message_clears_wait(monkeypatch):
    pub = []
    core, w = make_core(monkeypatch, "继续吗？<ask-user/>", pub)
    run_turn(core)
    assert pub[-1]["wait"] == ASK_USER_DEFAULT
    n = len(pub)
    w.reply = "好的，继续跑。"
    run_turn(core, "没问题，请继续")
    # 新一轮一开头发的那份就已经清掉了（不是等到回合结束）
    assert pub[n]["wait"] == "" and pub[n]["on"] is True
    assert pub[-1]["wait"] == ""


def test_bg_result_stripped_and_sets_wait(monkeypatch):
    from closecrab.core import bot as B
    from closecrab.voice import livekit_out
    pub, sent = [], []
    monkeypatch.setattr(livekit_out, "publish_state", lambda p: pub.append(dict(p)))
    core = B.BotCore.__new__(B.BotCore)
    core._user_task_locks = {}

    class Ch:
        async def send_to_user(self, uk, text):
            sent.append((uk, text))
    core._channel = Ch()
    asyncio.run(core._deliver_bg_result("u1", "后台跑完了，要合并吗？<ask-user>合并吗</ask-user>"))
    assert sent == [("u1", "后台跑完了，要合并吗？")]
    assert pub and pub[-1]["wait"] == "合并吗" and pub[-1]["on"] is False

    # 正有一轮在跑 ⇒ 只剥不发状态（别把「在跑」盖掉）
    pub.clear(); sent.clear()

    async def busy():
        lock = core._user_task_locks.setdefault("u1", asyncio.Lock())
        await lock.acquire()
        await core._deliver_bg_result("u1", "又一条？<ask-user/>")
        lock.release()
    asyncio.run(busy())
    assert sent == [("u1", "又一条？")] and pub == []

    # 没标记 ⇒ 不发状态
    asyncio.run(core._deliver_bg_result("u1", "普通后台回复"))
    assert pub == []


def test_prompt_has_rule():
    src = open("closecrab/main.py", encoding="utf-8").read()
    assert "`<ask-user/>` 举手标记" in src
    m = re.search(r'"\\n\\n## `<ask-user/>` 举手标记\\n"(.*?)\n    \)', src, re.S)
    assert m and len(m.group(1)) < 400, "规则要写短（每个 bot 冷启动都付这些 token）"


# ── 推荐答案（Chris 2026-10-06：「带两个最推荐的选项，推到屏幕上去选」）──────────

from closecrab.utils.ask_user import (  # noqa: E402
    ASK_USER_OPTION_MAX, ASK_USER_OPTIONS_MAX, parse_ask_user,
)


def test_options_parsed():
    a = parse_ask_user("两条路。\n<ask-user>先修哪个？|先修麦克风|先做切房间</ask-user>")
    assert a.text == "两条路。"
    assert a.summary == "先修哪个？"
    assert a.options == ["先修麦克风", "先做切房间"]


def test_old_interface_ignores_options():
    # extract_ask_user 的老调用方拿到的摘要里不能混进竖线和答案
    assert extract_ask_user("x<ask-user>先修哪个？|A|B</ask-user>") == ("x", "先修哪个？")


def test_no_options_is_empty_list():
    assert parse_ask_user("x<ask-user>要继续吗？</ask-user>").options == []
    assert parse_ask_user("x<ask-user/>").options == []
    assert parse_ask_user("没标记").options == []


def test_options_trimmed_deduped_capped():
    a = parse_ask_user("<ask-user>选？| A | |A|B|C</ask-user>")
    assert a.options == ["A", "B"]
    assert len(a.options) <= ASK_USER_OPTIONS_MAX == 2


def test_option_truncated():
    a = parse_ask_user(f"<ask-user>选？|{'长' * 200}</ask-user>")
    assert len(a.options[0]) == ASK_USER_OPTION_MAX and a.options[0].endswith("…")


def test_empty_summary_with_options():
    a = parse_ask_user("<ask-user>|好|不好</ask-user>")
    assert a.summary == ASK_USER_DEFAULT and a.options == ["好", "不好"]


def test_only_pipes_is_like_empty():
    a = parse_ask_user("好了吗？<ask-user> | | </ask-user>")
    assert a == type(a)("好了吗？", ASK_USER_DEFAULT, [])


def test_snapshot_carries_options_only_while_waiting():
    from closecrab.core.agent_state import AgentState
    s = AgentState()
    s.wait_options = ["A", "B"]
    assert s.snapshot()["opts"] == []          # 没在等 ⇒ 不带（各处清 waiting_for 不必顺手清它）
    s.waiting_for = "选哪个？"
    assert s.snapshot()["opts"] == ["A", "B"]
    s.begin_turn(task="新一轮")
    assert s.snapshot()["opts"] == []


# ── 按钮短标签（Chris 2026-10-06：「每个答案再给一个简短的 summary，显示在按钮上」）──

from closecrab.utils.ask_user import ASK_USER_LABEL_MAX  # noqa: E402


def test_labels_parsed():
    a = parse_ask_user("<ask-user>先修哪个？|修麦克风::先修启动时麦克风闪烁|切房间::先做锁屏切房间</ask-user>")
    assert a.options == ["先修启动时麦克风闪烁", "先做锁屏切房间"]
    assert a.labels == ["修麦克风", "切房间"]
    assert a.summary == "先修哪个？"


def test_label_missing_falls_back_to_full():
    a = parse_ask_user("<ask-user>选？|好|短::完整的那一句</ask-user>")
    assert a.options == ["好", "完整的那一句"] and a.labels == ["好", "短"]


def test_label_without_full_uses_label():
    a = parse_ask_user("<ask-user>选？|继续::</ask-user>")
    assert a.options == ["继续"] and a.labels == ["继续"]


def test_labels_aligned_with_options_after_dedupe():
    a = parse_ask_user("<ask-user>选？|甲::A|乙::A|丙::B|丁::C</ask-user>")
    assert a.options == ["A", "B"] and a.labels == ["甲", "丙"]
    assert len(a.labels) == len(a.options)


def test_label_truncated():
    a = parse_ask_user(f"<ask-user>选？|{'长' * 30}::完整</ask-user>")
    assert len(a.labels[0]) == ASK_USER_LABEL_MAX and a.labels[0].endswith("…")


def test_only_first_double_colon_splits():
    a = parse_ask_user("<ask-user>选？|调用::用 std::move 那个</ask-user>")
    assert a.labels == ["调用"] and a.options == ["用 std::move 那个"]


def test_snapshot_carries_labels():
    from closecrab.core.agent_state import AgentState
    s = AgentState()
    s.wait_options, s.wait_labels = ["先修麦克风闪烁"], ["修麦克风"]
    assert s.snapshot()["optl"] == []
    s.waiting_for = "先修哪个？"
    snap = s.snapshot()
    assert snap["opts"] == ["先修麦克风闪烁"] and snap["optl"] == ["修麦克风"]
