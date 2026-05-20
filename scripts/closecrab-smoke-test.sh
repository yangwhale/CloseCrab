#!/usr/bin/env bash
# CloseCrab post-restart smoke test
#
# Usage:
#   closecrab-smoke-test.sh <bot_name>          # check single bot
#   closecrab-smoke-test.sh --all               # every bot with a process here
#   closecrab-smoke-test.sh <bot> --quiet       # only summary line
#   closecrab-smoke-test.sh <bot> --json        # machine-readable (implies --quiet)
#   closecrab-smoke-test.sh <bot> --json --actions  # JSON + paste-ready fix cmds
#
# Exit code = number of failed checks (0 = all pass)
# Drop-in extensions: ~/.closecrab/smoke-tests.d/*.sh         (all bots)
#                     ~/.closecrab/smoke-tests.d/<bot>/*.sh   (per-bot)
#
# Inspired by gbrain skills/{smoke-test,skillpack-check}
# v1: detect-only (suggest actions; never auto-fix running bots)

set -u
LC_ALL=C.UTF-8

# ---- args ----
QUIET=0
BOT=""
MODE="single"
JSON=0
ACTIONS=0
for arg in "$@"; do
    case "$arg" in
        --quiet|-q) QUIET=1 ;;
        --json)     JSON=1; QUIET=1 ;;
        --actions)  ACTIONS=1 ;;
        --all)      MODE="all" ;;
        -*) echo "unknown flag: $arg" >&2; exit 2 ;;
        *) BOT="$arg" ;;
    esac
done

if [ "$MODE" = "single" ] && [ -z "$BOT" ]; then
    cat <<EOF
usage: closecrab-smoke-test.sh <bot_name> [--quiet] [--json]
       closecrab-smoke-test.sh --all     [--quiet] [--json]
EOF
    exit 2
fi

# ---- color / output ----
if [ -t 1 ] && [ "$QUIET" = "0" ]; then
    C_PASS=$'\033[32m'; C_FAIL=$'\033[31m'; C_WARN=$'\033[33m'
    C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
    C_PASS=""; C_FAIL=""; C_WARN=""; C_DIM=""; C_RST=""
fi

# ---- counters & results buffer (used by helpers) ----
PASS_N=0
FAIL_N=0
SKIP_N=0
RESULTS_JSON=""   # comma-separated json objects
ACTIONS_JSON=""   # comma-separated json action objects {check, cmd, reason}

# Append an actionable remediation for a failed check.
# Args: check_name, fix_command, reason
_suggest() {
    [ "$ACTIONS" = "1" ] || return 0
    local check="$1" cmd="$2" reason="$3"
    local c_esc r_esc cmd_esc
    c_esc=$(printf '%s' "$check"  | sed 's/\\/\\\\/g; s/"/\\"/g')
    cmd_esc=$(printf '%s' "$cmd"  | sed 's/\\/\\\\/g; s/"/\\"/g')
    r_esc=$(printf '%s' "$reason" | sed 's/\\/\\\\/g; s/"/\\"/g')
    local entry; entry=$(printf '{"check":"%s","cmd":"%s","reason":"%s"}' \
            "$c_esc" "$cmd_esc" "$r_esc")
    if [ -z "$ACTIONS_JSON" ]; then ACTIONS_JSON="$entry"
    else ACTIONS_JSON="$ACTIONS_JSON,$entry"; fi
}

_emit() {
    # $1=status (pass/fail/skip)  $2=name  $3=detail
    local status="$1" name="$2" detail="$3"
    case "$status" in
        pass) PASS_N=$((PASS_N+1));
              [ "$QUIET" = "0" ] && printf "  %s✓%s %-32s %s%s%s\n" \
                  "$C_PASS" "$C_RST" "$name" "$C_DIM" "$detail" "$C_RST" ;;
        fail) FAIL_N=$((FAIL_N+1));
              [ "$QUIET" = "0" ] && printf "  %s✗%s %-32s %s\n" \
                  "$C_FAIL" "$C_RST" "$name" "$detail" ;;
        skip) SKIP_N=$((SKIP_N+1));
              [ "$QUIET" = "0" ] && printf "  %s⊘%s %-32s %s%s%s\n" \
                  "$C_WARN" "$C_RST" "$name" "$C_DIM" "$detail" "$C_RST" ;;
    esac
    if [ "$JSON" = "1" ]; then
        local d_esc; d_esc=$(printf '%s' "$detail" | sed 's/\\/\\\\/g; s/"/\\"/g')
        local entry; entry=$(printf '{"name":"%s","status":"%s","detail":"%s"}' \
                "$name" "$status" "$d_esc")
        if [ -z "$RESULTS_JSON" ]; then RESULTS_JSON="$entry"
        else RESULTS_JSON="$RESULTS_JSON,$entry"; fi
    fi
}

