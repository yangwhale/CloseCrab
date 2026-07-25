#!/bin/bash
# GPU 节点质检编排工具
# 支持 GB200/GB300 + GKE/自建 k8s，通过 profile 配置
#
# 用法:
#   bash qa/run-checks.sh <profile> <action> <subblock> [node-suffix]
#
#   bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh hw-check 0011
#   bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh nccl 0011 01v7
#   bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh all 0012
#   bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh clean
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="${SCRIPT_DIR}/templates"
LOGS_BASE="${SCRIPT_DIR}/logs"
mkdir -p "$LOGS_BASE"
MANIFEST_FILE=""
FAIL_COUNT=0

PROFILE="${1:-}"
ACTION="${2:-help}"
SUBBLOCK="${3:-}"
NODE_SUFFIX="${4:-}"

log() { echo "=== [$(date +%H:%M:%S)] $* ==="; }

###############################################################################
# Profile 加载
###############################################################################
if [ -z "$PROFILE" ] || [ "$ACTION" = "help" ] || [ "$ACTION" = "--help" ]; then
  echo "GPU 节点质检工具"
  echo ""
  echo "用法: $0 <profile> <action> <subblock> [node-suffix]"
  echo ""
  echo "Profile: qa/profiles/*.sh"
  echo ""
  echo "Actions:"
  echo "  hw-check <sub> [node]              硬件自检"
  echo "  nccl <sub> [node]                  单机 NCCL"
  echo "  gemm <sub> [node]                  cuBLAS GEMM"
  echo "  nccl-multi <sub> [--mnnvl=on|off]  多节点 NCCL (同 domain)"
  echo "  nccl-cross <sub1> <sub2>           跨域 NCCL (2 domain, MNNVL=2)"
  echo "  all <sub> [node]                   全部单节点测试"
  echo "  all-full <sub>                     全面质检 (单节点 + 多节点 RDMA + MNNVL)"
  echo "  logs                               查看状态"
  echo "  clean                              清理"
  echo ""
  echo "示例:"
  echo "  $0 qa/profiles/gb300-gke-taiji.sh hw-check 0011"
  echo "  $0 qa/profiles/gb300-gke-taiji.sh all 0012"
  echo "  $0 qa/profiles/gb300-gke-taiji.sh hw-check 0011 01v7"
  echo "  $0 qa/profiles/gb300-gke-taiji.sh nccl-multi 0012 --mnnvl=off"
  echo "  $0 qa/profiles/gb300-gke-taiji.sh all-with-multi 0012"
  exit 0
fi

if [ ! -f "$PROFILE" ]; then
  echo "ERROR: profile 不存在: $PROFILE"
  exit 1
fi
source "$PROFILE"

# kubectl context: profile 中 QA_KUBE_CONTEXT 指定，所有 kubectl 调用通过 ktl 包装
CTX="${QA_KUBE_CONTEXT:-}"
ktl() {
  if [ -n "$CTX" ]; then
    kubectl --context="$CTX" "$@"
  else
    kubectl "$@"
  fi
}

# 只读 kubectl 查询 retry wrapper（3 次指数退避 2s/5s/10s），吃掉 GKE public
# endpoint 瞬时 i/o timeout。只用于 get/describe/version 类只读命令；不要用于
# apply/delete/wait/exec 等副作用或长连接命令。
ktl_ro_retry() {
  local OUT RC ATTEMPT
  for ATTEMPT in 1 2 3; do
    OUT=$(timeout 30 "${KTL_CMD[@]}" "$@" 2>&1)
    RC=$?
    if [ $RC -eq 0 ]; then
      echo "$OUT"
      return 0
    fi
    # 仅对网络/连接类错误 retry；其它 (RBAC/NotFound/语法) 立刻返回
    if ! echo "$OUT" | grep -qE "i/o timeout|connection refused|Unable to connect|EOF|no route to host|network is unreachable|TLS handshake timeout"; then
      echo "$OUT" >&2
      return $RC
    fi
    if [ $ATTEMPT -lt 3 ]; then
      local BACKOFF=$((ATTEMPT * ATTEMPT + 1))
      echo "  ktl 网络毛刺 (${ATTEMPT}/3): ${OUT}. ${BACKOFF}s 后重试" >&2
      sleep $BACKOFF
    fi
  done
  echo "$OUT" >&2
  return $RC
}
if [ -n "$CTX" ]; then
  KTL_CMD=(kubectl --context="$CTX")
else
  KTL_CMD=(kubectl)
fi
if [ -n "$CTX" ]; then
  CTX_OK=0
  for CTX_TRY in 1 2 3; do
    if timeout 30 kubectl --context="$CTX" cluster-info > /dev/null 2>&1; then
      CTX_OK=1; break
    fi
    echo "  context 连接尝试 ${CTX_TRY}/3 失败，5s 后重试..."
    sleep 5
  done
  if [ "$CTX_OK" -eq 0 ]; then
    echo "ERROR: kubectl context '${CTX}' 3 次均不可达"
    exit 1
  fi
fi

# namespace 带 subblock 后缀，支持多 domain 并行
if [ -n "$SUBBLOCK" ]; then
  export QA_NAMESPACE="${QA_NAMESPACE}-${SUBBLOCK}"
fi
NS="${QA_NAMESPACE}"
MANIFEST_FILE="${LOGS_BASE}/qa-manifest-${QA_GPU_TYPE}-${SUBBLOCK:-all}-$(date +%Y%m%d-%H%M%S).txt"
log "Profile: $(basename $PROFILE) (${QA_GPU_TYPE}, ${QA_CLUSTER_TYPE}, ns=${NS})"

