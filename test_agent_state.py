"""Bot 状态机 —— 纯逻辑，不碰 IO。

事件序列**照抄 2026-09-20 实测抓下来的那一串**（/tmp/sub-raw.jsonl），
不是我想象的形状。状态机错一格的后果是屏幕上显示的和实际不符，
而那种错在真机上看不出来 —— 它永远看起来很合理。
"""
import pytest

from closecrab.core.agent_state import AgentState, TaskView, describe_tool


def tool(name, inp, tuid="toolu_1", parent=None):
    d = {"type": "assistant",
         "message": {"content": [{"type": "tool_use", "id": tuid,
                                  "name": name, "input": inp}]}}
    if parent:
        d["parent_tool_use_id"] = parent
    return d


# ── 一句人话 ──────────────────────────────────────────────

def test_describe_common_tools():
    assert describe_tool("Read", {"file_path": "/a/b/x.py"}) == "在读 x.py"
    assert describe_tool("Bash", {"command": "ls -la\nfoo"}).startswith("在跑 ls -la")
    assert describe_tool("Grep", {"pattern": "abc"}) == "在搜 abc"
    assert describe_tool("TodoWrite", {}) == "在列计划"


def test_mcp_tools_are_grouped_not_dumped():
    """MCP 工具要按前缀归类。不归的话查 wiki 和搜网页在屏幕上长一样。"""
    assert describe_tool("mcp__wiki__wiki_query", {}) == "在查 wiki"
    assert describe_tool("mcp__tavily__tavily_search", {}) == "在搜网页"
    assert describe_tool("mcp__jina-ai__read_url", {}) == "在搜网页"


def test_unknown_tool_says_so_instead_of_making_up_a_verb():
    """不认识就如实说不认识。**编一个动词上去比不显示更糟** ——
    它看起来像我们知道它在干什么。"""
    assert describe_tool("SomeNewThing", {}) == "在用 SomeNewThing"


# ── 主 turn ───────────────────────────────────────────────

def test_turn_lifecycle():
    s = AgentState()
    s.begin_turn(now=100)
    assert s.turn_active and s.main_activity == "在想"
    s.on_event(tool("Read", {"file_path": "/x/y.py"}), now=101)
    assert s.main_activity == "在读 y.py"
    s.on_event({"type": "result", "subtype": "success"}, now=105)
    assert not s.turn_active
    assert s.snapshot(now=200)["sec"] == 5.0, "结束之后耗时要冻住，不能一直涨"


def test_waiting_for_user_is_its_own_state():
    """「在跑」和「在等你」必须分得开 —— 这两种在屏幕上后果完全不同。"""
    s = AgentState(); s.begin_turn(now=0)
    s.on_event(tool("ExitPlanMode", {}), now=1)
    assert s.waiting_for == "等你批准方案"
    s.on_permission_resolved()
    assert s.waiting_for == ""
    s.on_permission_request("Bash")
    assert "Bash" in s.waiting_for


# ── 子 agent（事件序列照抄实测）────────────────────────────

def _spawn(s, tuid, tid, desc="查点东西", kind="general-purpose", now=0):
    s.on_event(tool("Agent", {"description": desc}, tuid=tuid), now=now)
    s.on_event({"type": "system", "subtype": "task_started",
                "task_id": tid, "subagent_type": kind}, now=now)


def test_subagent_counted_and_linked():
    s = AgentState(); s.begin_turn(now=0)
    _spawn(s, "toolu_A", "task_A", desc="术语一致性审计", now=1)
    snap = s.snapshot(now=2)
    assert snap["subs"] == {"run": 1, "done": 0}
    assert snap["tasks"][0]["what"] == "术语一致性审计"
    assert snap["tasks"][0]["kind"] == "general-purpose"


