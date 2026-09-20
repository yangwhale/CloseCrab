"""Bot 在忙什么 —— 从 stream-json 事件里攒出一份「此刻的状态」。

给 iOS 那块大屏用：主 turn 在干啥、起了几个子 agent、各自跑完没有、
是在跑还是在等你点确认。

## 为什么单独一个纯逻辑模块

这里**不 import livekit、不 import 任何 IO**，所以能在开发机上直接跑测试。
状态机错一格的后果是「屏幕上显示的和实际不符」——那种错只能靠测试拦，
靠真机看是看不出来的（它看起来永远很合理）。

## 事件是怎么来的（2026-09-20 实测抓的原始流，不是猜的）

派一个子 agent，CLI 依次吐出：

    assistant   tool_use name=Agent          id=toolu_xxx
    system      subtype=task_started         task_id=af70… subagent_type=general-purpose
    user        parent_tool_use_id=toolu_xxx subagent_type=… task_description=…
    system      subtype=task_updated         task_id=af70…
    system      subtype=task_notification    task_id=af70… status=completed summary=…
    result      subtype=success              num_turns=2

**有专门的任务生命周期事件**，不用从 `parent_tool_use_id` 硬推。

⚠️ 但 `task_started` 里**没有** tool_use_id，`user` 事件里**没有** task_id ——
这两个标识符官方没给我们连起来。要把「某个子 agent 此刻在干啥」挂到
具体那一条任务上，只能靠「Agent 工具调用之后紧跟着的那条 task_started
就是它」这个顺序假设。**这个假设我们自己盯着**：连不上就记一笔
`unlinked`，不假装连上了。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["AgentState", "TaskView", "describe_tool"]


def describe_tool(name: str, inp: dict) -> str:
    """一个工具调用 → 一句人话。

    给屏幕看的，所以是「在读 xxx.py」不是 `Read(file_path=...)`。
    不认识的工具**如实说不认识**（显示工具名），不要编一个动词上去。
    """
    import os

    def _base(p: Any) -> str:
        return os.path.basename(str(p)) if p else ""

    n = name or ""
    if n in ("Read", "Write", "Edit", "NotebookEdit"):
        verb = {"Read": "在读", "Write": "在写", "Edit": "在改",
                "NotebookEdit": "在改"}[n]
        return f"{verb} {_base(inp.get('file_path'))}".strip()
    if n == "Bash":
        cmd = str(inp.get("command", "")).strip().split("\n")[0]
        return f"在跑 {cmd[:60]}" if cmd else "在跑命令"
    if n in ("Grep", "Glob"):
        return f"在搜 {str(inp.get('pattern', ''))[:40]}"
    if n in ("WebSearch",):
        return f"在搜网页 {str(inp.get('query', ''))[:40]}"
    if n in ("WebFetch",):
        return f"在读网页 {str(inp.get('url', ''))[:50]}"
    if n in ("Agent", "Task"):
        return f"在派活 {str(inp.get('description', ''))[:40]}"
    if n == "TodoWrite":
        return "在列计划"
    if n == "Skill":
        return f"在用 {inp.get('skill', '')}"
    # MCP 工具按前缀归类。不归的话它们全挤在「其它」里，
    # 而查 wiki 和搜网页在屏幕上应该是两回事。
    low = n.lower()
    if "wiki" in low:
        return "在查 wiki"
    if any(k in low for k in ("search", "tavily", "jina", "brave")):
        return "在搜网页"
    if any(k in low for k in ("browser", "chrome", "playwright")):
        return "在开浏览器"
    if any(k in low for k in ("image", "multimodal", "vision")):
        return "在看图"
    return f"在用 {n}" if n else "在忙"


@dataclass
class TaskView:
    """一个子 agent。"""

    task_id: str
    kind: str = ""              # subagent_type
    what: str = ""              # 派它去干什么
    started_at: float = 0.0
    done_at: Optional[float] = None
    status: str = "running"     # running / completed / failed
    summary: str = ""           # 干完之后那一句
    activity: str = ""          # 此刻在干啥（连得上才有，见模块文档）
    tool_use_id: str = ""       # 跟 parent_tool_use_id 对上用的

    @property
    def is_subagent(self) -> bool:
        """真子 agent，还是后台任务。

        ⚠️ **`task_started` 不只发给子 agent** —— 后台 Bash 也发。
        2026-09-20 上线第一分钟就被真实数据抓到：屏幕上显示「1 个子 agent
        已完成」，而那其实是一条后台命令，摘要写的是那条命令的描述。

        判别依据是 `subagent_type`：真子 agent 带（`general-purpose` 之类），
        后台任务不带。**这条是从实测事件里读出来的，不是约定。**
        """
        return bool(self.kind)

    @property
    def running(self) -> bool:
        return self.done_at is None

    def elapsed(self, now: float) -> float:
        return (self.done_at or now) - self.started_at


@dataclass
class AgentState:
    """**一个 turn 的状态。** 喂原始事件进去，读属性出来。

    `on_event` 返回「**耐久状态**变了没有」—— 变了才值得走参与者属性
    （每改一次都是一趟信令）。滚动的流水另有出口，不看这个返回值。
    """

    turn_active: bool = False
    turn_started_at: float = 0.0
    turn_ended_at: Optional[float] = None
    #: 在等人点确认（批准工具 / 选方案 / 回答问题）。等的是什么放这里。
    waiting_for: str = ""
    #: 主 agent 此刻在干啥（最后一个**不带父 ID** 的工具调用）
    main_activity: str = ""
    tasks: dict[str, TaskView] = field(default_factory=dict)
    #: 有子 agent 的活动挂不到任何一条任务上的次数。**不藏起来。**
    unlinked: int = 0
    #: 这一轮**被派去干什么** —— 就是用户那句话。
    #:
    #: ⚠️ Chris 2026-09-20：「这个主 agent 为什么不能拥有 Task 和 Summary？
    #: 后面总是跟着一句『在跑命令』之类的，那也看不出来任务到底在干啥。」
    #: 他说得对，而且**不是「做不到」是「没送」** —— 子 agent 的任务来自
    #: 派活时那句描述、摘要来自它的回报，主 turn 这两样都有对应物：
    #:   任务 = 用户原话（`_pure_text`，去掉 channel / 时间那些标记之后）
    #:   摘要 = 回复的第一句
    #: 之前那一格填的是「此刻在调哪个工具」，那是**过程**不是**任务**。
    task: str = ""
    #: 这一轮**做成了什么** —— 回复的第一句。turn 结束时才有。
    summary: str = ""

    _pending_agent_tool: list = field(default_factory=list, repr=False)
    _by_tool_use: dict = field(default_factory=dict, repr=False)

    # ── 生命周期 ──────────────────────────────────────────────

    def begin_turn(self, now: Optional[float] = None, *, task: str = "") -> None:
        now = time.time() if now is None else now
        self.task = task[:80]
        self.summary = ""
        self.turn_active = True
        self.turn_started_at = now
        self.turn_ended_at = None
        self.waiting_for = ""
        self.main_activity = "在想"
        self.tasks.clear()
        self.unlinked = 0
        self._pending_agent_tool.clear()
        self._by_tool_use.clear()

    def end_turn(self, now: Optional[float] = None, *, summary: str = "") -> None:
        now = time.time() if now is None else now
        if summary:
            self.summary = summary[:80]
        self.turn_active = False
        self.turn_ended_at = now
        self.waiting_for = ""
        self.main_activity = ""
        # ⚠️ **不清 tasks。** turn 结束之后那块屏还要显示「刚才派了 3 个、
        #    都干完了」。清掉的话屏幕会在结束那一瞬间变空，
        #    看起来像什么都没发生过。
        for t in self.tasks.values():
            if t.running:
                # turn 都结束了还挂着 running 的，是我们漏收了收尾事件。
                # 标成 unknown 而不是 completed —— 不知道就说不知道。
                t.status = "unknown"
                t.done_at = now

    # ── 喂事件 ────────────────────────────────────────────────

    def on_event(self, d: dict, now: Optional[float] = None) -> bool:
        """喂一条原始 stream-json 事件。返回耐久状态有没有变。"""
        now = time.time() if now is None else now
        t = d.get("type", "")
        sub = d.get("subtype", "")
        parent = d.get("parent_tool_use_id") or ""

        if t == "system":
            return self._on_system(d, sub, now)
        if t == "result":
            self.end_turn(now)
            return True
        if t == "assistant":
            return self._on_assistant(d, parent, now)
        if t == "user" and parent:
            # 子 agent 那一侧的消息。带着 task_description，
            # 是我们唯一能拿到「它被派去干什么」的地方。
            task = self._by_tool_use.get(parent)
            if task is None:
                self.unlinked += 1
                return False
            desc = d.get("task_description") or ""
            if desc and not task.what:
                task.what = desc[:60]
                return True
        return False

    # ── 内部 ──────────────────────────────────────────────────

    def _on_system(self, d: dict, sub: str, now: float) -> bool:
        tid = d.get("task_id") or ""
        if sub == "task_started" and tid:
            task = TaskView(task_id=tid, kind=d.get("subagent_type") or "",
                            started_at=now)
            # 跟刚才那个 Agent 工具调用配对。**顺序假设，见模块文档。**
            if self._pending_agent_tool:
                tuid, what = self._pending_agent_tool.pop(0)
                task.tool_use_id = tuid
                task.what = what
                self._by_tool_use[tuid] = task
            self.tasks[tid] = task
            return True
        if sub in ("task_notification", "task_completed") and tid:
            task = self.tasks.get(tid)
            if task is None:
                # 收尾先到、开始没见着。补一条，别丢 —— 但它没有
                # `subagent_type`，所以会落进「后台任务」那一栏，不算子 agent。
                task = TaskView(task_id=tid, started_at=now)
                self.tasks[tid] = task
            status = d.get("status") or "completed"
            task.status = status
            task.summary = (d.get("summary") or "")[:120]
            if status != "running":
                task.done_at = now
                task.activity = ""
            return True
        return False

    def _on_assistant(self, d: dict, parent: str, now: float) -> bool:
        changed = False
        for block in (d.get("message", {}) or {}).get("content", []) or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "thinking":
                if not parent and self.main_activity != "在想":
                    self.main_activity = "在想"
                    changed = True
                continue
            if bt != "tool_use":
                continue

            name = block.get("name", "")
            inp = block.get("input", {}) or {}
            what = describe_tool(name, inp)

            if parent:
                task = self._by_tool_use.get(parent)
                if task is None:
                    self.unlinked += 1
                else:
                    task.activity = what
                    changed = True
                continue

            # 主 agent 自己的工具
            if name in ("Agent", "Task"):
                self._pending_agent_tool.append(
                    (block.get("id", ""), str(inp.get("description", ""))[:60]))
            if name in ("ExitPlanMode", "AskUserQuestion"):
                self.waiting_for = ("等你批准方案" if name == "ExitPlanMode"
                                    else "等你回答")
                changed = True
            self.main_activity = what
            changed = True
        return changed

    def on_permission_request(self, tool: str) -> bool:
        """要人点「允许」了。由 worker 的 control_request 那条路调。"""
        self.waiting_for = f"等你允许 {tool}"
        return True

    def on_permission_resolved(self) -> bool:
        if not self.waiting_for:
            return False
        self.waiting_for = ""
        return True

    # ── 出口 ──────────────────────────────────────────────────

    def snapshot(self, now: Optional[float] = None) -> dict:
        """给客户端看的一份。**字段短，因为要塞进参与者属性。**"""
        now = time.time() if now is None else now
        subs = [t for t in self.tasks.values() if t.is_subagent]
        bgs = [t for t in self.tasks.values() if not t.is_subagent]
        return {
            "v": 1,
            "on": self.turn_active,
            "wait": self.waiting_for,
            "act": self.main_activity,
            # 主 turn 的任务和摘要。**跟子 agent 那两格是同一个语义** ——
            # 跑着的时候看「被派去干什么」，干完了看「做成了什么」。
            "task": self.task,
            "sum": self.summary,
            "sec": round((self.turn_ended_at or now) - self.turn_started_at, 1)
                   if self.turn_started_at else 0.0,
            # 子 agent 和后台任务**分开数**。混在一起的话，跑一条后台命令
            # 屏幕上就多一个「子 agent」，而那是假的。
            "subs": {"run": sum(1 for t in subs if t.running),
                     "done": sum(1 for t in subs if not t.running)},
            "bg": {"run": sum(1 for t in bgs if t.running),
                   "done": sum(1 for t in bgs if not t.running)},
            "tasks": [
                {
                    "id": t.task_id[:8],
                    "sub": t.is_subagent,
                    "kind": t.kind,
                    "what": t.what,
                    "act": t.activity,
                    "st": t.status,
                    "sec": round(t.elapsed(now), 1),
                    "sum": t.summary,
                }
                for t in self.tasks.values()
            ],
            # ⚠️ 这个数不为 0 就说明「某个子 agent 在干啥」那一栏不完整。
            #    **要显示出来**，不然屏幕会安静地少掉一部分。
            "unlinked": self.unlinked,
        }
