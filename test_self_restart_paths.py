"""自重启 marker 的消费路径。

**为什么要有这段代码**：2026-09-12，我在一个 watch-task 触发的 turn 里调了
`scripts/self-restart.py`。脚本回「本轮回复发出后 channel 会走 exit-42 干净
重启」，我据此告诉 Chris 修复已上生产。什么都没发生。

下一轮 watch 报回来的仍是旧行为，看上去像「修复无效」—— 排查方向被整个带偏，
直到去数进程启动时间才发现 bot 根本没重启过。

真因：`_check_self_restart` 只挂在**人类消息**那条路上
（`_handle_message_async` 末尾）。inbox / watch-task / cron 触发的 turn 走的是
`_execute_task`，从头到尾碰不到它，marker 就静静躺在盘上等下一条人类消息。

**不是失败，是根本没被调用** —— 所以日志里连一行警告都没有。这类 bug 的唯一
抓手是「把契约本身写成断言」：marker 被写下 → 消费它的地方必须都在。

两层测：
  1. 行为层：marker 真被读、真被删、冷却期真被拦。
  2. 结构层：两条 turn 路径都必须调这个函数。第 2 条看着笨，但它正是这次
     漏掉的那件事 —— 行为测得再全，call site 少一个照样全绿。
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import time

import pytest


SRC = pathlib.Path(__file__).parent / "closecrab" / "channels" / "feishu.py"

# 所有「一个 turn 跑完」的收尾函数。新增 turn 入口时这里也要加，
# 否则那条新路径上的自重启会静默失效 —— 跟这次一模一样。
TURN_TAILS = ("_handle_message_async", "_execute_task")


# ── 结构层：call site 一个都不能少 ────────────────────────────────────────

def _awaited_names(fn: ast.AST) -> set[str]:
    """函数体里所有 `await self.X(...)` 的 X。"""
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            f = node.value.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _find(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    return None


@pytest.mark.parametrize("fname", TURN_TAILS)
def test_每条turn路径收尾都要消费自重启marker(fname):
    """漏掉任何一条，那条路径上的自重启就是个静默的空操作。"""
    tree = ast.parse(SRC.read_text())
    fn = _find(tree, fname)
    assert fn is not None, f"{fname} 不见了 —— 改名了就把这份测试一起改"
    assert "_check_self_restart" in _awaited_names(fn), (
        f"{fname} 没有调 _check_self_restart。"
        f"这条路径上的 self-restart 会写完 marker 然后什么都不发生，"
        f"而且不报错 —— 2026-09-12 就是这么被骗了半小时。"
    )


def test_marker只有一个写入方():
    """写 marker 的只该是 scripts/self-restart.py。channel 里若也有人写，
    消费顺序就没法推理了。"""
    src = SRC.read_text()
    assert '".self_restart"' in src          # 读取方在
    assert 'write_text' not in src.split('".self_restart"')[1][:200], \
        "channel 里出现了写 .self_restart 的代码"


# ── 行为层：读、删、冷却 ──────────────────────────────────────────────────

class _Chan:
    """只带 `_check_self_restart` 需要的那几个字段的壳。

    不实例化真 FeishuChannel —— 它要 lark client、WebSocket、Firestore。
    要验的是这个方法自己的逻辑，把它绑到壳上就够。
    """

    def __init__(self, state: pathlib.Path, boot_ago: float):
        self._state_path = state
        self._boot_time = time.time() - boot_ago
        self._restart_requested = False
        self._loop = None
        self.sent: list[str] = []

    async def _async_send_text(self, chat_id, text):
        self.sent.append(text)


def _bind(chan):
    from closecrab.channels.feishu import FeishuChannel
    return FeishuChannel._check_self_restart.__get__(chan, type(chan))


def _cooldown() -> float:
    from closecrab.channels.feishu import _SELF_RESTART_COOLDOWN
    return _SELF_RESTART_COOLDOWN


def _write_marker(d: pathlib.Path, note: str = "接着干"):
    (d / ".self_restart").write_text(json.dumps({"note": note, "ts": time.time()}))


def test_有marker就请求重启并留续接note(tmp_path):
    _write_marker(tmp_path, "幻影缺口修好了，看新账单")
    chan = _Chan(tmp_path, boot_ago=_cooldown() + 10)
    asyncio.run(_bind(chan)("u1", "c1"))

    assert chan._restart_requested is True
    assert not (tmp_path / ".self_restart").exists(), "marker 没删 → 下次 boot 会重复消费"
    greet = json.loads((tmp_path / ".restart_greet").read_text())
    assert greet["note"] == "幻影缺口修好了，看新账单"
    assert greet["user_key"] == "u1" and greet["chat_id"] == "c1"


def test_没有marker就什么都不做(tmp_path):
    chan = _Chan(tmp_path, boot_ago=_cooldown() + 10)
    asyncio.run(_bind(chan)("u1", "c1"))
    assert chan._restart_requested is False
    assert chan.sent == []
    assert not (tmp_path / ".restart_greet").exists()


def test_冷却期内拒绝且必须说出来(tmp_path):
    """静默拒绝比不拒绝更糟：模型会以为重启成功，拿着旧代码继续汇报。"""
    _write_marker(tmp_path)
    chan = _Chan(tmp_path, boot_ago=1)
    asyncio.run(_bind(chan)("u1", "c1"))

    assert chan._restart_requested is False
    assert chan.sent and "冷却" in chan.sent[0], chan.sent
    assert not (tmp_path / ".self_restart").exists(), "拒绝了也要删，否则冷却一过就诈尸"


def test_marker内容坏掉也要删也不能崩(tmp_path):
    (tmp_path / ".self_restart").write_text("{不是 json")
    chan = _Chan(tmp_path, boot_ago=_cooldown() + 10)
    asyncio.run(_bind(chan)("u1", "c1"))
    assert not (tmp_path / ".self_restart").exists()
    assert chan._restart_requested is True      # 没 note 照样该重启