def test_subagent_completion_carries_summary():
    s = AgentState(); s.begin_turn(now=0)
    _spawn(s, "toolu_A", "task_A", now=1)
    s.on_event({"type": "system", "subtype": "task_notification",
                "task_id": "task_A", "status": "completed",
                "summary": "1+1=2"}, now=4)
    snap = s.snapshot(now=9)
    assert snap["subs"] == {"run": 0, "done": 1}
    t = snap["tasks"][0]
    assert t["st"] == "completed" and t["sum"] == "1+1=2"
    assert t["sec"] == 3.0, "干完之后它自己的耗时也要冻住"


def test_subagent_activity_is_attributed_to_the_right_task():
    """两个子 agent 同时在跑，各自的动作不能串台。"""
    s = AgentState(); s.begin_turn(now=0)
    _spawn(s, "toolu_A", "task_A", desc="甲", now=1)
    _spawn(s, "toolu_B", "task_B", desc="乙", now=1)
    s.on_event(tool("Read", {"file_path": "/a.py"}, parent="toolu_A"), now=2)
    s.on_event(tool("Bash", {"command": "make"}, parent="toolu_B"), now=2)
    by = {t["what"]: t["act"] for t in s.snapshot(now=3)["tasks"]}
    assert by["甲"] == "在读 a.py"
    assert by["乙"].startswith("在跑 make")


def test_subagent_activity_does_not_overwrite_main():
    """子 agent 的动作**不许**顶掉主 agent 那一行 —— 今天飞书卡片上
    正是这么串的：子 agent 说的话跟主 agent 的步骤混在一条流水里。"""
    s = AgentState(); s.begin_turn(now=0)
    s.on_event(tool("Edit", {"file_path": "/main.py"}), now=1)
    _spawn(s, "toolu_A", "task_A", now=2)
    s.on_event(tool("Read", {"file_path": "/sub.py"}, parent="toolu_A"), now=3)
    assert s.main_activity == "在派活 查点东西", s.main_activity
    assert s.snapshot(now=4)["tasks"][0]["act"] == "在读 sub.py"


def test_unlinkable_activity_is_counted_not_swallowed():
    """挂不上任何任务的子 agent 动作要**记一笔并显示出来**。
    悄悄吞掉的话，屏幕会安静地少一块，而没人知道少了。"""
    s = AgentState(); s.begin_turn(now=0)
    s.on_event(tool("Read", {"file_path": "/x.py"}, parent="toolu_ghost"), now=1)
    assert s.snapshot(now=2)["unlinked"] == 1


def test_orphan_completion_lands_in_background_not_subagents():
    """只收到收尾、没收到开始 —— 显示，但**算后台任务不算子 agent**。

    因为它没有 subagent_type。见下面那条测试。"""
    s = AgentState(); s.begin_turn(now=0)
    s.on_event({"type": "system", "subtype": "task_notification",
                "task_id": "task_Z", "status": "completed", "summary": "好了"}, now=2)
    snap = s.snapshot(now=3)
    assert snap["subs"] == {"run": 0, "done": 0}
    assert snap["bg"] == {"run": 0, "done": 1}


def test_background_bash_is_not_counted_as_a_subagent():
    """⚠️ **上线第一分钟被真实数据抓到的 bug。**

    `task_started` 不只发给子 agent，**后台 Bash 也发**。屏幕上当时显示
    「1 个子 agent 已完成」，而那其实是一条后台命令。

    判别依据是 `subagent_type`：真子 agent 带，后台任务不带。"""
    s = AgentState(); s.begin_turn(now=0)
    # 后台命令：task_started 不带 subagent_type
    s.on_event({"type": "system", "subtype": "task_started",
                "task_id": "bg1"}, now=1)
    # 真子 agent：带
    _spawn(s, "toolu_A", "sub1", desc="查东西", now=1)
    snap = s.snapshot(now=2)
    assert snap["subs"] == {"run": 1, "done": 0}, "后台命令被算成子 agent 了"
    assert snap["bg"] == {"run": 1, "done": 0}
    kinds = {t["id"]: t["sub"] for t in snap["tasks"]}
    assert kinds["bg1"] is False and kinds["sub1"] is True


