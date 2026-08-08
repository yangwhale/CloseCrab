#!/bin/bash
# 后台日志播报器：只在日志出现「新的匹配行」时才推飞书，不触发 LLM turn。
#
# 跟 cron-tool.py 的本质区别：
#   cron-tool.py → 进 inbox → 触发 bot 的完整 LLM turn（占 lock、打断在跑的命令、任务堆积）
#   本脚本       → 调 feishu-notify.py 直发 → 只是往聊天窗口贴一行字
#
# 用法（挂系统 crontab）:
#   * * * * * /home/chrisya/CloseCrab/scripts/log-watch-notify.sh <日志路径> <grep正则> <状态文件> [前缀]
#
# 例:
#   * * * * * .../log-watch-notify.sh /tmp/isl.log '^ISL=' /tmp/.isl.seen 'ISL扫描'
set -o pipefail
# 身份必须由调用处给定。挂 crontab 时在那一行前面写 BOT_NAME=xxx。
# 不给兜底默认值：缺省会以别人的飞书 app 身份发出（见 feishu-notify.py）。
[ -z "$BOT_NAME" ] && { echo "$(basename "$0"): BOT_NAME not set" >&2; exit 2; }
LOG=$1; PAT=$2; STATE=${3:-/tmp/.logwatch.seen}; PREFIX=${4:-进度}
[ -f "$LOG" ] || exit 0

NOW=$(grep -cE "$PAT" "$LOG" 2>/dev/null || echo 0)
SEEN=$(cat "$STATE" 2>/dev/null || echo 0)
[ "$NOW" -le "$SEEN" ] && exit 0        # 没新行就静默退出，不刷屏

NEWLINES=$(grep -E "$PAT" "$LOG" | tail -n $((NOW - SEEN)))
echo "$NOW" > "$STATE"
# 同 agent-watch.sh：显式传身份，别用默认值冒名。
python3 /home/chrisya/CloseCrab/scripts/feishu-notify.py \
  --bot "$BOT_NAME" "[$PREFIX] $(date '+%H:%M')
$NEWLINES" >/dev/null 2>&1
