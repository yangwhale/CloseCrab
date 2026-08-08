#!/usr/bin/env python3
"""L1 time-computation tests for scripts/cron-tool.py.

Covers docs/task-scheduler-design.md §14.1 (T101-T108, T124, T181-T186) and
§14.4 (T401 migration equivalence).

Pure functions only: no Firestore, no LLM, no network, no sleep. Every case
drives a frozen `now`, which is exactly why §14.0 T-0-A insists the time
functions accept an injected `now` instead of reaching for datetime.now().

    python3 -m pytest test_cron_time.py -q
"""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# cron-tool.py has a hyphen, so it can't be imported normally.
_spec = importlib.util.spec_from_file_location(
    "cron_tool", Path(__file__).parent / "scripts" / "cron-tool.py"
)
cron_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cron_tool)

next_cron_fire = cron_tool.next_cron_fire
parse_in = cron_tool.parse_in
parse_at = cron_tool.parse_at
validate_tz = cron_tool.validate_tz
build_instruction = cron_tool.build_instruction

HK = "Asia/Hong_Kong"


def U(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


# ── A. cron + timezone (T101-T108) ──────────────────────────────────────────
# T101 is the core assertion for G4: before the fix, "0 8 * * *" resolved to
# 08:00 UTC (= 16:00 HKT) with no error anywhere.

@pytest.mark.parametrize(
    "tid,expr,tz,now,expected",
    [
        ("T101", "0 8 * * *",    HK,    U(2026, 8, 8, 5, 0),   U(2026, 8, 9, 0, 0)),
        ("T102", "0 8 * * *",    HK,    U(2026, 8, 8, 23, 0),  U(2026, 8, 9, 0, 0)),
        ("T103", "0 8 * * *",    "UTC", U(2026, 8, 8, 5, 0),   U(2026, 8, 8, 8, 0)),
        ("T104", "13 8 * * 1",   HK,    U(2026, 8, 8, 5, 0),   U(2026, 8, 10, 0, 13)),
        ("T105", "0 8 * * MON-FRI", HK, U(2026, 8, 7, 5, 0),   U(2026, 8, 10, 0, 0)),
        ("T106", "0 8 1 * *",    HK,    U(2026, 8, 8, 5, 0),   U(2026, 9, 1, 0, 0)),
        ("T107", "0 8 * * *",    HK,    U(2026, 12, 31, 20, 0), U(2027, 1, 1, 0, 0)),
        ("T108", "0 8 29 2 *",   HK,    U(2026, 3, 1, 0, 0),   U(2028, 2, 29, 0, 0)),
    ],
)
def test_cron_with_tz(tid, expr, tz, now, expected):
    assert next_cron_fire(expr, now, tz) == expected, tid


def test_T101_regression_utc_vs_hkt_differ_by_eight_hours():
    """Guard the fix itself: the same expr in the two zones must differ by 8h.

    If someone later makes tz a no-op, every case above could still pass by
    accident if the fixtures were rewritten to match. This one cannot.
    """
    # `now` must sit before both resolutions, otherwise one of them rolls to
    # the next day and the gap is no longer the raw zone offset.
    now = U(2026, 8, 7, 20, 0)  # = 2026-08-08 04:00 HKT
    utc_fire = next_cron_fire("0 8 * * *", now, "UTC")
    hkt_fire = next_cron_fire("0 8 * * *", now, HK)
    assert hkt_fire == U(2026, 8, 8, 0, 0)
    assert utc_fire == U(2026, 8, 8, 8, 0)
    assert utc_fire - hkt_fire == timedelta(hours=8)


# ── B. timezone enum (T124) ─────────────────────────────────────────────────
# Q7: DST zones are refused loudly rather than silently mis-scheduled.

@pytest.mark.parametrize("tz", ["America/New_York", "Europe/London", "Mars/Olympus", "PST"])
def test_T124_dst_and_unknown_zones_rejected(tz):
    with pytest.raises(ValueError, match="unsupported"):
        validate_tz(tz)
    with pytest.raises(ValueError, match="unsupported"):
        next_cron_fire("0 8 * * *", U(2026, 8, 8, 5, 0), tz)


@pytest.mark.parametrize("tz", [HK, "UTC"])
def test_allowed_zones_accepted(tz):
    assert validate_tz(tz) == tz


# ── C. --in / --at / malformed input (T181-T186) ────────────────────────────

def test_T181_relative_delay():
    now = U(2026, 8, 8, 5, 0)
    assert parse_in("10m", now) == now + timedelta(minutes=10)
    assert parse_in("2h", now) == now + timedelta(hours=2)
    assert parse_in("90s", now) == now + timedelta(seconds=90)
    assert parse_in("3d", now) == now + timedelta(days=3)


@pytest.mark.parametrize("bad", ["90", "abc", "10x", "", "-5m"])
def test_T182_unitless_or_malformed_delay_rejected(bad):
    with pytest.raises(ValueError):
        parse_in(bad, U(2026, 8, 8, 5, 0))


def test_T183_bare_at_is_read_as_hkt_not_utc():
    """The --at counterpart of G4.

    Our VMs run UTC, so a naive timestamp used to be silently taken as UTC.
    "8am" must mean 8am where the user lives.
    """
    assert parse_at("2026-08-09T08:00:00", HK) == U(2026, 8, 9, 0, 0)
    assert parse_at("2026-08-09T08:00:00", "UTC") == U(2026, 8, 9, 8, 0)


def test_at_with_explicit_offset_wins_over_tz():
    assert parse_at("2026-08-09T08:00:00Z", HK) == U(2026, 8, 9, 8, 0)
    assert parse_at("2026-08-09T08:00:00+08:00", "UTC") == U(2026, 8, 9, 0, 0)


@pytest.mark.parametrize("expr", ["0 25 * * *", "0 8 * *", "0 8 * * * *", "notacron"])
def test_T184_malformed_cron_rejected(expr):
    with pytest.raises((ValueError, KeyError)):
        next_cron_fire(expr, U(2026, 8, 8, 5, 0), HK)


def test_T185_step_syntax_supported():
    """*/7 was used by a real (now-cancelled) production job."""
    got = next_cron_fire("*/7 * * * *", U(2026, 8, 8, 5, 3), HK)
    assert got == U(2026, 8, 8, 5, 7)


def test_T186_unknown_tz_rejected_at_parse_at_too():
    with pytest.raises(ValueError, match="unsupported"):
        parse_at("2026-08-09T08:00:00", "Mars/Olympus")


# ── D. T401 — migration equivalence, the golden criterion ───────────────────

@pytest.mark.parametrize(
    "old_expr,new_expr",
    [
        ("13 0 * * 1", "13 8 * * 1"),   # live job: Weekly Prompt Audit
        ("7 0 * * 1", "7 8 * * 1"),     # live job: Weekly Memory Audit
    ],
)
def test_T401_migration_is_equivalent(old_expr, new_expr):
    """Old expr under old semantics (UTC) == new expr under new semantics (HKT).

    Sampled weekly across a year. This is what licenses the migration; reading
    the two expressions side by side is not.
    """
    ok, detail = cron_tool._exprs_equivalent(old_expr, new_expr, samples=60)
    assert ok, detail


def test_T401_negative_a_wrong_shift_is_caught():
    """The equivalence check must actually be able to fail.

    A checker that returns True unconditionally would let any migration
    through, so prove it rejects a deliberately wrong shift.
    """
    ok, _ = cron_tool._exprs_equivalent("13 0 * * 1", "13 9 * * 1", samples=10)
    assert not ok


def test_shift_helper_refuses_ambiguous_exprs():
    for expr in ["*/7 * * * *", "0 * * * *", "0 17 * * *", "0 1-5 * * *"]:
        new, why = cron_tool._shift_expr_utc_to_hkt(expr)
        assert new is None and why, expr


def test_shift_helper_handles_the_two_live_jobs():
    assert cron_tool._shift_expr_utc_to_hkt("13 0 * * 1")[0] == "13 8 * * 1"
    assert cron_tool._shift_expr_utc_to_hkt("7 0 * * 1")[0] == "7 8 * * 1"


# ── D2. Backward compatibility: legacy jobs must not move on deploy ─────────

def test_legacy_job_without_tz_keeps_utc_semantics():
    """A job with no `tz` predates the G4 fix and was hand-compensated to UTC.

    Reading it as HKT the moment the new code ships would shift every live job
    by 8 hours — the same bug we're fixing, sign flipped. This asserts the
    constant that prevents it, and that it differs from the new-job default.
    """
    assert cron_tool.LEGACY_TZ == "UTC"
    assert cron_tool.DEFAULT_TZ == HK

    now = U(2026, 8, 7, 20, 0)
    legacy = next_cron_fire("13 0 * * 1", now, cron_tool.LEGACY_TZ)
    migrated = next_cron_fire("13 8 * * 1", now, cron_tool.DEFAULT_TZ)
    # Migration is a no-op in wall-clock terms; that is the whole point.
    assert legacy == migrated == U(2026, 8, 10, 0, 13)


# ── D3. State transition on fire (pure half of the claim) ───────────────────

def test_recurring_advances_fire_at():
    now = U(2026, 8, 7, 20, 0)
    job = {"kind": "recurring", "cron": "0 8 * * *", "tz": HK, "fire_count": 4}
    upd, err = cron_tool.compute_fire_update(job, now)
    assert err is None
    assert upd["fire_at"] == U(2026, 8, 8, 0, 0)
    assert upd["fire_count"] == 5
    assert "status" not in upd  # stays scheduled


def test_oneshot_is_marked_done():
    upd, err = cron_tool.compute_fire_update(
        {"kind": "oneshot", "fire_count": 0}, U(2026, 8, 7, 20, 0)
    )
    assert err is None and upd["status"] == "done"


def test_recurring_with_broken_cron_goes_to_error_not_silent_stall():
    """A job whose expression stopped parsing must surface as `error`.

    Leaving it `scheduled` with an unchanged fire_at would make it re-fire on
    every single tick — a 30-second spam loop.
    """
    upd, err = cron_tool.compute_fire_update(
        {"kind": "recurring", "cron": "0 25 * * *", "tz": HK, "fire_count": 0},
        U(2026, 8, 7, 20, 0),
    )
    assert err and upd["status"] == "error"


def test_legacy_recurring_job_advances_using_utc():
    """No tz field → keep UTC semantics (the deploy-safety invariant)."""
    now = U(2026, 8, 7, 20, 0)
    upd, err = cron_tool.compute_fire_update(
        {"kind": "recurring", "cron": "13 0 * * 1", "fire_count": 1}, now
    )
    assert err is None and upd["fire_at"] == U(2026, 8, 10, 0, 13)


# ── E. T201 — job_id must reach the agent (G1) ──────────────────────────────

def test_T201_recurring_instruction_carries_job_id():
    job = {
        "job_id": "abc123def456",
        "kind": "recurring",
        "message": "查一下资源池",
        "fire_count": 2,
    }
    text = build_instruction(job)
    assert "abc123def456" in text          # the handle itself
    assert "remove abc123def456" in text   # and how to use it
    assert "第 3 次触发" in text


def test_oneshot_instruction_stays_minimal():
    """A one-shot job retires itself; telling it how to self-remove is noise."""
    job = {"job_id": "x1", "kind": "oneshot", "message": "记得开会"}
    text = build_instruction(job)
    assert text == "[⏰ 定时提醒] 记得开会"


# ═══════════════════════════════════════════════════════════════════════════
# R1 审计：「永远不会发生」必须抛，不能变成 None 往下流
# ═══════════════════════════════════════════════════════════════════════════

_R1_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("expr", [
    "0 8 31 2 *",   # 2 月 31 号
    "0 8 30 2 *",   # 2 月 30 号
])
def test_impossible_date_raises_not_none(expr):
    """每个字段都在合法范围内，但日期组合永远不存在。

    以前返回 None，而 None 一路往下流会被当成「没事」：tz 迁移把
    fire_at=None 写进 Firestore，claim 那里 `if not fa` 永远跳过它，
    既不触发也不报错 —— 一个静默死掉的 job。
    """
    with pytest.raises(ValueError, match="never fires"):
        cron_tool.next_cron_fire(expr, _R1_NOW, "Asia/Hong_Kong")


def test_zero_step_gives_readable_error():
    """`*/0` 以前漏出 range() 的内部报错，看不出是表达式的问题。"""
    with pytest.raises(ValueError, match="step must be positive"):
        cron_tool.next_cron_fire("*/0 * * * *", _R1_NOW, "Asia/Hong_Kong")


def test_equivalence_refuses_when_either_side_cannot_be_evaluated():
    """两条永不触发的表达式过去双双得到 None，None == None 判成「等价」。

    这是最阴的一条 —— 迁移的黄金判据会给一条死表达式盖章放行。
    """
    ok, detail = cron_tool._exprs_equivalent("0 8 31 2 *", "0 8 31 2 *")
    assert ok is False
    assert "cannot evaluate" in detail


def test_equivalence_still_accepts_the_real_migration():
    """防过度收紧：真实的那两条生产迁移必须仍判等价。"""
    for old, new in [("13 0 * * 1", "13 8 * * 1"), ("7 0 * * 1", "7 8 * * 1")]:
        ok, detail = cron_tool._exprs_equivalent(old, new)
        assert ok, f"{old} -> {new} 被误判为不等价: {detail}"


def test_next_cron_fire_never_returns_none():
    """签名上已经没有 None 了，用一批合法表达式确认行为一致。"""
    for expr in ["0 8 * * *", "0 8 * * MON-FRI", "*/7 * * * *", "0 8 29 2 *",
                 "0 8 1 * *", "13 8 * * 1"]:
        got = cron_tool.next_cron_fire(expr, _R1_NOW, "Asia/Hong_Kong")
        assert got is not None and got > _R1_NOW, expr


@pytest.mark.parametrize("bad", [
    "0 8 * *",        # 4 字段
    "0 8 * * * *",    # 6 字段
    "@daily",         # 别名语法不支持
    "",
    "60 8 * * *",     # 分钟越界
    "0 8 0 * *",      # 日 0
    "0 8 * 13 *",     # 月 13
    "0 8 * * 7",      # 周 7（我们只收 0-6，宁可报错也别猜）
])
def test_malformed_expressions_are_loud(bad):
    with pytest.raises(ValueError):
        cron_tool.next_cron_fire(bad, _R1_NOW, "Asia/Hong_Kong")