pass() { _emit pass "$1" "${2:-OK}"; }
fail() { _emit fail "$1" "${2:-FAILED}"; }
skip() { _emit skip "$1" "${2:-skipped}"; }

# ---- helpers ----
firestore_get() {
    # $1=bot $2=dotted.path
    # Output line 1: status   (EXISTS | NOTFOUND | ERROR:<msg>)
    # Output line 2: value at path, or empty if missing
    timeout 8 python3 - "$1" "$2" <<'PY' 2>/dev/null
import json, os, sys
try:
    from google.cloud import firestore  # type: ignore
except Exception as e:
    print(f"ERROR:import:{e}"); sys.exit(0)
bot, path = sys.argv[1], sys.argv[2]
project = os.environ.get("FIRESTORE_PROJECT", "chris-pgp-host")
database = os.environ.get("FIRESTORE_DATABASE", "closecrab")
try:
    db = firestore.Client(project=project, database=database)
    doc = db.collection("bots").document(bot).get()
except Exception as e:
    print(f"ERROR:{type(e).__name__}:{e}"); sys.exit(0)
if not doc.exists:
    print("NOTFOUND"); sys.exit(0)
print("EXISTS")
if not path:
    sys.exit(0)
data = doc.to_dict() or {}
for key in path.split("."):
    if not isinstance(data, dict) or key not in data:
        sys.exit(0)
    data = data[key]
if isinstance(data, (dict, list)):
    print(json.dumps(data, ensure_ascii=False))
elif data is not None:
    print(data)
PY
}

_fs_status()  { echo "$1" | sed -n '1p'; }
_fs_value()   { echo "$1" | sed -n '2p'; }

# ---- the actual checks ----
check_bot_process() {
    local bot="$1"
    local pids; pids=$(pgrep -f "python3 -m closecrab.*--bot ${bot}([^a-zA-Z]|$)" 2>/dev/null)
    if [ -n "$pids" ]; then
        local pid; pid=$(echo "$pids" | head -1)
        local rss; rss=$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')
        pass bot_process "pid=$pid rss=${rss}KB"
    else
        fail bot_process "no closecrab process for bot=$bot"
        _suggest bot_process \
            "cd ~/CloseCrab && nohup ./run.sh $bot > /tmp/${bot}.run.log 2>&1 &" \
            "Bot process not running; relaunch via run.sh supervisor."
    fi
}

check_run_sh() {
    local bot="$1"
    if pgrep -f "run\.sh ${bot}([^a-zA-Z]|$)" >/dev/null 2>&1; then
        pass run_sh_wrapper "supervisor present"
    else
        skip run_sh_wrapper "no run.sh wrapper (manual launch?)"
    fi
}

check_firestore_sa() {
    local key="${GOOGLE_APPLICATION_CREDENTIALS:-}"
    if [ -z "$key" ]; then
        # fall through — Vertex bots may use ADC, gLinux uses gcert
        skip firestore_sa_key "GOOGLE_APPLICATION_CREDENTIALS unset (ADC?)"
        return
    fi
    if [ ! -r "$key" ]; then
        fail firestore_sa_key "file not readable: $key"
        return
    fi
    if python3 -c "import json; json.load(open('$key'))" 2>/dev/null; then
        pass firestore_sa_key "$key"
    else
        fail firestore_sa_key "JSON parse failed: $key"
    fi
}

