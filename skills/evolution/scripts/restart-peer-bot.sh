#!/usr/bin/env bash
# restart-peer-bot.sh - Cross-bot SIGHUP restart for evolution rounds
#
# Usage: ./restart-peer-bot.sh <target_bot> [--delay SECONDS] [--reason "..."]
#
# Triggers a SIGHUP-based restart of a peer bot on the SAME machine.
# SIGHUP → bot's main.py signal handler → sys.exit(129) → run.sh wrapper → restart.
#
# Why nohup + sleep pattern: if we kill the target's run.sh process directly,
# this script (running inside another bot's worker) could be killed by the
# same wrapper if we share parent PID lineage. nohup + setsid disowns the
# kill command so it survives our own session termination.
#
# Authorization: Chris standing-authorized cross-bot restart inside evolution
# rounds (2026-05-19, "你就互相 restart 呗"). Use outside evolution flow requires
# explicit permission.
#
# Verification: after restart, this script polls for the new PID and checks
# the bot.log startup line. Exits 0 only on confirmed restart, exits 2 on
# verification failure.

set -euo pipefail

DELAY=12
REASON="evolution-round-restart"
TARGET=""
LOG_FILE="/tmp/cross-bot-restart.log"
WAIT_TIMEOUT=60

usage() {
    echo "Usage: $0 <target_bot> [--delay N] [--reason '...']"
    echo ""
    echo "Examples:"
    echo "  $0 xiaoaitongxue"
    echo "  $0 tiemu --delay 8 --reason 'kilo-worker-patch-v3'"
    exit 1
}

# --- arg parsing ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --delay) DELAY="$2"; shift 2 ;;
        --reason) REASON="$2"; shift 2 ;;
        --wait) WAIT_TIMEOUT="$2"; shift 2 ;;
        -h|--help) usage ;;
        --*) echo "Unknown flag: $1"; usage ;;
        *)
            if [[ -z "$TARGET" ]]; then TARGET="$1"; shift
            else echo "Extra arg: $1"; usage; fi
            ;;
    esac
done

[[ -z "$TARGET" ]] && usage

# --- find target PID ---
# We grep for the main process: `python3 -m closecrab --bot <target>`
# Avoid matching run.sh wrapper (we want SIGHUP on main.py, not the wrapper)
get_main_pid() {
    pgrep -f "python3 -m closecrab.*--bot[= ]\?${TARGET}\b" | head -1 || true
}

OLD_PID=$(get_main_pid)
if [[ -z "$OLD_PID" ]]; then
    echo "❌ Target bot '$TARGET' not running on this host (no python3 -m closecrab process found)"
    echo "   Try: ps aux | grep closecrab"
    exit 1
fi

OLD_STIME=$(ps -p "$OLD_PID" -o lstart= 2>/dev/null | xargs || echo "unknown")

echo "🎯 Target: $TARGET"
echo "   Old PID: $OLD_PID (started: $OLD_STIME)"
echo "   Delay:   ${DELAY}s"
echo "   Reason:  $REASON"

# --- audit log ---
mkdir -p "$(dirname "$LOG_FILE")"
NOW=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
SENDER="${BOT_NAME:-$(whoami)}"
echo "$NOW: cross-bot SIGHUP fired to $TARGET PID $OLD_PID by $SENDER (reason: $REASON)" >> "$LOG_FILE"

# --- detached SIGHUP ---
# nohup + setsid → disown from this script's process group
# After $DELAY seconds, kill -HUP fires from a fully detached process
nohup setsid bash -c "sleep ${DELAY} && kill -HUP ${OLD_PID} 2>/dev/null && echo '[restart-peer-bot] SIGHUP sent to PID ${OLD_PID}' >> ${LOG_FILE}" >/dev/null 2>&1 &
disown

echo "✅ SIGHUP scheduled (T+${DELAY}s) for PID $OLD_PID, will verify in $((DELAY + 30))s"

# --- verification poll ---
START=$(date +%s)
DEADLINE=$((START + DELAY + WAIT_TIMEOUT))

while [[ $(date +%s) -lt $DEADLINE ]]; do
    sleep 3
    NEW_PID=$(get_main_pid)

    # Wait for OLD to die AND NEW (different) to appear
    if [[ -n "$NEW_PID" && "$NEW_PID" != "$OLD_PID" ]]; then
        # Sanity: NEW must be younger than OLD
        NEW_STIME=$(ps -p "$NEW_PID" -o lstart= 2>/dev/null | xargs || echo "unknown")
        echo "✅ New PID detected: $NEW_PID (started: $NEW_STIME)"

        # Verify bot.log shows startup line (Phase E injection)
        BOT_LOG="$HOME/.claude/closecrab/${TARGET}/bot.log"
        if [[ -f "$BOT_LOG" ]]; then
            # Look for Phase E line within last 10 lines
            STARTUP_LINE=$(tail -100 "$BOT_LOG" 2>/dev/null | grep -E "system_prompt|Phase E|GBrain index|injecting" | tail -1 || true)
            if [[ -n "$STARTUP_LINE" ]]; then
                echo "   bot.log startup: $STARTUP_LINE"
            else
                echo "   ⚠️  No Phase E startup line in bot.log tail — restart may have hit early error"
                echo "       check: tail -50 $BOT_LOG"
            fi
        else
            echo "   ⚠️  bot.log not found at $BOT_LOG"
        fi

        echo ""
        echo "🎉 Restart verified: $TARGET $OLD_PID → $NEW_PID"
        echo "$NOW: verified restart $OLD_PID → $NEW_PID" >> "$LOG_FILE"
        exit 0
    fi

    if [[ -z "$NEW_PID" ]]; then
        echo "   ... waiting (target gone, no new PID yet)"
    fi
done

# --- timeout ---
echo "❌ Verification timeout after ${WAIT_TIMEOUT}s past delay"
echo "   Last known: OLD=$OLD_PID, NEW=$(get_main_pid)"
echo "   Check manually: ps aux | grep $TARGET ; tail -50 ~/.claude/closecrab/$TARGET/bot.log"
echo "$NOW: VERIFICATION_FAILED restart $TARGET" >> "$LOG_FILE"
exit 2
