#!/usr/bin/env python3
"""cron-daemon.py — Tick scheduled_jobs every 30s. Lightweight singleton.

Pidfile: /tmp/closecrab-cron-daemon.pid
Usage:
  python3 cron-daemon.py start    # foreground
  python3 cron-daemon.py status
  python3 cron-daemon.py stop
"""
import os, sys, time, signal, subprocess
from pathlib import Path

PID = Path("/tmp/closecrab-cron-daemon.pid")
SCRIPT = Path(__file__).resolve().parent / "cron-tool.py"
INTERVAL = 30
LOG = Path.home() / ".claude" / "closecrab" / "cron-daemon.log"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def cmd_status():
    if PID.exists():
        pid = int(PID.read_text().strip())
        print(f"running pid={pid} alive={alive(pid)}")
    else:
        print("not running")


def cmd_stop():
    if not PID.exists():
        print("not running")
        return
    pid = int(PID.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"sent SIGTERM to {pid}")
    except ProcessLookupError:
        pass
    PID.unlink(missing_ok=True)


def cmd_start():
    if PID.exists():
        pid = int(PID.read_text().strip())
        if alive(pid):
            print(f"already running pid={pid}")
            return
        PID.unlink(missing_ok=True)
    PID.write_text(str(os.getpid()))
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log = LOG.open("a", buffering=1)
    log.write(f"\n=== cron-daemon started pid={os.getpid()} at {time.strftime('%F %T')} ===\n")
    try:
        while True:
            t0 = time.time()
            try:
                r = subprocess.run(
                    ["python3", str(SCRIPT), "tick"],
                    capture_output=True, text=True, timeout=60,
                )
                if r.stdout.strip() and r.stdout.strip() != '{"fired": [], "count": 0}':
                    log.write(f"[{time.strftime('%F %T')}] {r.stdout.strip()}\n")
                if r.returncode != 0 and r.stderr:
                    log.write(f"[{time.strftime('%F %T')}] STDERR: {r.stderr[:200]}\n")
            except subprocess.TimeoutExpired:
                log.write(f"[{time.strftime('%F %T')}] tick timeout\n")
            except Exception as e:
                log.write(f"[{time.strftime('%F %T')}] tick error: {e}\n")
            time.sleep(max(0, INTERVAL - (time.time() - t0)))
    finally:
        log.write(f"=== cron-daemon stopped at {time.strftime('%F %T')} ===\n")
        log.close()
        PID.unlink(missing_ok=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    {"start": cmd_start, "status": cmd_status, "stop": cmd_stop}.get(cmd, cmd_status)()