check_firestore_reachable() {
    local bot="$1"
    local out; out=$(firestore_get "$bot" "" 2>/dev/null)
    local st; st=$(_fs_status "$out")
    case "$st" in
        EXISTS)   pass firestore_reachable "bots/$bot doc exists" ;;
        NOTFOUND) fail firestore_reachable "bots/$bot doc missing"
                  _suggest firestore_reachable \
                      "python3 ~/CloseCrab/scripts/config-manage.py add $bot --channel feishu" \
                      "No bot config in Firestore -- needs initial registration." ;;
        ERROR:*)  fail firestore_reachable "${st#ERROR:}"
                  _suggest firestore_reachable \
                      "gcloud auth application-default login --project=chris-pgp-host" \
                      "Firestore client failed -- credentials or network." ;;
        *)        fail firestore_reachable "empty response (timeout?)" ;;
    esac
}

check_worker_type() {
    local bot="$1"
    local out; out=$(firestore_get "$bot" "worker_type")
    local wt; wt=$(_fs_value "$out")
    [ -z "$wt" ] && wt="claude"
    WORKER_TYPE="$wt"
    pass worker_type "$wt"
}

check_claude_settings() {
    local f="${HOME}/.claude/settings.json"
    if [ ! -f "$f" ]; then
        skip claude_settings "no $f"
        return
    fi
    if python3 -c "import json; json.load(open('$f'))" 2>/dev/null; then
        local nperm; nperm=$(python3 -c \
            "import json; d=json.load(open('$f')); p=d.get('permissions',{}).get('allow',[]); print(len(p))" 2>/dev/null)
        pass claude_settings "valid, ${nperm:-0} allow rules"
    else
        fail claude_settings "JSON parse failed: $f"
    fi
}

check_worker_secrets() {
    local bot="$1" wt="$2"
    case "$wt" in
        claude|ClaudeCodeWorker)
            # Vertex (default) needs ANTHROPIC_VERTEX_PROJECT_ID; direct API needs ANTHROPIC_API_KEY
            if [ -n "${ANTHROPIC_VERTEX_PROJECT_ID:-}" ]; then
                pass worker_secret_claude "Vertex project=$ANTHROPIC_VERTEX_PROJECT_ID"
            elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
                pass worker_secret_claude "ANTHROPIC_API_KEY set (direct)"
            else
                fail worker_secret_claude "neither ANTHROPIC_VERTEX_PROJECT_ID nor ANTHROPIC_API_KEY"
            fi ;;
        gemini|GeminiACPWorker)
            if [ -n "${GOOGLE_GENERATIVE_AI_API_KEY:-}${GEMINI_API_KEY:-}" ]; then
                pass worker_secret_gemini "API key set"
            else
                skip worker_secret_gemini "no gemini key (may use Vertex ADC)"
            fi ;;
        openclaw|OpenClawWorker)
            if ss -tlnp 2>/dev/null | grep -q ":18789 "; then
                pass worker_openclaw_gateway "gateway on :18789"
            else
                fail worker_openclaw_gateway "gateway not listening on :18789"
                _suggest worker_openclaw_gateway \
                    "nohup openclaw gateway > /tmp/openclaw-gateway.log 2>&1 &" \
                    "OpenClaw Gateway must run before any OpenClaw worker can start."
            fi ;;
        kilo|KiloWorker)
            local kbin; kbin=$(command -v kilo 2>/dev/null)
            if [ -n "$kbin" ]; then
                pass worker_kilo_binary "$kbin"
            else
                fail worker_kilo_binary "kilo not on PATH"
            fi
            local out; out=$(firestore_get "$bot" "model")
            local m; m=$(_fs_value "$out")
            if [ -n "$m" ]; then
                pass worker_kilo_model "$m"
            else
                skip worker_kilo_model "no model preset (will use kilo default)"
            fi ;;
        *)
            skip worker_secrets "unknown worker_type=$wt" ;;
    esac
}

check_bot_log_recent() {
    local bot="$1"
    local logf="${HOME}/.claude/closecrab/${bot}/bot.log"
    if [ ! -f "$logf" ]; then
        skip bot_log_recent "no $logf"
        return
    fi
    local mtime now age
    mtime=$(stat -c %Y "$logf" 2>/dev/null) || mtime=0
    now=$(date +%s)
    age=$((now - mtime))
    if [ "$age" -lt 3600 ]; then
        pass bot_log_recent "log mtime ${age}s ago"
    elif [ "$age" -lt 86400 ]; then
        skip bot_log_recent "log idle ${age}s (no recent activity)"
    else
        fail bot_log_recent "log silent ${age}s (>24h)"
        _suggest bot_log_recent \
            "python3 ~/CloseCrab/scripts/inbox-send.py $bot '/restart'" \
            "Bot log silent >24h -- likely stuck, restart it."
    fi
}

