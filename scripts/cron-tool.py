#!/usr/bin/env python3
"""
cron-tool.py — Schedule reminders / delayed tasks for Kilo workers.

Why this exists:
  Kilo workers have no native cron/timer capability. To support
  "remind me in 10 minutes" or "every weekday 09:00 check X", we
  add a thin scheduler on top of Firestore.

How it works:
  - Each scheduled job is a Firestore doc in `scheduled_jobs/`
  - A separate daemon (cron-daemon.py) polls every 30s and dispatches
    due jobs by writing to the target bot's inbox.
  - This file is the CRUD CLI the bot uses from inside its bash tool.

Usage:
  # Add a one-shot reminder
  python3 cron-tool.py add --target jarvis --in 10m --message "记得查会议室"
  python3 cron-tool.py add --target jarvis --at "2026-05-17T15:00:00Z" --message "..."

  # Recurring (cron expr in UTC)
  python3 cron-tool.py add --target jarvis --cron "0 9 * * MON-FRI" --message "..."

  # List own jobs
  python3 cron-tool.py list                     # all jobs created by current bot
  python3 cron-tool.py list --target jarvis

  # Remove
  python3 cron-tool.py remove <job_id>

  # Run due jobs (called by daemon, not by bot)
  python3 cron-tool.py tick

  # Show 5 scheduling principles (read before adding new recurring jobs)
  python3 cron-tool.py principles

Env:
  BOT_NAME — sender bot (auto-set by bot.py)

================================================================
5 SCHEDULING PRINCIPLES — read before --cron / --in / --at
(adapted from gbrain skills/cron-scheduler)
================================================================

1. STAGGER — Never schedule multiple jobs at the same minute mark.
   Every bot defaulting to "0 9 * * *" means a flood at 09:00.00,
   contending on Firestore + inbox.
   GOOD: "3 9 * * *", "17 9 * * *", "29 9 * * *", "47 9 * * *"
   BAD : "0 9 * * *" for every bot.
   Heuristic: when user says "morning", pick :03, :17, :29, :47.

2. QUIET HOURS — Avoid 23:00-08:00 HKT for user-facing notifications.
   If a job must run nightly (e.g. backup), it can run silently but DO
   NOT push a chat notification during quiet hours. Hold and release
   in the morning batch (08:30-09:30).

3. THIN PROMPTS — Job `message` is a one-liner pointing at a skill /
   doc, NOT a 2000-word inline prompt:
   GOOD: "/health 体检一下，跑 ~/CloseCrab/skills/smoke-test"
   BAD : ...3000 words of context glued into the message...
   Why: messages persist in Firestore; long messages bloat `cron-tool list`
   output, and the receiving bot's Claude fetches fresh context from
   skill files (which evolve over time) anyway.

4. IDEMPOTENCY — Assume any job may fire twice (daemon retry, race).
   Job receiver must be safe to re-run:
   - Side-effect actions guarded by "did I already do this in the last
     N minutes?" check.
   - Reports written to time-stamped paths
     (~/.closecrab/cron-reports/<job>/<YYYY-MM-DD-HHMM>.md), not
     overwriting.
   - DB writes use idempotency keys derived from the slot.

5. REPORTS PATH — Output goes to a consistent, discoverable location:
   ~/.closecrab/cron-reports/<job_name>/<YYYY-MM-DD-HHMM>.md
   Don't dump into /tmp (cleared on reboot) or inline in inbox messages
   (gets buried).

Trade-offs: cron-daemon polls every 30s (lossy) so jitter ≈ 30s.
Don't schedule things tighter than 1 minute. For 1s-precision use
in-process asyncio.sleep, not this scheduler.
"""

PRINCIPLES = """\
5 CRON PRINCIPLES (read before scheduling a new recurring job)

1. STAGGER       never use :00 mark; pick :03 / :17 / :29 / :47 for "morning"
2. QUIET HOURS   avoid 23:00-08:00 HKT for user-facing notifications
3. THIN PROMPTS  message = one-liner pointing at a skill, not 2000 words inline
4. IDEMPOTENCY   daemon may fire twice; receiver must be safe to re-run
5. REPORTS PATH  output to ~/.closecrab/cron-reports/<name>/<YYYY-MM-DD-HHMM>.md

(See top of cron-tool.py for full rationale + examples.)
"""

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from closecrab.constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
from google.cloud import firestore

COLL = "scheduled_jobs"
NOW = lambda: datetime.now(timezone.utc)

