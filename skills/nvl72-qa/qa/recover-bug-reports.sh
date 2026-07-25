#!/bin/bash
# 从节点主机 /tmp 直接捞回 nvidia-bug-report gz
#
# 用途：hw-check 跑完后 pod 已删除、或 collect_bug_reports 没收齐时，
#       gz 仍留在各节点主机 /tmp 上，用本脚本按节点批量取回。
#
# 用法:
#   bash qa/recover-bug-reports.sh <profile> <subblock> [outdir]
#
# 例:
#   bash qa/recover-bug-reports.sh qa/profiles/gb300-gke-taiji.sh 0013
#   bash qa/recover-bug-reports.sh qa/profiles/gb300-gke-taiji.sh 0013 qa/logs/qa-bug-reports-gb300-0013-20260725-061634
set -uo pipefail

PROFILE="${1:-}"; SUBBLOCK="${2:-}"; OUTDIR="${3:-}"
if [ -z "$PROFILE" ] || [ -z "$SUBBLOCK" ]; then
  echo "用法: $0 <profile> <subblock> [outdir]"; exit 1
fi
[ ! -f "$PROFILE" ] && echo "ERROR: profile 不存在: $PROFILE" && exit 1
source "$PROFILE"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POOL="${QA_POOL_FALLBACK_PREFIX:-gb300-pool}-${SUBBLOCK}"
NS="br-recover-${SUBBLOCK}"
CTX="${QA_KUBE_CONTEXT:-}"
if [ -n "$CTX" ]; then KTL=(kubectl --context="$CTX"); else KTL=(kubectl); fi
[ -z "$OUTDIR" ] && OUTDIR="${SCRIPT_DIR}/logs/qa-bug-reports-${QA_GPU_TYPE:-gb300}-${SUBBLOCK}-recovered-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUTDIR"

log() { echo "=== [$(date +%H:%M:%S)] $* ==="; }

TOTAL_NODES=$("${KTL[@]}" get nodes -l "${QA_NODE_SELECTOR_KEY}=${POOL}" --no-headers 2>/dev/null | wc -l)
log "pool=${POOL}  节点=${TOTAL_NODES}  输出=${OUTDIR}"
[ "$TOTAL_NODES" -eq 0 ] && echo "ERROR: pool ${POOL} 无节点" && exit 1

# 已存在且非空的按节点名跳过（支持增量补收）
SKIP=0
for f in "${OUTDIR}"/nvidia-bug-report-*.log.gz; do
  [ -s "$f" ] && ((SKIP++)) || true
done
[ "$SKIP" -gt 0 ] && echo "  已有 ${SKIP} 份，将跳过同名节点（增量补收）"

trap '"${KTL[@]}" delete ns "$NS" --wait=false >/dev/null 2>&1' EXIT

"${KTL[@]}" create ns "$NS" --dry-run=client -o yaml | "${KTL[@]}" apply -f - >/dev/null 2>&1
cat <<EOF | "${KTL[@]}" apply -f - >/dev/null
apiVersion: apps/v1
kind: DaemonSet
metadata: {name: br, namespace: ${NS}}
spec:
  selector: {matchLabels: {app: br}}
  template:
    metadata: {labels: {app: br}}
    spec:
      nodeSelector: {${QA_NODE_SELECTOR_KEY}: ${POOL}}
      tolerations:
      - {key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}
      - {key: kubernetes.io/arch, operator: Exists, effect: NoSchedule}
      - {key: node.kubernetes.io/unschedulable, operator: Exists, effect: NoSchedule}
      volumes: [{name: t, hostPath: {path: /tmp}}]
      containers:
      - name: c
        image: ${QA_IMAGE}
        securityContext: {privileged: true}
        volumeMounts: [{name: t, mountPath: /host-tmp}]
        command: ["/bin/sh","-c","sleep 3600"]
EOF

log "等待 DaemonSet pod 就绪"
for i in $(seq 1 40); do
  R=$("${KTL[@]}" get pods -n "$NS" -l app=br --no-headers 2>/dev/null | grep -c Running || true)
  [ $((i % 5)) -eq 0 ] && echo "  ${R}/${TOTAL_NODES} Running"
  [ "$R" -ge "$TOTAL_NODES" ] && break
  sleep 6
done

log "逐节点取回"
OK=0; FAIL=0; SKIPPED=0
for POD in $("${KTL[@]}" get pods -n "$NS" -l app=br --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null); do
  # 节点名带重试；取不到就跳过，绝不用 pod 名命名
  NODE=""
  for _t in 1 2 3; do
    NODE=$(timeout 15 "${KTL[@]}" get pod "$POD" -n "$NS" -o jsonpath='{.spec.nodeName}' 2>/dev/null || true)
    [ -n "$NODE" ] && break
    sleep 3
  done
  if [ -z "$NODE" ]; then
    echo "  [SKIP] pod ${POD}: 取不到 nodeName，跳过（不产出无法追溯的文件）"
    ((FAIL++)) || true; continue
  fi
  SHORT=$(echo "$NODE" | grep -oE '[^-]+$')
  DST="${OUTDIR}/nvidia-bug-report-${SHORT}.log.gz"

  if [ -s "$DST" ]; then echo "  [SKIP] ${SHORT}: 已存在"; ((SKIPPED++)) || true; continue; fi

  GZ=""
  for _t in 1 2 3; do
    GZ=$(timeout 30 "${KTL[@]}" exec "$POD" -n "$NS" -- \
         sh -c 'ls /host-tmp/nvidia-bug-report-*.log.gz 2>/dev/null' 2>/dev/null | head -1 || true)
    [ -n "$GZ" ] && break
    sleep 3
  done
  if [ -z "$GZ" ]; then echo "  [FAIL] ${SHORT}: 主机 /tmp 上无 gz"; ((FAIL++)) || true; continue; fi

  CPOK=0
  for _t in 1 2 3; do
    if timeout 120 "${KTL[@]}" cp "${NS}/${POD}:${GZ}" "$DST" 2>/dev/null && [ -s "$DST" ]; then CPOK=1; break; fi
    rm -f "$DST"; sleep 5
  done
  if [ "$CPOK" -eq 1 ]; then
    echo "  [OK]   ${SHORT}: $(ls -lh "$DST" | awk '{print $5}')"
    ((OK++)) || true
  else
    echo "  [FAIL] ${SHORT}: cp 3 次均失败"; ((FAIL++)) || true
  fi
done

HAVE=$(ls -1 "${OUTDIR}"/nvidia-bug-report-*.log.gz 2>/dev/null | wc -l)
log "取回 ${OK} 份 (跳过已有 ${SKIPPED}, 失败 ${FAIL})；目录内共 ${HAVE} / ${TOTAL_NODES} 节点"
[ "$FAIL" -gt 0 ] && exit 1
exit 0