check_binary_alignment() {
    # Round 3 anti-pattern 1: bot lstart > git HEAD commit time
    local bot="$1"
    local out; out=$(python3 ~/CloseCrab/scripts/check-binary-alignment.py "$bot" --json 2>/dev/null)
    if [ -z "$out" ]; then
        skip binary_alignment "check-binary-alignment.py not available"
        return
    fi
    local aligned; aligned=$(echo "$out" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('aligned', False))" 2>/dev/null)
    local delta;   delta=$(echo "$out"   | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('delta_seconds', 0))" 2>/dev/null)
    local sha;     sha=$(echo "$out"     | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('target_commit_short', '?'))" 2>/dev/null)
    if [ "$aligned" = "True" ]; then
        pass binary_alignment "bot $((delta))s newer than HEAD ($sha)"
    else
        fail binary_alignment "bot $((-delta))s STALER than HEAD ($sha) -- restart needed for any fast-path test"
        _suggest binary_alignment \
            "nohup setsid bash -c 'sleep 12 && pkill -HUP -f \"python3 -m closecrab --bot $bot\"' </dev/null >/dev/null 2>&1 & disown" \
            "Round 3 anti-pattern 1: stale binary; fast-path tests on this bot will not reflect HEAD."
    fi
}

check_fast_path_callbacks() {
    # Round 2/3: every channel's _make_input_callback should accept is_inbox parameter
    # Static check across closecrab/channels/*.py (cheap, runs once per bot invocation)
    local channels_dir="$HOME/CloseCrab/closecrab/channels"
    if [ ! -d "$channels_dir" ]; then
        skip fast_path_callbacks "channels dir not found"
        return
    fi
    local missing=""
    for f in "$channels_dir"/*.py; do
        local fname; fname=$(basename "$f")
        # Skip base.py and non-channel utilities
        [ "$fname" = "base.py" ] && continue
        # If file defines _make_input_callback, the def line must contain is_inbox
        if grep -q "def _make_input_callback" "$f"; then
            if ! grep -q "def _make_input_callback.*is_inbox" "$f"; then
                missing="${missing}${fname}, "
            fi
        fi
    done
    if [ -z "$missing" ]; then
        pass fast_path_callbacks "all channels have is_inbox parameter"
    else
        fail fast_path_callbacks "missing is_inbox in: ${missing%, }"
        _suggest fast_path_callbacks \
            "Patch each channel's _make_input_callback signature to add is_inbox: bool = False + fast-path block (see evolution/references/control-request-fastpath.md)" \
            "Round 2/3 anti-pattern: channel without is_inbox routes inbox-initiated control_request to user-facing path; bot-to-bot turns will block 5min × N hitting BotCore lock timeout."
    fi
}

check_recent_errors() {
    local bot="$1"
    local logf="${HOME}/.claude/closecrab/${bot}/bot.log"
    [ ! -f "$logf" ] && { skip recent_errors "no log"; return; }
    # Count CRITICAL/ERROR in last 200 lines, ignore well-known noise
    local n
    n=$(tail -n 200 "$logf" 2>/dev/null | \
        grep -Ec '\b(ERROR|CRITICAL|Traceback|Exception)\b' || true)
    if [ "$n" = "0" ]; then
        pass recent_errors "tail clean"
    elif [ "$n" -lt 3 ]; then
        skip recent_errors "$n error lines in tail (review)"
    else
        fail recent_errors "$n error lines in tail (last 200)"
    fi
}

check_drop_ins() {
    local bot="$1"
    local d1="${HOME}/.closecrab/smoke-tests.d"
    local d2="${HOME}/.closecrab/smoke-tests.d/${bot}"
    local count=0
    for d in "$d1" "$d2"; do
        [ -d "$d" ] || continue
        for f in "$d"/*.sh; do
            [ -f "$f" ] || continue
            count=$((count+1))
            local name; name=$(basename "$f" .sh)
            # Drop-in contract: prints "OK ..." / "FAIL ..." / "SKIP ..." to stdout
            local out rc
            out=$(BOT="$bot" timeout 10 bash "$f" 2>&1)
            rc=$?
            if [ $rc -eq 0 ] && echo "$out" | head -1 | grep -q '^OK'; then
                pass "ext:$name" "$(echo "$out" | head -1 | sed 's/^OK *//')"
            elif echo "$out" | head -1 | grep -q '^SKIP'; then
                skip "ext:$name" "$(echo "$out" | head -1 | sed 's/^SKIP *//')"
            else
                fail "ext:$name" "rc=$rc $(echo "$out" | head -1 | sed 's/^FAIL *//')"
            fi
        done
    done
    [ "$count" = "0" ] && skip drop_ins "no .sh files in smoke-tests.d/"
}

