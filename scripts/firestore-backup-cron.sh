#!/bin/bash
# Firestore 周度备份 —— 两条腿：GCS 全保真 export + 私有仓库脱敏 JSON 快照。
#
# 为什么能放系统 crontab：全程 gcloud / python / git，**不调任何 LLM**。
# 会起模型的东西一律走 cron-tool.py 的 timeline（见 CLAUDE.md），这个不是。
#
# 三层备份的分工：
#   托管 backup schedule  每天，GCP 自己管，防「库没了」        ← 已配，不用管
#   PITR                  连续 7 天，防「数据被改坏」           ← 已开，不用管
#   本脚本                每周，GCS export 离线归档 + git 可读快照
#
# 装法（幂等）：
#   crontab -l | grep -q firestore-backup-cron || \
#     (crontab -l 2>/dev/null; echo "30 4 * * 1 $HOME/CloseCrab/scripts/firestore-backup-cron.sh >> $HOME/firestore-backup.log 2>&1") | crontab -

set -uo pipefail

PROJECT="${FIRESTORE_PROJECT:-chris-pgp-host}"
BUCKET="${FIRESTORE_BACKUP_BUCKET:-gs://chris-pgp-host-asia/firestore-backups}"
PRIVATE_REPO="${PRIVATE_REPO:-$HOME/my-private}"
DBS=(closecrab closecrab-public)
KEEP_WEEKS=8

# /snap/bin 必须在里面 —— 本机 gcloud 是 snap 装的，cron 的默认 PATH 没有它。
# 首版我照着"想当然"写了 $HOME/google-cloud-sdk/bin，那个目录根本不存在，
# 结果 export 静默失败而 JSON 快照照常成功，日志看起来"跑了"。
# 用 env -i 最小环境实跑才暴露出来。
export PATH="/snap/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
command -v gcloud >/dev/null || { echo "[FATAL] PATH 里没有 gcloud，当前 PATH=$PATH"; exit 3; }
TS=$(TZ='Asia/Hong_Kong' date +%Y%m%d-%H%M)
say() { echo "[$(TZ='Asia/Hong_Kong' date '+%F %T %Z')] $*"; }

say "==== Firestore 周度备份开始 ===="

# ── 1. GCS export（全保真，能 import 回去）────────────────────────
for db in "${DBS[@]}"; do
    # 不要吞 stderr。首版这里写的是 >/dev/null 2>&1，于是 "gcloud: command not found"
    # 被完整咽掉，只剩一行"失败"，等于把唯一有用的线索删了。
    err=$(gcloud firestore export "$BUCKET/$TS-$db" \
            --database="$db" --project="$PROJECT" 2>&1) \
      && say "export $db → $BUCKET/$TS-$db  OK" \
      || { say "!! export $db 失败："; echo "$err" | tail -5 | sed 's/^/      /'; }
done

# ── 2. 清理超过 KEEP_WEEKS 的旧 export ────────────────────────────
# 目录名形如 20260810-0052-closecrab，按日期前缀比较即可
CUTOFF=$(TZ='Asia/Hong_Kong' date -d "-${KEEP_WEEKS} weeks" +%Y%m%d)
while read -r d; do
    [ -z "$d" ] && continue
    name=$(basename "${d%/}")
    day=${name%%-*}
    # 只处理 8 位纯数字开头的，避开手工放的其它目录
    [[ "$day" =~ ^[0-9]{8}$ ]] || continue
    if [[ "$day" < "$CUTOFF" ]]; then
        gcloud storage rm -r "$d" >/dev/null 2>&1 && say "清理旧 export $name"
    fi
done < <(gcloud storage ls "$BUCKET/" 2>/dev/null)

# ── 3. 脱敏 JSON 快照 → 私有仓库 ──────────────────────────────────
if [ -d "$PRIVATE_REPO/.git" ]; then
    OUT="$PRIVATE_REPO/firestore"
    if python3 "$(dirname "$0")/firestore-dump.py" --project "$PROJECT" --out "$OUT" --no-logs; then
        cd "$PRIVATE_REPO" || exit 1
        if [ -n "$(git status --porcelain firestore)" ]; then
            git add firestore
            git commit -q -m "chore(firestore): $TS 配置快照

由 scripts/firestore-backup-cron.sh 自动生成。脱敏、不含日志。
全保真备份见 GCS $BUCKET/$TS-* 与每日托管 backup schedule。"
            if git push -q 2>/dev/null; then
                say "私有仓库快照已提交并推送"
            else
                say "!! 快照已提交但 push 失败（下次会一起推）"
            fi
        else
            say "配置无变化，跳过提交"
        fi
    else
        say "!! JSON dump 失败"
    fi
else
    say "!! 找不到私有仓库 $PRIVATE_REPO，跳过 JSON 快照"
fi

say "==== 完成 ===="
