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
_HERE = Path(__file__).resolve().parent
# 两个调度器共用一个心跳，不各起一份 daemon：省一个进程，也保证两者的
# tick 节奏一致。cron-tool 管定时提醒，watch-task 管盯长跑任务。
SCRIPTS = [_HERE / "cron-tool.py", _HERE / "watch-task.py"]
SCRIPT = SCRIPTS[0]  # 兼容旧引用
INTERVAL = 30
# watch-task 的探针要跑 sub-agent（十几秒到数分钟），不能跟 cron tick
# 共用 60s 超时，否则会被腰斩在半路。
TIMEOUTS = {"cron-tool.py": 60, "watch-task.py": 600}
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
            for script in SCRIPTS:
                if not script.exists():
                    continue  # 部署未同步的机器上跳过，不要整个 tick 崩掉
                name = script.name
                try:
                    r = subprocess.run(
                        ["python3", str(script), "tick"],
                        capture_output=True, text=True, timeout=TIMEOUTS.get(name, 60),
                    )
                    out = r.stdout.strip()
                    # 空转是常态（多数 tick 什么都不到期），只记有内容的
                    if out and '"count": 0' not in out:
                        log.write(f"[{time.strftime('%F %T')}] {name}: {out}\n")
                    # 退出码 0 也要记 stderr：播报出口是 best-effort 的，失败只打一行
                    # warn 就照常返回 0。只在非零时记，等于把「飞书发出去了但语音没推
                    # 成」这类半失败全吞掉 —— 事后分不清是探针判了 SKIP 还是出口挂了。
                    if r.stderr:
                        log.write(f"[{time.strftime('%F %T')}] {name} STDERR: {r.stderr[:400]}\n")
                except subprocess.TimeoutExpired:
                    log.write(f"[{time.strftime('%F %T')}] {name} tick timeout\n")
                except Exception as e:
                    log.write(f"[{time.strftime('%F %T')}] {name} tick error: {e}\n")
            time.sleep(max(0, INTERVAL - (time.time() - t0)))
    finally:
        log.write(f"=== cron-daemon stopped at {time.strftime('%F %T')} ===\n")
        log.close()
        PID.unlink(missing_ok=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    {"start": cmd_start, "status": cmd_status, "stop": cmd_stop}.get(cmd, cmd_status)()