def test_turn_end_marks_dangling_subagents_unknown_not_completed():
    """turn 结束时还挂着的子 agent 标成 unknown。
    **不知道就说不知道** —— 标成 completed 是在编。"""
    s = AgentState(); s.begin_turn(now=0)
    _spawn(s, "toolu_A", "task_A", now=1)
    s.on_event({"type": "result", "subtype": "success"}, now=5)
    assert s.snapshot(now=6)["tasks"][0]["st"] == "unknown"


def test_tasks_survive_turn_end():
    """turn 结束后那块屏还要显示「刚才派了几个」。清掉的话屏幕会突然变空。"""
    s = AgentState(); s.begin_turn(now=0)
    _spawn(s, "toolu_A", "task_A", now=1)
    s.on_event({"type": "system", "subtype": "task_notification",
                "task_id": "task_A", "status": "completed"}, now=3)
    s.on_event({"type": "result", "subtype": "success"}, now=4)
    assert s.snapshot(now=5)["subs"] == {"run": 0, "done": 1}


def test_new_turn_clears_previous():
    s = AgentState(); s.begin_turn(now=0)
    _spawn(s, "toolu_A", "task_A", now=1)
    s.on_event({"type": "result"}, now=2)
    s.begin_turn(now=10)
    assert s.snapshot(now=11)["subs"] == {"run": 0, "done": 0}


def test_only_durable_changes_report_true():
    """返回值是「该不该走参与者属性」。每改一次属性都是一趟信令，
    没变的事件必须返回 False，否则就是拿信令刷屏。"""
    s = AgentState(); s.begin_turn(now=0)
    assert s.on_event({"type": "system", "subtype": "hook_started"}, now=1) is False
    assert s.on_event({"type": "user", "message": {"content": "x"}}, now=1) is False
    assert s.on_event(tool("Read", {"file_path": "/a.py"}), now=1) is True


def test_main_turn_carries_task_and_summary():
    """主 turn 也该有「被派去干什么」和「做成了什么」。

    ⚠️ Chris 2026-09-20 指出的：状态屏上主 turn 那一格填的是
    「此刻在调哪个工具」——**那是过程不是任务**，看不出这一轮在干啥。
    子 agent 早就有这两样（派活描述 ＋ 回报摘要），主 turn 的对应物
    是「用户原话」和「回复第一句」，只是之前没送。
    """
    st = AgentState()
    st.begin_turn(1000.0, task="把今天的 commit 过一遍写成时间线")
    snap = st.snapshot(1005.0)
    assert snap["task"] == "把今天的 commit 过一遍写成时间线"
    assert snap["sum"] == ""          # 还没干完，没有摘要
    assert snap["on"] is True

    st.end_turn(1060.0, summary="四个根因假设，三个被推翻")
    done = st.snapshot(1060.0)
    assert done["task"] == "把今天的 commit 过一遍写成时间线"   # 任务不因结束而消失
    assert done["sum"] == "四个根因假设，三个被推翻"
    assert done["on"] is False


def test_task_and_summary_are_truncated():
    """两个都要截断 —— 它们要塞进参与者属性，而用户那句话可能很长。"""
    st = AgentState()
    st.begin_turn(1000.0, task="很长" * 200)
    assert len(st.snapshot(1000.0)["task"]) == 80
    st.end_turn(1001.0, summary="也很长" * 200)
    assert len(st.snapshot(1001.0)["sum"]) == 80


def test_new_turn_clears_last_summary():
    """新一轮开始时上一轮的摘要必须清掉。

    不清的话屏幕上会是「在忙 ＋ 上一轮的结论」—— 那比没有更糟，
    因为它看起来完全合理。
    """
    st = AgentState()
    st.begin_turn(1000.0, task="第一件事")
    st.end_turn(1010.0, summary="第一件事的结论")
    st.begin_turn(1020.0, task="第二件事")
    snap = st.snapshot(1021.0)
    assert snap["task"] == "第二件事"
    assert snap["sum"] == "", "上一轮的摘要漏进了新一轮"