# ── Timezone policy (docs/task-scheduler-design.md §5.5① / Q7) ──
# Deliberately a two-value enum, not "any IANA zone". Supporting arbitrary
# zones means handling DST's two nasty cases (spring-forward wall-clock times
# that don't exist, autumn-fallback times that occur twice) for a need that
# does not exist today. Anything else must ERROR at creation — a loud refusal
# beats a schedule that silently runs an hour off.
DEFAULT_TZ = "Asia/Hong_Kong"
TZ_ALLOWED = ("Asia/Hong_Kong", "UTC")

# Semantics for pre-G4 jobs that carry no `tz` field. Their expressions were
# hand-compensated into UTC, so they must keep being read as UTC until
# `migrate-tz` rewrites expression and tz atomically. Defaulting these to HKT
# would shift every live job by 8 hours on deploy.
LEGACY_TZ = "UTC"


def validate_tz(tz: str) -> str:
    if tz not in TZ_ALLOWED:
        raise ValueError(
            f"unsupported --tz {tz!r}; only {' / '.join(TZ_ALLOWED)} are supported. "
            f"Zones with DST are intentionally rejected (see docs/task-scheduler-design.md Q7)."
        )
    return tz


def _hkt(dt: datetime | None) -> str | None:
    """Render an instant as HKT for human eyes."""
    if not dt:
        return None
    return dt.astimezone(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M HKT")


def db():
    return firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)


def parse_in(s: str, now: datetime | None = None) -> datetime:
    """Parse '10m', '2h', '90s', '3d' → datetime in future."""
    m = re.fullmatch(r"(\d+)\s*([smhd])", s.strip().lower())
    if not m:
        raise ValueError(f"bad --in {s!r}; use 10m/2h/90s/3d")
    n = int(m.group(1))
    mul = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return (now or NOW()) + timedelta(seconds=n * mul)


def parse_at(s: str, tz: str = DEFAULT_TZ) -> datetime:
    """ISO 8601 → aware UTC datetime.

    A bare timestamp (no Z, no offset) is read in `tz`, NOT in the machine's
    local zone. Our VMs run UTC, so the old behaviour silently turned
    "2026-08-09T08:00:00" into 08:00 UTC = 16:00 HKT — the same class of
    off-by-timezone bug as G4.
    """
    s = s.strip()
    s = s[:-1] + "+00:00" if s.endswith("Z") else s
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(validate_tz(tz)))
    return dt.astimezone(timezone.utc)


def next_cron_fire(expr: str, after: datetime, tz: str = DEFAULT_TZ) -> datetime:
    """Next fire time for a cron expr, evaluated against wall-clock in `tz`.

    Returns an aware UTC datetime.

    The tz round-trip is the whole point (G4): the expression describes a
    *wall-clock* moment, so it must be evaluated in the user's zone and only
    then converted back to the UTC instant the scheduler compares against.
    Evaluating directly in UTC — what this did before — makes "0 8 * * *"
    fire at 16:00 HKT with no error anywhere.

    One implementation on purpose. This used to prefer croniter and fall back
    to _basic_cron on ImportError. But croniter is not a declared dependency,
    so *which branch runs depended on whether the package happened to be
    installed on that host* — and the daemon runs on three of them. All three
    are croniter-free today, so the fallback is what has always executed; that
    consistency was luck, not design. The day one host picks croniter up as a
    transitive dependency, it starts computing different fire times than its
    peers, silently. _basic_cron is covered by the L1 suite (T101-T108: leap
    day, cross-year, MON-FRI, month-start, steps), so the optional branch
    bought nothing and risked divergence.
    """
    zone = ZoneInfo(validate_tz(tz))
    return _basic_cron(expr, after.astimezone(zone), zone)


_DOW = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}


def _expand(field, lo, hi, aliases=None):
    out = set()
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/")
            step = int(step)
            if step <= 0:
                raise ValueError(f"step must be positive in {part!r}")
        else:
            base, step = part, 1
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-")
            start = aliases.get(a.upper(), None) if aliases and not a.isdigit() else int(a)
            end = aliases.get(b.upper(), None) if aliases and not b.isdigit() else int(b)
            if start is None or end is None:
                raise ValueError(f"bad alias in {base}")
        else:
            start = end = aliases.get(base.upper(), None) if aliases and not base.isdigit() else int(base)
            if start is None:
                raise ValueError(f"bad alias {base}")
        if start > end:
            raise ValueError(f"range {base!r} is inverted")
        for v in range(start, end + 1, step):
            # Out-of-range values used to be accepted silently: "0 25 * * *"
            # produced hour={25}, matched nothing, and the scan returned None
            # — a bad expression became "never fires" instead of an error.
            if not (lo <= v <= hi):
                raise ValueError(f"value {v} out of range [{lo},{hi}] in field {field!r}")
            out.add(v)
    if not out:
        raise ValueError(f"field {field!r} expands to nothing")
    return out


