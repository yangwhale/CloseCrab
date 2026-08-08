"""R4 审计：资源回收。没人回收的东西不会报错，只会慢慢变多。

实测背景（2026-08-08）：
- messages 里 30 条 error 躺了 70-92 天 —— 而且**是这个 sweep 自己造出来的**
  （它把孤儿 processing 标成 error），却从不清理，自我喂养。
- 另有 2 条终态文档没有 created_at，永远算不出年龄，也就永远扫不掉。
- watch_tasks 一个 GC 都没有。
"""
import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from closecrab.utils import firestore_inbox as fi  # noqa: E402

_spec = importlib.util.spec_from_file_location("wt", _ROOT / "scripts" / "watch-task.py")
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


# ── 假 Firestore：只做 sweep 用得到的那点行为 ──────────────────────────────

class _Ref:
    def __init__(self, store, key):
        self.store, self.id = store, key

    def delete(self):
        self.store.pop(self.id, None)

    def update(self, u):
        self.store[self.id].update(u)

    def set(self, u, merge=False):
        self.store.setdefault(self.id, {}).update(u)

    def get(self):
        d = self.store.get(self.id)
        return types.SimpleNamespace(exists=d is not None, to_dict=lambda: d)


class _Coll:
    def __init__(self, store):
        self.store = store

    def stream(self):
        return [
            types.SimpleNamespace(reference=_Ref(self.store, k), id=k,
                                  to_dict=(lambda v=v: v))
            for k, v in list(self.store.items())
        ]

    def document(self, key):
        return _Ref(self.store, key)


class _DB:
    def __init__(self, **colls):
        self.colls = {k: dict(v) for k, v in colls.items()}
        self.colls.setdefault("config", {})
        self.colls.setdefault("bots", {})

    def collection(self, name):
        return _Coll(self.colls.setdefault(name, {}))


def _msg(status, days_old, to="jarvis", dated=True):
    m = {"status": status, "to": to}
    if dated:
        m["created_at"] = NOW - timedelta(days=days_old)
    return m


# ── messages ───────────────────────────────────────────────────────────────

def test_error_messages_are_eventually_collected():
    """error 过去落进 else 分支永不删除，而 sweep 自己还在不停造 error。"""
    db = _DB(messages={
        "old_err": _msg("error", fi.SWEEP_ERROR_DAYS + 1),
        "new_err": _msg("error", 1),
    }, bots={"jarvis": {}})
    stats = fi.sweep_messages(db, now=NOW, force=True)
    assert stats["error"] == 1
    assert "old_err" not in db.colls["messages"]
    assert "new_err" in db.colls["messages"], "还新鲜的 error 有排查价值，别急着删"


def test_undated_terminal_docs_are_collected():
    """没有 created_at 就永远算不出年龄 —— 终态的直接收，否则永久滞留。"""
    db = _DB(messages={
        "t1": _msg("done", 0, dated=False),
        "t2": _msg("error", 0, dated=False),
        "p1": _msg("pending", 0, dated=False),
    }, bots={"jarvis": {}})
    stats = fi.sweep_messages(db, now=NOW, force=True)
    assert stats["undated"] == 2
    assert "p1" in db.colls["messages"], "非终态可能正在写，保守留下"


def test_live_pending_is_never_touched():
    """防过度收紧：给活着的 bot 的 pending 是真实待办，一条都不能动。"""
    db = _DB(messages={"live": _msg("pending", 0, to="jarvis")}, bots={"jarvis": {}})
    fi.sweep_messages(db, now=NOW, force=True)
    assert "live" in db.colls["messages"]


def test_sweep_stats_match_reality():
    """统计必须跟实际删除量对得上 —— 边 stream 边 delete 会让两个数都不可信。"""
    msgs = {f"d{i}": _msg("done", fi.SWEEP_DONE_DAYS + 1) for i in range(20)}
    msgs.update({f"e{i}": _msg("error", fi.SWEEP_ERROR_DAYS + 1) for i in range(15)})
    msgs.update({f"k{i}": _msg("done", 1) for i in range(10)})
    db = _DB(messages=msgs, bots={"jarvis": {}})
    before = len(db.colls["messages"])
    stats = fi.sweep_messages(db, now=NOW, force=True)
    deleted = before - len(db.colls["messages"])
    assert deleted == stats["done"] + stats["error"] + stats["undated"] == 35


# ── watch_tasks ────────────────────────────────────────────────────────────

def _task(status, days_old, dated=True):
    t = {"name": "x", "status": status}
    if dated:
        t["created_at"] = NOW - timedelta(days=days_old)
    return t


def test_watch_terminal_tasks_collected():
    db = _DB(watch_tasks={
        "done_old": _task("done", wt.SWEEP_TERMINAL_DAYS + 1),
        "cancel_old": _task("cancelled", wt.SWEEP_TERMINAL_DAYS + 1),
        "expire_old": _task("expired", wt.SWEEP_TERMINAL_DAYS + 1),
        "done_new": _task("done", 1),
        "active": _task("active", 999),
    })
    assert wt._sweep(db, NOW) == 3
    left = set(db.colls["watch_tasks"])
    assert left == {"done_new", "active"}


def test_watch_sweep_never_touches_active():
    """跑了一年的 active 任务也不能碰 —— 它可能真的还在盯着什么。"""
    db = _DB(watch_tasks={"a": _task("active", 365)})
    assert wt._sweep(db, NOW) == 0
    assert "a" in db.colls["watch_tasks"]


def test_watch_sweep_is_rate_limited():
    """一小时内只扫一次，别每 30 秒全表扫一遍。"""
    db = _DB(watch_tasks={"d": _task("done", 99)})
    assert wt._sweep(db, NOW) == 1
    db.colls["watch_tasks"]["d2"] = _task("done", 99)
    assert wt._sweep(db, NOW + timedelta(minutes=5)) == 0, "5 分钟内不该再扫"
    assert wt._sweep(db, NOW + timedelta(hours=2)) == 1, "过了间隔就该扫"
