"""Fleet watchdog 的重复报警与硬上限。

2026-08-08 实测的 bug：一条漏发 done 的 task 在 registry 里不会过期，而防 spam
是按「上次 fire 时刻」算的 —— 只要 fleet 偶尔来点**无关的** inbox 流量再静默
十分钟，同一条 task 就再报一次。10:41 报 7205ecb3，10:59 一条给别人的 cron
回执刷新了时钟，11:10 又报同一条，内容一字不差。
"""
from datetime import datetime, timedelta, timezone

import pytest

from closecrab.channels import feishu as F


def _task(tid="t1", kickoff_min_ago=30, update_min_ago=30):
    now = datetime.now(timezone.utc)
    return F._TaskState(
        task_id=tid, task_name=f"任务{tid}", worker_bot="bunny",
        kickoff_at=now - timedelta(minutes=kickoff_min_ago),
        last_update_at=now - timedelta(minutes=update_min_ago),
        chat_id="oc_test",
    )


def _pick(tasks, now=None):
    """调**真的** _watchdog_select，不在测试里另抄一份判定。

    ticker 本体是带 sleep 的无限循环没法直接跑，所以判定被抽成了独立方法；
    这里用一个裸实例挂上 registry 去调它 —— 被测的是线上那份代码。
    """
    ch = object.__new__(F.FeishuChannel)
    ch._task_registry = {t.task_id: t for t in tasks}
    return ch._watchdog_select(now or datetime.now(timezone.utc))


def _fire(picked, now=None):
    now = now or datetime.now(timezone.utc)
    for t in picked:
        t.alerted_at = now


# ── 核心：同一条 task 不许报第二次 ──────────────────────────────────────────

def test_same_stale_task_alerts_only_once():
    t = _task()
    first = _pick([t])
    assert [x.task_id for x in first] == ["t1"], "第一次必须报"
    _fire(first)

    # 无关流量刷新了全局 activity 时钟，又静默十分钟 —— 老逻辑这里会再报一次
    assert _pick([t]) == [], "没有新进展就不该重复报警"


def test_new_progress_reopens_alerting():
    """报过之后又收到新进展、然后再次静默 → 这是新信息，应该再报。"""
    t = _task()
    _fire(_pick([t]))
    assert _pick([t]) == []

    t.last_update_at = datetime.now(timezone.utc)      # 来了一条 progress
    assert [x.task_id for x in _pick([t])] == ["t1"]


def test_other_task_not_suppressed_by_first():
    """按 task 计而不是按 fire 时刻计：A 报过不该堵住 B 的第一次报警。

    老逻辑用一个全局 _last_watchdog_fired_at，会连带压掉 B。
    """
    a, b = _task("a"), _task("b")
    _fire(_pick([a]))
    assert [x.task_id for x in _pick([a, b])] == ["b"]


# ── 硬上限 ──────────────────────────────────────────────────────────────────

def test_task_abandoned_after_max_age():
    old = _task("old", kickoff_min_ago=F._TASK_MAX_AGE_SEC / 60 + 1)
    assert _pick([old]) == []
    assert old.status == "timeout", "超龄的要停止跟踪，不能一直挂在 registry 里"


def test_task_just_under_max_age_still_tracked():
    young = _task("young", kickoff_min_ago=F._TASK_MAX_AGE_SEC / 60 - 1)
    assert [x.task_id for x in _pick([young])] == ["young"]
    assert young.status == "active"


def test_done_task_never_alerts():
    t = _task()
    t.status = "done"
    assert _pick([t]) == []


# ── 防回归：老的全局 fire 时刻字段不该再出现 ────────────────────────────────

def test_global_fire_timestamp_no_longer_read():
    """留着这个字段就会有人再拿它做防 spam，等于把 bug 请回来。

    只查**属性访问**（`self._last_watchdog_fired_at`）；注释里提它是有意保留的
    病历，不该被判违规。
    """
    with open(F.__file__.replace(".pyc", ".py"), encoding="utf-8") as f:
        body = f.read()
    assert "self._last_watchdog_fired_at" not in body