# ---- runner per bot ----
run_for_bot() {
    local bot="$1"
    local start_pass=$PASS_N start_fail=$FAIL_N start_skip=$SKIP_N
    [ "$QUIET" = "0" ] && printf "\n%s[bot=%s]%s\n" "$C_DIM" "$bot" "$C_RST"

    WORKER_TYPE="claude"
    check_bot_process       "$bot"
    check_run_sh            "$bot"
    check_firestore_sa
    check_firestore_reachable "$bot"
    check_worker_type       "$bot"
    check_claude_settings
    check_worker_secrets    "$bot" "$WORKER_TYPE"
    check_bot_log_recent    "$bot"
    check_recent_errors     "$bot"
    check_binary_alignment  "$bot"
    check_fast_path_callbacks
    check_drop_ins          "$bot"

    local p=$((PASS_N - start_pass))
    local f=$((FAIL_N - start_fail))
    local s=$((SKIP_N - start_skip))
    if [ "$QUIET" = "0" ]; then
        local color="$C_PASS"; [ "$f" -gt 0 ] && color="$C_FAIL"
        printf "  %s—> %d passed, %d failed, %d skipped%s\n" \
            "$color" "$p" "$f" "$s" "$C_RST"
    fi
}

# ---- main ----
BOTS=()
if [ "$MODE" = "all" ]; then
    # discover from process list
    while read -r line; do
        b=$(echo "$line" | sed -nE 's/.*--bot ([A-Za-z0-9_-]+).*/\1/p' | head -1)
        [ -n "$b" ] && BOTS+=("$b")
    done < <(pgrep -af "python3 -m closecrab" 2>/dev/null | grep -v grep)
    # de-dup
    if [ ${#BOTS[@]} -gt 0 ]; then
        readarray -t BOTS < <(printf '%s\n' "${BOTS[@]}" | awk '!seen[$0]++')
    fi
    if [ ${#BOTS[@]} -eq 0 ]; then
        echo "no closecrab bot processes detected on this host" >&2
        exit 2
    fi
else
    BOTS=("$BOT")
fi

for b in "${BOTS[@]}"; do
    run_for_bot "$b"
done

# summary
if [ "$JSON" = "1" ]; then
    # overall status: ok | warn | fail
    status_field="ok"
    [ "$SKIP_N" -gt 0 ] && status_field="warn"
    [ "$FAIL_N" -gt 0 ] && status_field="fail"
    printf '{"status":"%s","pass":%d,"fail":%d,"skip":%d,"bots":["%s"],"results":[%s],"actions":[%s]}\n' \
        "$status_field" "$PASS_N" "$FAIL_N" "$SKIP_N" \
        "$(IFS='","'; printf '%s' "${BOTS[*]}")" \
        "$RESULTS_JSON" "$ACTIONS_JSON"
else
    [ "$QUIET" = "0" ] && echo ""
    if [ "$FAIL_N" -eq 0 ]; then
        printf "%sResults: %d passed, %d failed, %d skipped%s\n" \
            "$C_PASS" "$PASS_N" "$FAIL_N" "$SKIP_N" "$C_RST"
    else
        printf "%sResults: %d passed, %d failed, %d skipped%s\n" \
            "$C_FAIL" "$PASS_N" "$FAIL_N" "$SKIP_N" "$C_RST"
    fi
fi

exit "$FAIL_N"