def _basic_cron(expr: str, after: datetime, zone: ZoneInfo | None = None) -> datetime:
    """Minute-scan cron evaluator. `after` must already be in the target zone."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron expr needs 5 fields: {expr!r}")
    mins = _expand(parts[0], 0, 59)
    hrs = _expand(parts[1], 0, 23)
    doms = _expand(parts[2], 1, 31)
    months = _expand(parts[3], 1, 12)
    dows = _expand(parts[4], 0, 6, _DOW)
    if zone is not None:
        after = after.astimezone(zone)
    # Two-level scan: skip a whole day at a time when the *date* can't match,
    # only walk minutes within a matching day. A naive minute-by-minute scan
    # over the 4-year horizon needed for "0 8 29 2 *" would be ~2.1M
    # iterations; this is ~1.5K days + at most 1440 minutes per candidate day.
    t = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    horizon = after + timedelta(days=366 * 4 + 1)
    while t <= horizon:
        if not (t.day in doms and t.month in months and (t.weekday() + 1) % 7 in dows):
            # Date can't match — jump to next local midnight.
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if t.hour in hrs and t.minute in mins:
            return t.astimezone(timezone.utc)
        t += timedelta(minutes=1)
        if t.hour == 0 and t.minute == 0:
            continue  # rolled into a new day; re-check the date predicate
    # Scanning four years without a hit means the expression can never fire —
    # "0 8 31 2 *" (February 31st) passes every per-field range check and then
    # matches no date, ever. Returning None made that a *silent* dead job:
    # cmd_add happened to check, but the tz migration wrote `fire_at: None`
    # straight to Firestore and claim_job then skipped it forever with no
    # error status. Raise instead, so every call site has to face it.
    raise ValueError(f"cron expr never fires within 4 years: {expr!r}")


def cmd_add(args):
    sender = os.environ.get("BOT_NAME", "unknown")
    if sum([bool(args.in_), bool(args.at), bool(args.cron)]) != 1:
        print(json.dumps({"error": "exactly one of --in / --at / --cron required"}))
        sys.exit(2)

    try:
        tz = validate_tz(args.tz or DEFAULT_TZ)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(2)

    try:
        if args.in_:
            fire_at, kind, cron_expr = parse_in(args.in_), "oneshot", None
        elif args.at:
            fire_at, kind, cron_expr = parse_at(args.at, tz), "oneshot", None
        else:
            fire_at = next_cron_fire(args.cron, NOW(), tz)
            kind, cron_expr = "recurring", args.cron
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(2)

    if fire_at <= NOW():
        print(json.dumps({"error": f"fire_at {fire_at.isoformat()} is in the past"}))
        sys.exit(2)

    job_id = uuid.uuid4().hex[:12]
    doc = {
        "job_id": job_id,
        "kind": kind,
        "cron": cron_expr,
        "tz": tz if cron_expr else None,
        "fire_at": fire_at,
        "target": args.target,
        "sender": sender,
        "message": args.message,
        "status": "scheduled",
        "created_at": NOW(),
        "last_fired_at": None,
        "fire_count": 0,
    }
    db().collection(COLL).document(job_id).set(doc)
    print(
        json.dumps(
            {
                "job_id": job_id,
                "target": args.target,
                "fire_at_hkt": _hkt(fire_at),
                "fire_at_utc": fire_at.isoformat(),
                "in_seconds": int((fire_at - NOW()).total_seconds()),
                "kind": kind,
                "cron": cron_expr,
                "tz": tz if cron_expr else None,
            }
        )
    )


def cmd_list(args):
    q = db().collection(COLL).where("status", "==", "scheduled")
    sender = os.environ.get("BOT_NAME")
    docs = []
    for d in q.stream():
        x = d.to_dict()
        if args.target and x.get("target") != args.target:
            continue
        if not args.all and sender and x.get("sender") != sender:
            continue
        docs.append(x)
    docs.sort(key=lambda d: d.get("fire_at") or NOW())
    out = []
    for x in docs[:50]:
        out.append(
            {
                "job_id": x["job_id"],
                "target": x["target"],
                "sender": x["sender"],
                "kind": x["kind"],
                # HKT first, on purpose: every human-facing time in this project
                # is HKT, and showing UTC first is how "0 8 * * *" got authored
                # as UTC in the first place.
                "fire_at_hkt": _hkt(x.get("fire_at")),
                "fire_at_utc": x["fire_at"].isoformat() if x.get("fire_at") else None,
                "cron": x.get("cron"),
                "tz": x.get("tz"),
                "message": (x.get("message") or "")[:80],
            }
        )
    print(json.dumps({"jobs": out, "count": len(out)}, ensure_ascii=False))


def cmd_remove(args):
    ref = db().collection(COLL).document(args.job_id)
    snap = ref.get()
    if not snap.exists:
        print(json.dumps({"error": f"job {args.job_id} not found"}))
        sys.exit(1)
    ref.update({"status": "cancelled"})
    print(json.dumps({"job_id": args.job_id, "status": "cancelled"}))


def _shift_expr_utc_to_hkt(expr: str) -> tuple[str | None, str]:
    """Rewrite a UTC-authored cron expr into the equivalent HKT one.

    Only handles the unambiguous case: a single literal hour that stays inside
    the same day after +8. Anything else (wildcards, lists, ranges, steps, or a
    shift that crosses midnight and would drag the day-of-week/day-of-month
    fields along) is refused rather than guessed — a wrong guess here silently
    moves a live job by hours.
    """
    parts = expr.split()
    if len(parts) != 5:
        return None, f"expr does not have 5 fields: {expr!r}"
    hour = parts[1]
    if not hour.isdigit():
        return None, f"hour field {hour!r} is not a single literal; migrate by hand"
    h = int(hour) + 8
    if h >= 24:
        return None, (
            f"hour {hour} + 8 crosses midnight; day-of-week/day-of-month would "
            "also need shifting. Migrate by hand."
        )
    parts[1] = str(h)
    return " ".join(parts), ""


def _exprs_equivalent(old_expr: str, new_expr: str, samples: int = 60) -> tuple[bool, str]:
    """T401 — the golden criterion for migration correctness.

    Evaluate the old expression the old way (UTC) and the new expression the
    new way (HKT) against the same frozen instants. Every pair must match.
    This catches far more than eyeballing "13 0 became 13 8" would.
    """
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(samples):
        now = base + timedelta(days=7 * i, hours=(i * 7) % 24, minutes=(i * 13) % 60)
        try:
            old = next_cron_fire(old_expr, now, "UTC")
            new = next_cron_fire(new_expr, now, "Asia/Hong_Kong")
        except ValueError as e:
            # 算不出来就不能声称等价。这以前是最阴的一条：两条永不触发的表达式
            # 双双得到 None，None == None 判成「等价」，迁移照做，然后往库里写
            # 一个 fire_at=None 的死 job。
            return False, f"cannot evaluate at now={now.isoformat()}: {e}"
        if old != new:
            return False, (
                f"mismatch at now={now.isoformat()}: "
                f"old({old_expr}/UTC)={old} new({new_expr}/HKT)={new}"
            )
    return True, f"{samples}/{samples} sampled instants identical"


def cmd_migrate_tz(args):
    """One-off G4 migration. Preview by default; --apply to write."""
    d = db()
    report = []
    for snap in d.collection(COLL).where("status", "==", "scheduled").stream():
        x = snap.to_dict()
        if not x.get("cron") or x.get("tz"):
            continue  # not a cron job, or already migrated
        old_expr = x["cron"]
        new_expr, why = _shift_expr_utc_to_hkt(old_expr)
        entry = {"job_id": x["job_id"], "old_cron": old_expr, "target": x.get("target")}
        if not new_expr:
            entry.update({"action": "MANUAL", "reason": why})
            report.append(entry)
            continue
        ok, detail = _exprs_equivalent(old_expr, new_expr)
        entry.update({"new_cron": new_expr, "equivalent": ok, "check": detail})
        if not ok:
            entry["action"] = "REFUSED"
        elif args.apply:
            # Atomic: expression and tz move together. Writing tz alone would
            # reinterpret "0" as 0am HKT and shift the job by 8 hours.
            try:
                nxt = next_cron_fire(new_expr, NOW(), "Asia/Hong_Kong")
            except ValueError as e:
                # 一条 job 算不出来不该打断整批迁移，也绝不能写进去 —— 写了就是
                # 一个 fire_at 不可用的死 job，claim 会永远跳过它且不报错。
                entry["action"] = f"FAILED: {e}"
                report.append(entry)
                continue
            snap.reference.update(
                {"cron": new_expr, "tz": "Asia/Hong_Kong", "fire_at": nxt}
            )
            entry["action"] = "MIGRATED"
        else:
            entry["action"] = "would migrate (pass --apply)"
        report.append(entry)
    print(json.dumps({"jobs": report, "count": len(report)}, ensure_ascii=False, default=str))


def compute_fire_update(job: dict, now: datetime) -> tuple[dict, str | None]:
    """State transition for one fired job. Pure — no I/O, so it's unit-testable.

    Returns (update_dict, error). Recurring jobs get a recomputed fire_at;
    one-shots are marked done.
    """
    upd = {"last_fired_at": now, "fire_count": (job.get("fire_count") or 0) + 1}
    if job.get("kind") == "recurring" and job.get("cron"):
        tz = job.get("tz") or LEGACY_TZ
        try:
            nxt = next_cron_fire(job["cron"], now, tz)
        except Exception as e:
            return {**upd, "status": "error"}, str(e)
        upd["fire_at"] = nxt
    else:
        upd["status"] = "done"
    return upd, None


def claim_job(db_client, job_id: str, cutoff: datetime, now: datetime):
    """Atomically claim one due job. Returns the job dict if we won, else None.

    Why a transaction: cron-daemon runs on several hosts against one shared
    Firestore collection, with no leader election. The old read-then-write
    let two daemons that ticked within the same second both see the same
    fire_at, both dispatch, and both advance it — one due job, two LLM turns.
    We got lucky (118 historical messages, no observed duplicate) because the
    race window is ~1s out of a 30s period, but luck is not a design.

    Reading fire_at and advancing it inside one transaction makes the claim
    the serialization point: the loser re-reads the already-advanced fire_at
    and skips. Multiple daemons then become a redundancy feature instead of a
    hazard — no host is special, and losing one doesn't stop the schedule.
    """
    ref = db_client.collection(COLL).document(job_id)
    transaction = db_client.transaction()

    @firestore.transactional
    def _claim(tx):
        snap = ref.get(transaction=tx)
        if not snap.exists:
            return None
        x = snap.to_dict() or {}
        # Re-check every precondition inside the transaction; the state may
        # have moved between the outer query and here.
        if x.get("status") != "scheduled":
            return None
        fa = x.get("fire_at")
        if not fa or fa > cutoff:
            return None
        upd, _err = compute_fire_update(x, now)
        tx.update(ref, upd)
        return x

    return _claim(transaction)


def build_instruction(job: dict) -> str:
    """Render the inbox instruction for one fired job.

    The job_id MUST appear in the body (G1). Without it a recurring job has no
    way to retire itself: the agent can decide "found it, we're done" but has
    no handle to act on, so every self-terminating task degrades into one a
    human has to remember to remove.
    """
    job_id = job["job_id"]
    lines = [f"[⏰ 定时提醒] {job.get('message', '')}"]
    if job.get("kind") == "recurring":
        lines += [
            "",
            f"[本任务 job_id={job_id}｜第 {(job.get('fire_count') or 0) + 1} 次触发]",
            f"目标达成后请自行收工：`python3 ~/CloseCrab/scripts/cron-tool.py remove {job_id}`",
            "未达成则本轮什么都不必回复，等下一次触发即可。",
        ]
    return "\n".join(lines)


def cmd_tick(args):
    """Run by daemon. Fire all due scheduled jobs and garbage-collect old done."""
    dry = getattr(args, "dry_run", False)
    fired, skipped_test, lost = [], 0, 0
    d = db()
    cutoff = NOW()
    swept = 0
    if not dry:
        # Sweep: delete done/cancelled/error jobs older than 7 days to keep
        # collection bounded.
        sweep_before = cutoff - timedelta(days=7)
        for snap in (
            d.collection(COLL).where("status", "in", ["done", "cancelled", "error"]).stream()
        ):
            x = snap.to_dict()
            last = x.get("last_fired_at") or x.get("created_at")
            if last and last < sweep_before:
                snap.reference.delete()
                swept += 1

    q = d.collection(COLL).where("status", "==", "scheduled")
    for snap in q.stream():
        x = snap.to_dict()
        # Test fixtures never fire in the real loop. Tests write jobs carrying
        # test_run_id; without this guard a stray fixture would spam a live bot.
        # --dry-run still shows them: it writes nothing, and inspecting fixtures
        # is precisely what it's for.
        if x.get("test_run_id") and not dry:
            skipped_test += 1
            continue
        fa = x.get("fire_at")
        if not fa or fa > cutoff:
            continue

        # LEGACY_TZ, not DEFAULT_TZ. A job with no `tz` field was authored
        # before G4 was fixed, i.e. its expression was hand-compensated into
        # UTC ("0" meaning 08:00 HKT). Reading it as HKT would silently move it
        # 8 hours the moment this code ships — the very bug we're fixing, with
        # the sign flipped. Legacy jobs keep UTC semantics until `migrate-tz`
        # rewrites expression and tz together.
        tz = x.get("tz") or LEGACY_TZ

        if dry:
            upd, err = compute_fire_update(x, NOW())
            fired.append(
                {
                    "job_id": x["job_id"],
                    "target": x["target"],
                    "due_hkt": _hkt(fa),
                    "next_hkt": _hkt(upd.get("fire_at")),
                    "tz": tz,
                    "error": err,
                    "instruction_preview": build_instruction(x)[:160],
                }
            )
            continue

        # Claim before dispatching. If another daemon got here first this
        # returns None and we move on without sending anything.
        won = claim_job(d, x["job_id"], cutoff, NOW())
        if won is None:
            lost += 1
            continue

        d.collection("messages").add(
            {
                "from": won.get("sender", "cron"),
                "to": won["target"],
                "instruction": build_instruction(won),
                "task_id": f"cron-{won['job_id']}",
                "status": "pending",
                "result": "",
                "created_at": NOW(),
            }
        )
        fired.append(won["job_id"])

    out = {"fired": fired, "count": len(fired)}
    if dry:
        out["dry_run"] = True
    if swept:
        out["swept"] = swept

    # messages 集合的 GC 挂在这个心跳上：它是全局唯一的 housekeeping 时机，
    # 而每个 bot 各扫一遍同一张表纯属浪费。函数内部自带 1 小时节流。
    if not dry:
        try:
            from closecrab.utils.firestore_inbox import sweep_messages

            ms = sweep_messages(d)
            if ms and not ms.get("skipped") and any(
                ms.get(k) for k in ("done", "dead_letter", "orphan_processing")
            ):
                out["messages_swept"] = ms
        except Exception as e:
            out["messages_sweep_error"] = str(e)[:120]
    if skipped_test:
        out["skipped_test_fixtures"] = skipped_test
    if lost:
        # Another daemon claimed these first — expected, not an error.
        out["lost_race"] = lost
    print(json.dumps(out, ensure_ascii=False, default=str))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", aliases=["create"])
    a.add_argument("--target", required=True, help="target bot name")
    a.add_argument("--in", dest="in_", help="relative delay: 10m/2h/90s/3d")
    a.add_argument(
        "--at",
        help="ISO time; bare timestamps are read in --tz (default HKT), "
        "not UTC: 2026-05-17T15:00:00",
    )
    a.add_argument("--cron", help='cron expr "M H DOM MON DOW", evaluated in --tz')
    a.add_argument(
        "--tz",
        default=DEFAULT_TZ,
        help=f"wall-clock zone for --cron / bare --at. One of: {' / '.join(TZ_ALLOWED)}. "
        f"Default {DEFAULT_TZ}.",
    )
    a.add_argument("--message", required=True, help="reminder text")
    a.set_defaults(fn=cmd_add)

    l = sub.add_parser("list")
    l.add_argument("--target", help="filter by target bot")
    l.add_argument("--all", action="store_true", help="show all senders (not just current bot)")
    l.set_defaults(fn=cmd_list)

    r = sub.add_parser("remove", aliases=["stop", "rm"])
    r.add_argument("job_id")
    r.set_defaults(fn=cmd_remove)

    t = sub.add_parser("tick")
    t.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would fire and each job's recomputed next time, "
        "without writing to inbox or mutating job state",
    )
    t.set_defaults(fn=cmd_tick)

    m = sub.add_parser(
        "migrate-tz",
        help="one-off: move UTC-authored cron exprs onto explicit tz (G4). "
        "Verifies equivalence before writing.",
    )
    m.add_argument("--apply", action="store_true", help="write changes (default: preview only)")
    m.set_defaults(fn=cmd_migrate_tz)

    p = sub.add_parser("principles", help="print the 5 cron scheduling principles")
    p.set_defaults(fn=lambda _args: print(PRINCIPLES))

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
