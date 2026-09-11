#!/bin/bash
# 判「语音录音守护还活着吗」—— 给 watch-task 当探针用，也可以手跑。
#
# 背景：2026-09-11 bunny 在语音频道**只能说不能听四个小时，一条 ERROR 都没有**。
# 起因是 `_ssrc_infer_loop` 被一个没人接的异常打死，而它是「录音掉了就重新拉起」
# 的唯一执行者。异常本身已经在 discord_voice_sidecar.py 里兜住了，但那次真正的
# 教训是**没人发现** —— 出站 TTS 一切正常，从外面看它活得好好的。
# 这个脚本补的是那个洞：守护每 ~0.3s 打一条「诊断#」，它一停就是死了。
#
# 用法：voice-watchdog-check.sh [bot_name]   默认 bunny
# 输出：OK <秒> / STALE <秒> / BOT_DOWN / NO_DIAG_LINE
#
# ⚠️ 那个 `grep -av claude_code` 不是随手加的：bot 自己的对话日志会把 agent 跑过
# 的 shell 命令原样记进同一个 bot.log。在里面 grep 「诊断#」会**匹配到自己刚才
# 那条命令**，于是探针永远报健康。claude_code 这个 logger 名是干净的判别式。
set -u
BOT="${1:-bunny}"
LOG="$HOME/.claude/closecrab/$BOT/bot.log"
STALE_AFTER="${STALE_AFTER:-180}"

pgrep -f "closecrab --bot $BOT" >/dev/null || { echo "BOT_DOWN"; exit 0; }
[ -r "$LOG" ] || { echo "NO_DIAG_LINE (读不到 $LOG)"; exit 0; }

LAST=$(tail -n 20000 "$LOG" \
        | grep -av "claude_code" \
        | grep -a "discord_voice_sidecar" \
        | grep -a "诊断#" \
        | tail -1 | cut -c1-19)

[ -z "$LAST" ] && { echo "NO_DIAG_LINE"; exit 0; }

AGE=$(( $(date -u +%s) - $(date -u -d "$LAST" +%s) ))
if [ "$AGE" -gt "$STALE_AFTER" ]; then
    echo "STALE ${AGE}s  最后一条=$LAST"
else
    echo "OK ${AGE}s"
fi
