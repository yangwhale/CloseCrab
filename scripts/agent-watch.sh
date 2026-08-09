#!/bin/bash
# 后台 sub-agent 巡检播报器。
#
# 跟另外两种做法的区别：
#   cron-tool.py         → 进 inbox → 触发主 bot 一次完整 LLM turn
#                          → 占 per-user lock、打断在跑的命令、任务堆积  ❌
#   log-watch-notify.sh  → 纯 shell grep → 只能贴原始日志，没有理解能力    ❌
#   本脚本               → 独立进程起一个 headless sub-agent，它自己看状态、
#                          自己判断有没有进展、自己组织语言，再直发飞书。
#                          模型在环，但不占主线。                          ✅
#
# 用法:
#   agent-watch.sh <名字> <给 agent 的指令>
#
# 例（挂系统 crontab）:
#   * * * * * .../agent-watch.sh isl "读 /tmp/isl.log ..."
#
# 机制:
#   - 把「上次播报内容」喂给 agent，让它自己判断是否有实质进展
#   - agent 认为没进展就回 SKIP，脚本静默退出，不刷屏
#   - 用 haiku（快 + 便宜，单次约 10s）
set -o pipefail
# 身份必须由调用处给定。挂 crontab 时在那一行前面写 BOT_NAME=xxx。
# 不给兜底默认值：缺省会以别人的飞书 app 身份发出（见 feishu-notify.py）。
[ -z "$BOT_NAME" ] && { echo "$(basename "$0"): BOT_NAME not set" >&2; exit 2; }
NAME=$1; shift
PROMPT="$*"
[ -z "$NAME" ] && { echo "usage: agent-watch.sh <name> <prompt>"; exit 1; }

LAST=/tmp/.agentwatch-$NAME.last
LOCK=/tmp/.agentwatch-$NAME.lock
# 防重入：上一轮还没跑完就跳过（agent 可能比 cron 间隔慢）
exec 9>"$LOCK"; flock -n 9 || exit 0

PREV=$(cat "$LAST" 2>/dev/null || echo "（还没报过）")
OUT=$(cd /tmp && env -u ANTHROPIC_BETAS timeout 240 claude -p "$PROMPT

【上次播报内容】
$PREV

【输出规则】
1. 先自己判断：跟上次相比有没有**实质进展**（新数字/新阶段/新故障）。
2. 没有实质进展 → 只输出 SKIP 四个字母，别的什么都不要输出。
3. 有进展 → 输出 2-4 句中文播报。要有判断，不要只念日志。带上关键数字。
4. 不要 markdown，不要标题，不要客套。" \
  --model claude-haiku-4-5-20251001 --dangerously-skip-permissions 2>/dev/null | tail -30)

[ -z "$OUT" ] && exit 0
echo "$OUT" | head -1 | grep -qE '^\s*SKIP' && exit 0
echo "$OUT" > "$LAST"
# --bot 必须显式传：本脚本常挂系统 crontab，那里没有 BOT_NAME，
# 而 feishu-notify 用的是该 bot 的飞书 app 凭证，缺省会以别人的身份发出。
python3 /home/chrisya/CloseCrab/scripts/feishu-notify.py \
  --bot "$BOT_NAME" "🤖 [$NAME] $(date '+%H:%M')
$OUT" >/dev/null 2>&1