# 并行 stagger: 按 subblock 号错开启动，避免多 domain 同时打 API
STAGGER_S=${QA_PREFLIGHT_STAGGER_S:-2}
if [ -n "$SUBBLOCK" ] && [ "$STAGGER_S" -gt 0 ] 2>/dev/null; then
  ORDINAL=$((10#${SUBBLOCK}))
  DELAY=$((ORDINAL * STAGGER_S))
  if [ "$DELAY" -gt 0 ]; then
    echo "  并行 stagger: subblock ${SUBBLOCK} → ${DELAY}s 后启动"
    sleep "$DELAY"
  fi
fi

###############################################################################
# envsubst 包装：替换所有 QA_* 变量
###############################################################################
qa_envsubst() {
  local VARS=$(env | grep ^QA_ | cut -d= -f1 | sed 's/^/$/g' | tr '\n' ' ')
  envsubst "$VARS"
}

###############################################################################
# Pool 名解析
###############################################################################
resolve_pool() {
  local SUB=$1
  local POOL=""
  if [ -n "${QA_RESERVATION_LABEL_KEY}" ] && [ -n "${QA_RESERVATION_PREFIX}" ]; then
    POOL=$(ktl_ro_retry get nodes -l "${QA_RESERVATION_LABEL_KEY}=${QA_RESERVATION_PREFIX}-${SUB}" \
      --no-headers -o jsonpath='{.items[0].metadata.labels.'"${QA_NODE_SELECTOR_KEY}"'}' 2>/dev/null)
  fi
  if [ -z "$POOL" ]; then
    POOL="${QA_POOL_FALLBACK_PREFIX}-${SUB}"
    echo "  WARNING: 反查 pool 名失败，fallback: ${POOL}" >&2
  fi
  echo "$POOL"
}

resolve_node() {
  local POOL=$1 SUFFIX=$2
  ktl_ro_retry get nodes -l "${QA_NODE_SELECTOR_KEY}=${POOL}" \
    --no-headers -o custom-columns=NAME:.metadata.name | grep "${SUFFIX}$"
}

###############################################################################
# 前置检查（gcloud 调用按 QA_GCLOUD_BATCH_SIZE 分批，防并行限流）
###############################################################################
preflight_check() {
  local POOL=$1
  local BATCH_SIZE=${QA_GCLOUD_BATCH_SIZE:-5}
  local BATCH_DELAY=${QA_GCLOUD_BATCH_DELAY:-1}
  log "前置检查: ${POOL} GPU 数量 (期望 ${QA_GPUS_PER_NODE})"
  local GCLOUD_COUNT=0
  while read NAME GPU SCHED; do
    SHORT=$(echo "$NAME" | grep -oE '[^-]+$')
    if [ "${SCHED}" = "true" ]; then
      echo "  ${SHORT}: cordoned (跳过)"
    elif [ "${GPU:-0}" -lt "${QA_GPUS_PER_NODE}" ]; then
      echo "  [FAIL] ${SHORT}: GPU=${GPU} (expected ${QA_GPUS_PER_NODE}) — 需要 cordon"
      if [ -n "${QA_GCLOUD_CONFIG:-}" ]; then
        ((GCLOUD_COUNT++)) || true
        if [ "$GCLOUD_COUNT" -gt "$BATCH_SIZE" ]; then
          sleep "$BATCH_DELAY"
          GCLOUD_COUNT=1
        fi
        PH=$(gcloud --configuration=${QA_GCLOUD_CONFIG} compute instances describe "$NAME" \
          --zone=${QA_ZONE} --project=${QA_PROJECT} --format="value(resourceStatus.physicalHost)" 2>/dev/null)
        echo "         physicalHost: ${PH}"
      fi
    fi
  done < <(ktl_ro_retry get nodes -l "${QA_NODE_SELECTOR_KEY}=${POOL}" \
    -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu,SCHED:.spec.unschedulable \
    --no-headers 2>/dev/null)
}

###############################################################################
# 部署 DaemonSet
###############################################################################
apply_yaml() {
  local YAML=$1 SUB=$2
  export QA_POOL
  QA_POOL=$(resolve_pool "$SUB")
  export QA_NODE=""

  preflight_check "$QA_POOL"

  if [ -n "${NODE_SUFFIX}" ]; then
    QA_NODE=$(resolve_node "$QA_POOL" "$NODE_SUFFIX")
    if [ -z "$QA_NODE" ]; then
      echo "ERROR: 找不到尾缀 '${NODE_SUFFIX}' 的节点 (pool ${QA_POOL})"
      exit 1
    fi
    log "Apply ${YAML} → 单节点 ${QA_NODE}"
  else
    log "Apply ${YAML} → ${QA_POOL} (全部节点)"
  fi

  local RENDERED
  RENDERED=$(qa_envsubst < "${TEMPLATE_DIR}/${YAML}" | python3 -c "
import sys, os
node = os.environ.get('QA_NODE', '')
key = os.environ.get('QA_NODE_SELECTOR_KEY', '')
for line in sys.stdin:
    sys.stdout.write(line)
    if node and key and key in line:
        indent = len(line) - len(line.lstrip())
        sys.stdout.write(' ' * indent + 'kubernetes.io/hostname: ' + node + '\n')
")

  # 保存渲染后的 YAML 到日志目录
  local YAML_BASENAME="${YAML%.yaml}"
  local YAML_OUT="${LOGS_BASE}/rendered-${YAML_BASENAME}-${QA_GPU_TYPE}-${SUBBLOCK}-$(date +%Y%m%d-%H%M%S).yaml"
  echo "$RENDERED" > "$YAML_OUT"
  echo "  渲染 YAML 已保存: $(basename "$YAML_OUT")"

  local RETRY
  for RETRY in 1 2 3; do
    if echo "$RENDERED" | ktl apply -f - 2>&1; then
      return 0
    fi
    echo "  [apply 尝试 ${RETRY}/3] kubectl apply 失败，等 10s 重试..."
    sleep 10
  done
  echo "ERROR: kubectl apply 3 次均失败，跳过此测试"
  return 1
}

###############################################################################
# 等待完成（kubectl wait --for=condition=Ready，单次 server-side watch）
###############################################################################
wait_completion() {
  local LABEL=$1 MARKER=$2 TIMEOUT=$3
  log "等待完成 (label=${LABEL}, timeout=${TIMEOUT}s)"

  # 等至少 1 个 pod 存在
  # ⚠️ COUNT 不只是这里的判断条件，还会作为后续 READY/COUNT 的分母与兜底阈值。
  #    查询失败时 `wc -l` 得 0，与「pod 确实还没创建」无法区分，故用 QFAIL 单独记录查询失败。
  local POD_WAIT_START=$SECONDS POD_WAIT_MAX=120
  local COUNT=0 QFAIL=0
  for i in $(seq 1 60); do
    [ $((SECONDS - POD_WAIT_START)) -ge $POD_WAIT_MAX ] && echo "  pod 启动等待超时 (${POD_WAIT_MAX}s)" && break
    local _po
    _po=$(ktl_ro_retry get pods -n ${NS} -l "app=${LABEL}" --no-headers 2>/dev/null)
    if [ $? -ne 0 ]; then
      QFAIL=$((QFAIL + 1))
      [ $((i % 6)) -eq 0 ] && echo "  [$(date +%H:%M:%S)] pod 查询失败 ${QFAIL} 次，继续重试"
      sleep 5; continue
    fi
    QFAIL=0
    if [ -z "$_po" ]; then COUNT=0; else COUNT=$(printf '%s\n' "$_po" | grep -c . || true); fi
    [ "$COUNT" -ge 1 ] && break
    [ $((i % 6)) -eq 0 ] && echo "  [$(date +%H:%M:%S)] 等待 pod 创建..."
    sleep 5
  done

  if [ "$COUNT" -eq 0 ]; then
    if [ "$QFAIL" -gt 0 ]; then
      echo "  ERROR: pod 数量查询连续失败（最后 ${QFAIL} 次），无法确认是否已创建 —— 按失败处理"
    else
      echo "  ERROR: 无 pod（查询成功，确实未创建）"
    fi
    return 1
  fi
  echo "  ${COUNT} pod(s) 已创建"

  # ktl wait: server-side watch，单个持久连接，不轮询
  #
  # ⚠️ GKE public endpoint 网络毛刺（`dial tcp ...:443: i/o timeout`）会让 kubectl wait
  #    立即返回非 0。若直接判 TIMEOUT，会在测试正常运行时提前 cleanup 把 DaemonSet 删掉，
  #    等于误杀。2026-07-25 pool-0013 的 NCCL 就是这么被砍的（37s 就判 timeout，预算 300s）。
  #    这里按 wall-clock 记预算，区分「网络断」与「真超时」：网络类错误且预算还剩就重试。
  local WAIT_START=$SECONDS
  local ATTEMPT=0 WOUT WRC=1
  while :; do
    ATTEMPT=$((ATTEMPT + 1))
    local REMAIN=$((TIMEOUT - (SECONDS - WAIT_START)))
    [ "$REMAIN" -le 10 ] && break
    echo "  kubectl wait --for=condition=Ready (剩余 ${REMAIN}s，第 ${ATTEMPT} 次)..."
    WOUT=$(timeout $((REMAIN + 30)) "${KTL_CMD[@]}" wait pods -n ${NS} -l "app=${LABEL}" \
           --for=condition=Ready --timeout="${REMAIN}s" 2>&1)
    WRC=$?
    echo "$WOUT" | tail -3
    [ "$WRC" -eq 0 ] && break
    if echo "$WOUT" | grep -qE "i/o timeout|connection refused|Unable to connect|TLS handshake timeout|unexpected EOF|no route to host"; then
      if [ "$ATTEMPT" -ge 4 ]; then
        echo "  网络毛刺已重试 ${ATTEMPT} 次仍失败，放弃 wait（下面按实际 Ready 数判定）"
        break
      fi
      echo "  [$(date +%H:%M:%S)] 检测到网络毛刺（非测试失败），5s 后重试 wait"
      sleep 5
      continue
    fi
    break   # 真正的 timeout / pod 失败
  done

  # ⚠️ READY 计数必须能区分「查询失败」与「真 0」：
  #    2026-07-25 pool-0015 DCGM 因该查询失败显示「全部完成 (0/18 Ready)」（实际 18 个 pod 全 Ready）；
  #    更严重的是这个值还参与下面的兜底判定 —— 查询一失败，兜底就会把成功误判成失败。
  local READY=-1 _out
  for _try in 1 2 3; do
    _out=$(timeout 30 "${KTL_CMD[@]}" get pods -n ${NS} -l "app=${LABEL}" \
           -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null)
    if [ $? -eq 0 ] && [ -n "$_out" ]; then
      READY=$(echo "$_out" | grep -c True); break
    fi
    sleep 3
  done
  local READY_DISP="$READY"
  [ "$READY" -lt 0 ] && READY_DISP="查询失败"

  # 兜底：即使 wait 因网络原因失败，只要**确实查到** Ready 数达标就算通过。
  # READY=-1（查询失败）绝不能当作达标，也不能当作 0 去判失败 —— 只据 wait 结果。
  if [ "$WRC" -eq 0 ]; then
    log "全部完成 (${READY_DISP}/${COUNT} Ready)"
    return 0
  fi
  if [ "$READY" -ge 0 ] && [ "$READY" -ge "${COUNT}" ] && [ "${COUNT}" -gt 0 ]; then
    echo "  (wait 调用失败但实际 ${READY}/${COUNT} 已 Ready，判定通过)"
    log "全部完成 (${READY}/${COUNT} Ready)"
    return 0
  fi
  echo "  WARNING: timeout (${READY_DISP}/${COUNT} Ready)"
  [ "$READY" -lt 0 ] && echo "  注意: Ready 数查询三次均失败，无法确认实际状态，按超时处理"
  return 1
}

###############################################################################
# 清理
###############################################################################
cleanup_ds() {
  local LABEL=$1
  local FLUSH_DELAY=${QA_CLOUD_LOG_FLUSH_DELAY:-0}
  if [ "${QA_LOG_SOURCE:-cloud-logging}" = "cloud-logging" ] && [ "$FLUSH_DELAY" -gt 0 ] 2>/dev/null; then
    log "等待 Cloud Logging flush (${FLUSH_DELAY}s)"
    sleep "$FLUSH_DELAY"
  fi
  log "清理 ${LABEL}"

  # ⚠️ 这里绝不能吞掉失败。2026-07-25 pool-0015 事故：delete ds 因 API i/o timeout 失败，
  #    原代码 `|| true` 静默放过 → DCGM 的 18 个 pod 继续占满 GPU →
  #    后续 nccl-single 与 cuBLAS 全部因 Insufficient nvidia.com/gpu 失败、卡死 21 分钟。
  #    清理失败必须重试并大声报错，让调用方知道后续测试已不可信。
  local DEL_OK=0
  for _try in 1 2 3; do
    if timeout 60 "${KTL_CMD[@]}" delete ds "${LABEL}" -n ${NS} \
         --cascade=background --wait=false --ignore-not-found 2>&1; then
      DEL_OK=1; break
    fi
    echo "  [清理重试 ${_try}/3] delete ds ${LABEL} 失败，5s 后重试"
    sleep 5
  done
  [ "$DEL_OK" -eq 0 ] && echo "  ❌ delete ds ${LABEL} 三次均失败"

  timeout 30 "${KTL_CMD[@]}" delete cm -n ${NS} --all --ignore-not-found 2>/dev/null || true

  # 等 pods 消失。
  # ⚠️ 判定必须区分两类残留，否则会误报（2026-07-25 首版修复即因此把成功判成失败，
  #    进而触发 CLEANUP_BROKEN 跳过后续全部测试 —— 比原来的静默失败更糟）：
  #      · Terminating   = 正在退出（60s grace period），良性，等一会就没了
  #      · Running/Pending = 真的还占着 GPU，才是会拖垮后续测试的情况
  #    真正的硬指标是 DaemonSet 本身是否已删除。
  local WAIT_START=$SECONDS ALIVE=-1 TERMING=0
  while [ $((SECONDS - WAIT_START)) -lt 300 ]; do
    local OUT
    OUT=$(ktl_ro_retry get pods -n ${NS} -l "app=${LABEL}" \
          -o custom-columns=P:.status.phase,D:.metadata.deletionTimestamp --no-headers 2>/dev/null)
    if [ $? -eq 0 ]; then
      # deletionTimestamp 非 <none> 即为 Terminating。
      # 计数有两个坑，都实测踩过：
      #   1) `echo "$OUT"` 在 $OUT 为空时输出一个空行 → grep -vc 记成 1（0 个 pod 误判为 1 个 Terminating）
      #   2) `printf '%s' "$OUT"` 末行无换行 → 本机 grep(ugrep) 不统计该行（漏计最后一个 pod）
      # 故：先显式判空，非空时用 printf '%s\n' 补足结尾换行。
      if [ -z "$OUT" ]; then
        ALIVE=0; TERMING=0
      else
        ALIVE=$(printf '%s\n' "$OUT"  | grep -c '<none>$' || true)
        TERMING=$(printf '%s\n' "$OUT" | grep -vc '<none>$' || true)
      fi
      [ "${ALIVE}" -eq 0 ] && [ "${TERMING}" -eq 0 ] && { echo "  ✓ ${LABEL} 已清理干净"; return 0; }
      # 只剩 Terminating 且 DS 已删 → 视为成功，不再干等
      if [ "${ALIVE}" -eq 0 ]; then
        # ⚠️ 查询失败时 stdout 也是空 → grep -c 得 0 → 会误判「DS 已删除」。
        #    必须先确认查询本身成功（exit 0），失败则不下结论、继续轮询。
        local DSOUT DSRC DSLEFT=-1
        DSOUT=$(ktl_ro_retry get ds "${LABEL}" -n ${NS} --no-headers 2>/dev/null); DSRC=$?
        if [ "$DSRC" -eq 0 ]; then
          if [ -z "$DSOUT" ]; then DSLEFT=0; else DSLEFT=$(printf '%s\n' "$DSOUT" | grep -c . || true); fi
        fi
        if [ "${DSLEFT}" -eq 0 ]; then
          echo "  ✓ ${LABEL} DS 已删除，剩余 ${TERMING} 个 pod 处于 Terminating（正常退出中）"
          return 0
        fi
      fi
    fi
    sleep 5
  done

  echo "  ❌❌ 清理失败: ${LABEL} 仍有 ${ALIVE} 个存活 pod / ${TERMING} 个 Terminating（-1 表示查询失败）"
  echo "     存活 pod 会继续占用 GPU，后续测试将失败。手动处理:"
  echo "       kubectl -n ${NS} delete ds ${LABEL}"
  echo "       kubectl -n ${NS} get pods"
  return 1
}

###############################################################################
# 收集 nvidia-bug-report.log.gz（hw-check 专用，cleanup 前调用）
###############################################################################
collect_bug_reports() {
  local OUTDIR="${LOGS_BASE}/qa-bug-reports-${QA_GPU_TYPE}-${SUBBLOCK}-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$OUTDIR"
  log "收集 nvidia-bug-report → ${OUTDIR}"

  # 2026-07-25 18 节点实测：原实现只收到 8/18，且其中 1 份用 pod 名命名无法追溯。
  # 根因是三处 timeout 过紧（5/10/30s）且无重试，API 一慢就连锁失败。
  # 关键原则：**宁可不收，也不能错标** —— node 名拿不到就绝不退化用 pod 名当文件名。
  local T_NODE="${QA_BR_TIMEOUT_NODE:-15}"
  local T_EXEC="${QA_BR_TIMEOUT_EXEC:-30}"
  local T_CP="${QA_BR_TIMEOUT_CP:-120}"

  local COLLECTED=0 FAILED=0 UNRESOLVED=0
  local PODS
  PODS=$(ktl_ro_retry get pods -n ${NS} -l "app=qa-hw-check" --no-headers \
         -o custom-columns=NAME:.metadata.name 2>/dev/null)

  for POD in ${PODS}; do
    # --- 1. 解析节点名（带重试；失败则跳过，不用 pod 名冒充）---
    local NODE="" SHORT=""
    for _try in 1 2 3; do
      NODE=$(timeout ${T_NODE} "${KTL_CMD[@]}" get pod "$POD" -n ${NS} \
             -o jsonpath='{.spec.nodeName}' 2>/dev/null || true)
      [ -n "$NODE" ] && break
      sleep 3
    done
    if [ -z "$NODE" ]; then
      echo "  [SKIP] pod ${POD}: 3 次都取不到 nodeName，跳过（不生成无法追溯的文件）"
      ((UNRESOLVED++)) || true
      continue
    fi
    SHORT=$(echo "$NODE" | grep -oE '[^-]+$')

    # --- 2. 找 gz（带重试）---
    local GZ=""
    for _try in 1 2 3; do
      GZ=$(timeout ${T_EXEC} "${KTL_CMD[@]}" exec "$POD" -n ${NS} -- \
           sh -c 'ls /host/tmp/nvidia-bug-report-*.log.gz 2>/dev/null' 2>/dev/null | head -1 || true)
      [ -n "$GZ" ] && break
      sleep 3
    done
    if [ -z "$GZ" ]; then
      echo "  [FAIL] ${SHORT}: 3 次都没找到 bug-report gz"
      ((FAILED++)) || true
      continue
    fi

    # --- 3. 拷回（带重试）---
    local DST="${OUTDIR}/nvidia-bug-report-${SHORT}.log.gz"
    local OK=0
    for _try in 1 2 3; do
      if timeout ${T_CP} "${KTL_CMD[@]}" cp "${NS}/${POD}:${GZ}" "$DST" 2>/dev/null \
         && [ -s "$DST" ]; then OK=1; break; fi
      rm -f "$DST"
      sleep 5
    done
    if [ "$OK" -eq 1 ]; then
      echo "  [OK]   ${SHORT}: $(ls -lh "$DST" | awk '{print $5}')"
      ((COLLECTED++)) || true
    else
      echo "  [FAIL] ${SHORT}: cp 3 次均失败"
      ((FAILED++)) || true
    fi
  done

  # ⚠️ PODS 为空可能是「查询失败」也可能是「真的没有 pod」，直接 grep -c 会都算 0，
  #    输出「成功 0 / 共 0 pod」看起来像正常完成，实际掩盖了查询失败。
  local TOTAL=0
  [ -n "${PODS}" ] && TOTAL=$(printf '%s\n' "${PODS}" | grep -c . || true)
  if [ "${TOTAL}" -eq 0 ]; then
    echo "  ⚠ 未取到任何 hw-check pod —— 可能是查询失败而非真的没有 pod，本次未收集任何 bug-report"
    echo "     可用 DaemonSet 方式从主机直接补收: bash qa/recover-bug-reports.sh <profile> ${SUBBLOCK}"
    return 0
  fi
  log "bug-report: 成功 ${COLLECTED} / 共 ${TOTAL} pod (失败 ${FAILED}, 节点名未解析 ${UNRESOLVED}) → ${OUTDIR}/"
  if [ "${COLLECTED}" -lt "${TOTAL}" ]; then
    echo "  ⚠ 未全部收齐 ($((TOTAL - COLLECTED)) 份缺失)。推荐用 DaemonSet 方式增量补收:"
    echo "     bash qa/recover-bug-reports.sh <profile> ${SUBBLOCK} ${OUTDIR}"
  fi
}

###############################################################################
# pod 日志内容校验（wait_completion 成功后，检查测试是否真正通过）
###############################################################################
check_pod_content() {
  local LABEL=$1
  local FAIL_SIGS="CUDA.*busy\|CUDA.*unavailable\|Test CUDA failure\|Test failure\|Test NCCL failure\|INTERNAL ERROR\|unhandled system error\|No CUDA-capable\|CUDA error"
  local CONTENT_FAIL=0

  for POD in $(timeout 10 "${KTL_CMD[@]}" get pods -n ${NS} -l "app=${LABEL}" --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null); do
    local TAIL=$(timeout 10 "${KTL_CMD[@]}" logs "$POD" -n ${NS} --tail=50 2>/dev/null || true)
    if echo "$TAIL" | grep -qi "$FAIL_SIGS" 2>/dev/null; then
      local SHORT=$(echo "$POD" | grep -oE '[^-]+$')
      local SIG=$(echo "$TAIL" | grep -oi "$FAIL_SIGS" | head -1)
      echo "  [CONTENT_FAIL] ${SHORT}: ${SIG}"
      ((CONTENT_FAIL++)) || true
    fi
  done

  return $CONTENT_FAIL
}

###############################################################################
# 运行单个测试
###############################################################################
run_test() {
  local YAML=$1 LABEL=$2 MARKER=$3 TIMEOUT=$4
  local RC=0

  # 上一项清理失败 → GPU 仍被占，本项必然 Pending 到超时。直接跳过，不做无用功。
  if [ "${CLEANUP_BROKEN:-0}" -eq 1 ]; then
    echo ""
    echo "  ⏭  跳过 ${LABEL}: 前序测试清理失败，GPU 未释放，运行本项只会超时"
    echo "     先手动清理 namespace ${NS} 内残留 DaemonSet 后重跑"
    ((FAIL_COUNT++)) || true
    return 1
  fi

  local TEST_START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if ! apply_yaml "$YAML" "$SUBBLOCK"; then
    echo "  SKIP: ${LABEL} (apply 失败)"
    ((FAIL_COUNT++)) || true
    return 1
  fi
  if ! wait_completion "$LABEL" "$MARKER" "$TIMEOUT"; then
    RC=1
    ((FAIL_COUNT++)) || true
    echo "${NS}|${LABEL}|${MARKER}|${SUBBLOCK}|${TEST_START_TS}|TIMEOUT" >> "$MANIFEST_FILE"
  else
    # pod Ready 不等于测试通过 — 校验日志内容
    if ! check_pod_content "$LABEL"; then
      RC=1
      ((FAIL_COUNT++)) || true
      echo "${NS}|${LABEL}|${MARKER}|${SUBBLOCK}|${TEST_START_TS}|CONTENT_FAIL" >> "$MANIFEST_FILE"
    else
      echo "${NS}|${LABEL}|${MARKER}|${SUBBLOCK}|${TEST_START_TS}" >> "$MANIFEST_FILE"
    fi
  fi

  # hw-check: cleanup 前收集 nvidia-bug-report
  [ "$LABEL" = "qa-hw-check" ] && collect_bug_reports

  # 清理失败 = 后续测试必然因 GPU 被占而失败，置全局阻断标志，避免级联浪费
  if ! cleanup_ds "$LABEL"; then
    CLEANUP_BROKEN=1
    ((FAIL_COUNT++)) || true
    RC=1
  fi
  return $RC
}

###############################################################################
# Main
###############################################################################
case "$ACTION" in
  hw-check|hw)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> hw-check <subblock> [node]" && exit 1
    run_test hw-check.yaml qa-hw-check "Summary:" "${QA_TIMEOUT_HW}"
    ;;

  nccl-single|nccl)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> nccl <subblock> [node]" && exit 1
    run_test nccl-single-node.yaml qa-nccl-single "Done:" "${QA_TIMEOUT_NCCL}"
    ;;

  gemm|cublas)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> gemm <subblock> [node]" && exit 1
    run_test cublas-bench.yaml qa-cublas-bench "DONE:" "${QA_TIMEOUT_CUBLAS}"
    ;;

  dcgm)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> dcgm <subblock> [node] (level=${QA_DCGM_LEVEL:-2})" && exit 1
    export QA_DCGM_LEVEL="${QA_DCGM_LEVEL:-2}"
    run_test dcgm-diag.yaml qa-dcgm-diag "DONE:" "${QA_TIMEOUT_DCGM:-600}"
    ;;

  nccl-multi)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> nccl-multi <subblock> [--mnnvl=on|off]" && exit 1
    MNNVL_FLAG="${NODE_SUFFIX:-off}"

    export QA_POOL
    QA_POOL=$(resolve_pool "$SUBBLOCK")
    # ⚠️ 查询失败会输出空 → wc -l 得 0 → 误判「0 台健康节点」并 exit 1，把整轮补跑打死。
    #    2026-07-25 pool-0016 MNNVL=ON 补跑即因此启动即失败（实有 18 台 Ready）。
    #    必须重试，并把「查询失败」与「真的节点不足」分开报。
    HEALTHY=-1
    for _try in 1 2 3; do
      _hn=$(ktl_ro_retry get nodes -l "${QA_NODE_SELECTOR_KEY}=${QA_POOL}" --no-headers 2>/dev/null)
      if [ $? -eq 0 ] && [ -n "$_hn" ]; then
        HEALTHY=$(printf '%s\n' "$_hn" | grep -vc SchedulingDisabled || true)
        break
      fi
      echo "  [健康节点查询重试 ${_try}/3] 未取到节点列表，5s 后重试"
      sleep 5
    done
    if [ "$HEALTHY" -lt 0 ]; then
      echo "ERROR: 无法查询 ${QA_POOL} 的节点列表（3 次均失败）—— 是查询问题，不是节点不足"
      echo "       请确认 kubectl 可达后重试: kubectl get nodes -l ${QA_NODE_SELECTOR_KEY}=${QA_POOL}"
      exit 1
    fi
    if [ "$HEALTHY" -lt 2 ]; then
      echo "ERROR: ${QA_POOL} 健康节点 ${HEALTHY} 台（已确认查询成功），至少需要 2 台"
      exit 1
    fi
    export QA_NCCL_NUM_NODES="$HEALTHY"

    case "$MNNVL_FLAG" in
      --mnnvl=on|on)
        export QA_NCCL_MNNVL=2 QA_NCCL_NVLS=1 QA_NCCL_CUMEM=1
        log "多节点 NCCL: ${QA_POOL}, ${HEALTHY} nodes, MNNVL=ON (测 NVSwitch)"
        ;;
      *)
        export QA_NCCL_MNNVL=0 QA_NCCL_NVLS=0 QA_NCCL_CUMEM=0
        log "多节点 NCCL: ${QA_POOL}, ${HEALTHY} nodes, MNNVL=OFF (测 RDMA NIC)"
        ;;
    esac

    preflight_check "$QA_POOL"

    log "Apply nccl-multi-node.yaml → ${QA_POOL} (${HEALTHY} nodes)"
    qa_envsubst < "${TEMPLATE_DIR}/nccl-multi-node.yaml" | ktl apply -f -

    # 等 JobSet 完成（rank 0 pod 输出 DONE）
    log "等待 JobSet 完成 (timeout ${QA_TIMEOUT_NCCL_MULTI}s)"
    START_TIME=$SECONDS
    while [ $((SECONDS - START_TIME)) -lt "${QA_TIMEOUT_NCCL_MULTI}" ]; do
      # 检查 rank 0 pod
      RANK0=$(ktl get pods -n ${NS} -l "jobset.sigs.k8s.io/jobset-name=qa-nccl-multi" --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | sort | head -1)
      if [ -n "$RANK0" ]; then
        PHASE=$(ktl get pod "$RANK0" -n ${NS} -o jsonpath='{.status.phase}' 2>/dev/null)
        [ "$PHASE" = "Succeeded" ] && { log "JobSet 完成 (rank 0 Succeeded)"; break; }
        [ "$PHASE" = "Failed" ] && { log "JobSet 失败 (rank 0 Failed)"; break; }
        # 检查日志中的 DONE 标记
        timeout 5 "${KTL_CMD[@]}" logs "$RANK0" -n ${NS} --tail=3 2>/dev/null | grep -q "DONE" && { log "JobSet 完成 (DONE in logs)"; break; }
      fi
      ELAPSED=$((SECONDS - START_TIME))
      [ $((ELAPSED % 60)) -lt 15 ] && [ $ELAPSED -gt 0 ] && echo "  [$(date +%H:%M:%S)] 等待中... (${ELAPSED}s)"
      sleep 15
    done

    # 收集 rank 0 日志
    LOGDIR="${LOGS_BASE}/qa-nccl-multi-${QA_GPU_TYPE}-${SUBBLOCK}-mnnvl${QA_NCCL_MNNVL}-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$LOGDIR"
    # ⚠️ 2026-07-25 pool-0016 MNNVL=ON：pod 查询失败 → 既没保存也没报错，
    #    脚本径直宣告「全面质检完成」，NVLink 数据静默丢失。
    #    这里必须重试、校验内容、失败时大声报错。
    RANK0=""
    for _try in 1 2 3; do
      RANK0=$(ktl_ro_retry get pods -n ${NS} -l "jobset.sigs.k8s.io/jobset-name=qa-nccl-multi" \
              --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | sort | head -1)
      [ -n "$RANK0" ] && break
      echo "  [rank0 查询重试 ${_try}/3] 未取到 pod，5s 后重试"
      sleep 5
    done

    if [ -z "$RANK0" ]; then
      echo "  ❌❌ 无法获取 rank0 pod，本轮 MNNVL=${QA_NCCL_MNNVL} 数据丢失"
      echo "     该轮结果不可用，需重跑: bash $0 <profile> nccl-multi ${SUBBLOCK} --mnnvl=${MNNVL_FLAG#--mnnvl=}"
      ((FAIL_COUNT++)) || true
    else
      SAVE_OK=0
      for _try in 1 2 3; do
        if timeout 120 "${KTL_CMD[@]}" logs "$RANK0" -n ${NS} > "${LOGDIR}/rank0.log" 2>/dev/null \
           && [ -s "${LOGDIR}/rank0.log" ] \
           && grep -q "Collective test" "${LOGDIR}/rank0.log" 2>/dev/null; then
          SAVE_OK=1; break
        fi
        echo "  [rank0 日志保存重试 ${_try}/3] 空文件或内容不完整，5s 后重试"
        sleep 5
      done
      if [ "$SAVE_OK" -eq 1 ]; then
        log "日志已保存: ${LOGDIR}/rank0.log ($(wc -c < "${LOGDIR}/rank0.log")b)"
      else
        echo "  ❌❌ rank0 日志保存失败（空或无 'Collective test' 内容），本轮数据不可用"
        echo "     pod=${RANK0}  目标=${LOGDIR}/rank0.log"
        echo "     若 pod 仍在可手动补: kubectl -n ${NS} logs ${RANK0} > ${LOGDIR}/rank0.log"
        ((FAIL_COUNT++)) || true
      fi
    fi

    # 清理
    log "清理 JobSet"
    ktl delete jobset qa-nccl-multi -n ${NS} --ignore-not-found 2>/dev/null || true
    ktl delete computedomain qa-nccl-multi-cd -n ${NS} --ignore-not-found 2>/dev/null || true
    ktl delete resourceclaimtemplate -n ${NS} --all --ignore-not-found 2>/dev/null || true
    ktl delete cm -n ${NS} --all --ignore-not-found 2>/dev/null || true
    ;;

  nccl-cross)
    [ -z "$SUBBLOCK" ] || [ -z "$NODE_SUFFIX" ] && echo "用法: $0 <profile> nccl-cross <sub1> <sub2>" && exit 1
    SUB1="$SUBBLOCK"
    SUB2="$NODE_SUFFIX"
    export QA_POOL_1=$(resolve_pool "$SUB1")
    export QA_POOL_2=$(resolve_pool "$SUB2")
    # 可调度节点数
    N1=$(ktl get nodes -l "${QA_NODE_SELECTOR_KEY}=${QA_POOL_1}" --no-headers 2>/dev/null | grep -vc SchedulingDisabled)
    N2=$(ktl get nodes -l "${QA_NODE_SELECTOR_KEY}=${QA_POOL_2}" --no-headers 2>/dev/null | grep -vc SchedulingDisabled)
    CROSS_MIN=$((N1 < N2 ? N1 : N2))
    export QA_NCCL_NODES_1="$CROSS_MIN"
    export QA_NCCL_NODES_2="$CROSS_MIN"
    export QA_NCCL_CROSS_NODES=$((CROSS_MIN * 2))
    export QA_NCCL_MNNVL=2
    export QA_NCCL_NVLS=1
    export QA_NCCL_CUMEM=1
    export QA_NAMESPACE="qa-cd-${SUB1}-${SUB2}"

    log "跨域 NCCL: ${QA_POOL_1}(${N1}) + ${QA_POOL_2}(${N2}) → 每域${CROSS_MIN}节点, 共${QA_NCCL_CROSS_NODES}节点/${QA_NCCL_CROSS_NODES}*${QA_GPUS_PER_NODE}GPU, MNNVL=2"

    TEST_START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    qa_envsubst < "${TEMPLATE_DIR}/nccl-cross-domain.yaml" | ktl apply -f - --validate=false 2>&1

    # ktl wait rank 0 完成
    log "等待 rank 0 完成 (timeout ${QA_TIMEOUT_NCCL_MULTI}s)"
    RANK0=""
    for i in $(seq 1 60); do
      RANK0=$(timeout 8 "${KTL_CMD[@]}" get pods -n ${QA_NAMESPACE} -l 'jobset.sigs.k8s.io/replicatedjob-name=d1' --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | sort | head -1)
      [ -n "$RANK0" ] && break
      sleep 5
    done
    if [ -n "$RANK0" ]; then
      timeout $((QA_TIMEOUT_NCCL_MULTI + 60)) "${KTL_CMD[@]}" wait pod "$RANK0" -n ${QA_NAMESPACE} \
        --for=condition=Ready --timeout="${QA_TIMEOUT_NCCL_MULTI}s" 2>&1 | tail -1
      log "rank 0 完成"
    else
      log "ERROR: 无 rank 0 pod"
    fi

    # 写 manifest
    echo "${QA_NAMESPACE}|nccl-cd|DONE:|cross-${SUB1}-${SUB2}|${TEST_START_TS}" >> "$MANIFEST_FILE"

    # 清理
    log "清理 JobSet"
    timeout 30 "${KTL_CMD[@]}" delete jobset nccl-cd -n ${QA_NAMESPACE} --cascade=background --wait=false --ignore-not-found 2>&1 || true
    timeout 30 "${KTL_CMD[@]}" delete computedomain -n ${QA_NAMESPACE} --all --ignore-not-found 2>&1 || true
    timeout 30 "${KTL_CMD[@]}" delete resourceclaimtemplate -n ${QA_NAMESPACE} --all --ignore-not-found 2>&1 || true

    log "跨域 NCCL 完成 (manifest: ${MANIFEST_FILE})"
    ;;

  all)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> all <subblock> [node]" && exit 1
    log "全部单节点测试: ${QA_GPU_TYPE} subblock ${SUBBLOCK}${NODE_SUFFIX:+ node *${NODE_SUFFIX}}"
    run_test hw-check.yaml qa-hw-check "Summary:" "${QA_TIMEOUT_HW}"
    export QA_DCGM_LEVEL="${QA_DCGM_LEVEL:-2}"
    run_test dcgm-diag.yaml qa-dcgm-diag "DONE:" "${QA_TIMEOUT_DCGM:-600}"
    run_test nccl-single-node.yaml qa-nccl-single "Done:" "${QA_TIMEOUT_NCCL}"
    run_test cublas-bench.yaml qa-cublas-bench "DONE:" "${QA_TIMEOUT_CUBLAS}"
    if [ "$FAIL_COUNT" -gt 0 ]; then
      log "全部单节点测试完成 — ${FAIL_COUNT} 项失败 (manifest: ${MANIFEST_FILE})"
      exit 1
    fi
    log "全部单节点测试完成 (manifest: ${MANIFEST_FILE})"
    ;;

  all-full)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> all-full <subblock>" && exit 1
    log "全面质检 (单节点 + 多节点 MNNVL+RDMA): ${QA_GPU_TYPE} subblock ${SUBBLOCK}"
    run_test hw-check.yaml qa-hw-check "Summary:" "${QA_TIMEOUT_HW}" || true
    export QA_DCGM_LEVEL="${QA_DCGM_LEVEL:-2}"
    run_test dcgm-diag.yaml qa-dcgm-diag "DONE:" "${QA_TIMEOUT_DCGM:-600}" || true
    run_test nccl-single-node.yaml qa-nccl-single "Done:" "${QA_TIMEOUT_NCCL}" || true
    run_test cublas-bench.yaml qa-cublas-bench "DONE:" "${QA_TIMEOUT_CUBLAS}" || true
    log "单节点测试完成 (${FAIL_COUNT} 失败)，启动多节点 NCCL"
    "$0" "$PROFILE" nccl-multi "$SUBBLOCK" --mnnvl=off || ((FAIL_COUNT++)) || true
    "$0" "$PROFILE" nccl-multi "$SUBBLOCK" --mnnvl=on || ((FAIL_COUNT++)) || true
    if [ "$FAIL_COUNT" -gt 0 ]; then
      log "全面质检完成 — ${FAIL_COUNT} 项失败 (manifest: ${MANIFEST_FILE})"
      exit 1
    fi
    log "全面质检完成 (manifest: ${MANIFEST_FILE})"
    ;;

  logs)
    echo "=== 当前 ${NS} pods ==="
    ktl get pods -n ${NS} -o wide --no-headers 2>/dev/null || echo "无 pods"
    echo ""
    echo "=== 最近日志 ==="
    ls -td "${LOGS_BASE}"/qa-* 2>/dev/null | head -5 || echo "无日志"
    ;;

  clean)
    if [ -n "$SUBBLOCK" ]; then
      log "清理 namespace ${NS}"
      ktl delete ns ${NS} --ignore-not-found --wait=false
    else
      log "清理全部 gpu-qa-* namespaces"
      ktl get ns --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | grep "^gpu-qa" | while read N; do
        echo "  删除 ${N}"
        ktl delete ns "$N" --ignore-not-found --wait=false 2>/dev/null
      done
    fi
    ;;

  *)
    echo "未知 action: $ACTION"
    echo "可用: hw-check | dcgm | nccl | gemm | nccl-multi | nccl-cross | all | all-full | logs | clean"
    exit 1
    ;;
esac
