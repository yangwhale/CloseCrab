#!/bin/bash
# GPU QA 环境配置工具 — 检测并安装 GKE 集群所需的 k8s 组件
# 幂等：已存在的组件跳过，缺失的自动安装
#
# 用法:
#   bash qa/setup-env.sh qa/profiles/gb300-gke-taiji.sh
#   bash qa/setup-env.sh qa/profiles/gb200-gke-taiji.sh
#
# 依赖: kubectl, helm (DRA driver 需要)
set -uo pipefail

###############################################################################
# 基础设施
###############################################################################
log() { echo "=== [$(date +%H:%M:%S)] $* ==="; }

PROFILE="${1:-}"
if [ -z "$PROFILE" ] || [ "$PROFILE" = "--help" ] || [ "$PROFILE" = "-h" ]; then
  echo "GPU QA 环境配置工具"
  echo ""
  echo "用法: $0 <profile>"
  echo ""
  echo "示例:"
  echo "  $0 qa/profiles/gb300-gke-taiji.sh"
  echo "  $0 qa/profiles/gb200-gke-taiji.sh"
  echo ""
  echo "检测并安装: DRA Driver, IMEX Channel Init, MPI Operator, JobSet"
  exit 0
fi

if [ ! -f "$PROFILE" ]; then
  echo "ERROR: profile 不存在: $PROFILE"
  exit 1
fi

source "$PROFILE"

###############################################################################
# 变量与默认值
###############################################################################
CTX="${QA_KUBE_CONTEXT:-}"
DRA_VERSION="${QA_DRA_VERSION:-0.4.1}"
MPI_VERSION="${QA_MPI_VERSION:-0.8.2}"
JOBSET_VERSION="${QA_JOBSET_VERSION:-0.12.0}"
GPU_TYPE="${QA_GPU_TYPE:?QA_GPU_TYPE not set in profile}"
DRIVER_ROOT="${QA_DRIVER_ROOT:-/home/kubernetes/bin/nvidia}"

# kubectl 包装：始终带 --context（如果设了）
kc() {
  if [ -n "$CTX" ]; then
    kubectl --context="${CTX}" "$@"
  else
    kubectl "$@"
  fi
}

# 加速器 label: gb200 → nvidia-gb200, gb300 → nvidia-gb300
ACCELERATOR_LABEL="nvidia-${GPU_TYPE}"

log "Profile: $(basename "$PROFILE") (GPU=${GPU_TYPE}, DRA=${DRA_VERSION}, MPI=${MPI_VERSION}, JobSet=${JOBSET_VERSION})"
if [ -n "$CTX" ]; then
  log "kube-context: ${CTX}"
else
  log "kube-context: (当前默认)"
fi

###############################################################################
# 状态追踪
###############################################################################
declare -A STATUS
COMPONENTS="dra-driver imex-channel-init mpi-operator jobset"
for C in $COMPONENTS; do
  STATUS[$C]="pending"
done
FAILED=0

###############################################################################
# 等 pods Ready（最多 wait_secs 秒）
###############################################################################
wait_pods_ready() {
  local NS=$1 LABEL=$2 WAIT_SECS=${3:-60} KIND=${4:-pods}
  local ELAPSED=0
  echo "  等待 ${KIND} Ready (ns=${NS}, label=${LABEL}, timeout=${WAIT_SECS}s)..."
  while [ $ELAPSED -lt $WAIT_SECS ]; do
    local TOTAL READY
    TOTAL=$(kc get pods -n "$NS" -l "$LABEL" --no-headers 2>/dev/null | wc -l)
    READY=$(kc get pods -n "$NS" -l "$LABEL" --no-headers 2>/dev/null | grep -cE 'Running|Completed' || true)
    if [ "$TOTAL" -gt 0 ] && [ "$READY" -eq "$TOTAL" ]; then
      echo "  ${KIND} Ready: ${READY}/${TOTAL}"
      return 0
    fi
    sleep 5
    ELAPSED=$((ELAPSED + 5))
    [ $((ELAPSED % 15)) -eq 0 ] && echo "  [$(date +%H:%M:%S)] ${KIND}: ${READY:-0}/${TOTAL:-0} Ready (${ELAPSED}s)"
  done
  echo "  WARNING: ${KIND} 未全部 Ready (${READY:-0}/${TOTAL:-0}, ${WAIT_SECS}s timeout)"
  return 1
}

###############################################################################
# 1. DRA Driver
###############################################################################
install_dra_driver() {
  log "检查 DRA Driver (v${DRA_VERSION})"

  if kc get ds dra-driver-nvidia-gpu-kubelet-plugin -n nvidia-dra-driver-gpu &>/dev/null; then
    echo "  DRA Driver 已存在"
    STATUS[dra-driver]="already-present"
    return 0
  fi

  echo "  DRA Driver 未安装，开始 helm install..."

  # 确认 helm 可用
  if ! command -v helm &>/dev/null; then
    echo "  ERROR: helm 未安装"
    STATUS[dra-driver]="failed"
    FAILED=$((FAILED + 1))
    return 1
  fi

  # 添加 nvidia helm repo
  helm repo add nvidia https://helm.ngc.nvidia.com/nvidia 2>/dev/null || true
  helm repo update nvidia 2>/dev/null || true

  # 构造 helm install 命令
  local HELM_ARGS=(
    install dra-driver-nvidia-gpu nvidia/dra-driver-nvidia-gpu
    --namespace nvidia-dra-driver-gpu
    --create-namespace
    --version "v${DRA_VERSION}"
    --set "nvidiaDriverRoot=${DRIVER_ROOT}"
    --set "resources.gpus.enabled=false"
    --set "allowDefaultNamespace=true"
    --set "kubeletPlugin.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].key=cloud.google.com/gke-accelerator"
    --set "kubeletPlugin.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].operator=In"
    --set "kubeletPlugin.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].values[0]=${ACCELERATOR_LABEL}"
    --set "kubeletPlugin.tolerations[0].key=nvidia.com/gpu"
    --set "kubeletPlugin.tolerations[0].operator=Exists"
    --set "kubeletPlugin.tolerations[0].effect=NoSchedule"
    --set "kubeletPlugin.tolerations[1].key=kubernetes.io/arch"
    --set "kubeletPlugin.tolerations[1].operator=Equal"
    --set "kubeletPlugin.tolerations[1].value=arm64"
    --set "kubeletPlugin.tolerations[1].effect=NoSchedule"
    --set "controller.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].key=nvidia.com/gpu"
    --set "controller.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].operator=DoesNotExist"
  )

  if [ -n "$CTX" ]; then
    HELM_ARGS+=(--kube-context "${CTX}")
  fi

  if helm "${HELM_ARGS[@]}" 2>&1; then
    echo "  DRA Driver helm install 成功"
  else
    echo "  ERROR: DRA Driver helm install 失败"
    STATUS[dra-driver]="failed"
    FAILED=$((FAILED + 1))
    return 1
  fi

  # ResourceQuota: 防止 pod 数量不足（DRA 需要 2x GPU 节点数 + 1）
  if ! kc get resourcequota dra-pod-quota -n nvidia-dra-driver-gpu &>/dev/null; then
    echo "  创建 ResourceQuota (pods: 500)..."
    kc apply -f - <<'QUOTA_EOF'
apiVersion: v1
kind: ResourceQuota
metadata:
  name: dra-pod-quota
  namespace: nvidia-dra-driver-gpu
spec:
  hard:
    pods: "500"
  scopeSelector:
    matchExpressions:
    - scopeName: PriorityClass
      operator: In
      values:
      - system-node-critical
      - system-cluster-critical
QUOTA_EOF
  else
    echo "  ResourceQuota 已存在"
  fi

  # rollout restart 确保 DRA 组件拿到最新配置
  echo "  rollout restart DRA 组件..."
  kc rollout restart ds dra-driver-nvidia-gpu-kubelet-plugin -n nvidia-dra-driver-gpu 2>/dev/null || true
  kc rollout restart deployment dra-driver-nvidia-gpu-controller -n nvidia-dra-driver-gpu 2>/dev/null || true

  if wait_pods_ready "nvidia-dra-driver-gpu" "app.kubernetes.io/instance=dra-driver-nvidia-gpu" 60 "DRA Driver"; then
    STATUS[dra-driver]="installed"
  else
    STATUS[dra-driver]="failed"
    FAILED=$((FAILED + 1))
  fi
}

###############################################################################
# 2. IMEX Channel Init DaemonSet
###############################################################################
install_imex_channel_init() {
  log "检查 IMEX Channel Init DaemonSet"

  if kc get ds imex-channel-init -n kube-system &>/dev/null; then
    echo "  IMEX Channel Init 已存在"
    STATUS[imex-channel-init]="already-present"
    return 0
  fi

  echo "  IMEX Channel Init 未安装，部署 DaemonSet..."

  kc apply -f - <<IMEX_EOF
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: imex-channel-init
  namespace: kube-system
  labels:
    app: imex-channel-init
spec:
  selector:
    matchLabels:
      app: imex-channel-init
  template:
    metadata:
      labels:
        app: imex-channel-init
    spec:
      hostPID: true
      nodeSelector:
        cloud.google.com/gke-accelerator: "${ACCELERATOR_LABEL}"
      tolerations:
      - key: nvidia.com/gpu
        operator: Exists
        effect: NoSchedule
      - key: kubernetes.io/arch
        operator: Equal
        value: arm64
        effect: NoSchedule
      initContainers:
      - name: init-imex-channels
        image: busybox:1.36
        securityContext:
          privileged: true
        command:
        - /bin/sh
        - -c
        - |
          DEVDIR=/host-dev/nvidia-caps-imex-channels
          mkdir -p "\$DEVDIR"
          for i in \$(seq 0 255); do
            DEV="\$DEVDIR/channel\$i"
            if [ ! -e "\$DEV" ]; then
              mknod "\$DEV" c 240 "\$i"
              chmod 666 "\$DEV"
            fi
          done
          echo "IMEX channels initialized (256 devices)"
        volumeMounts:
        - name: dev
          mountPath: /host-dev
      containers:
      - name: pause
        image: registry.k8s.io/pause:3.10
      volumes:
      - name: dev
        hostPath:
          path: /dev
          type: DirectoryOrCreate
IMEX_EOF

  if [ $? -eq 0 ]; then
    echo "  IMEX Channel Init DaemonSet 已部署"
    if wait_pods_ready "kube-system" "app=imex-channel-init" 60 "IMEX Channel Init"; then
      STATUS[imex-channel-init]="installed"
    else
      STATUS[imex-channel-init]="failed"
      FAILED=$((FAILED + 1))
    fi
  else
    echo "  ERROR: IMEX Channel Init 部署失败"
    STATUS[imex-channel-init]="failed"
    FAILED=$((FAILED + 1))
  fi
}

###############################################################################
# 3. MPI Operator
###############################################################################
install_mpi_operator() {
  log "检查 MPI Operator (v${MPI_VERSION})"

  if kc get deploy mpi-operator -n mpi-operator &>/dev/null; then
    echo "  MPI Operator 已存在"
    STATUS[mpi-operator]="already-present"
    return 0
  fi

  echo "  MPI Operator 未安装，部署 v${MPI_VERSION}..."

  local URL="https://raw.githubusercontent.com/kubeflow/mpi-operator/v${MPI_VERSION}/deploy/v2beta1/mpi-operator.yaml"

  if kc apply --server-side -f "$URL" 2>&1; then
    echo "  MPI Operator manifest applied"
    if wait_pods_ready "mpi-operator" "app=mpi-operator" 60 "MPI Operator"; then
      STATUS[mpi-operator]="installed"
    else
      STATUS[mpi-operator]="failed"
      FAILED=$((FAILED + 1))
    fi
  else
    echo "  ERROR: MPI Operator 部署失败"
    STATUS[mpi-operator]="failed"
    FAILED=$((FAILED + 1))
  fi
}

###############################################################################
# 4. JobSet
###############################################################################
install_jobset() {
  log "检查 JobSet (v${JOBSET_VERSION})"

  if kc get deploy jobset-controller-manager -n jobset-system &>/dev/null; then
    echo "  JobSet 已存在"
    STATUS[jobset]="already-present"
    return 0
  fi

  echo "  JobSet 未安装，部署 v${JOBSET_VERSION}..."

  local URL="https://github.com/kubernetes-sigs/jobset/releases/download/v${JOBSET_VERSION}/manifests.yaml"

  if kc apply --server-side -f "$URL" 2>&1; then
    echo "  JobSet manifest applied"
    if wait_pods_ready "jobset-system" "control-plane=controller-manager" 60 "JobSet"; then
      STATUS[jobset]="installed"
    else
      STATUS[jobset]="failed"
      FAILED=$((FAILED + 1))
    fi
  else
    echo "  ERROR: JobSet 部署失败"
    STATUS[jobset]="failed"
    FAILED=$((FAILED + 1))
  fi
}

###############################################################################
# 5. 验证 CRD 和 ResourceSlice
###############################################################################
verify_crds() {
  log "验证 ComputeDomain CRD 和 ResourceSlice"

  local CRD_OK=true

  # ComputeDomain CRD
  if kc get crd computedomains.resource.nvidia.com &>/dev/null; then
    echo "  ComputeDomain CRD: OK"
  else
    echo "  WARNING: ComputeDomain CRD 不存在 (DRA driver 可能需要手动安装 CRD)"
    CRD_OK=false
  fi

  # ResourceSlice（DRA driver 发布的 GPU 资源）
  local SLICE_COUNT
  SLICE_COUNT=$(kc get resourceslices --no-headers 2>/dev/null | wc -l)
  if [ "$SLICE_COUNT" -gt 0 ]; then
    echo "  ResourceSlice: ${SLICE_COUNT} 个已发布"
  else
    echo "  WARNING: 无 ResourceSlice (DRA kubelet-plugin 可能未 Ready)"
  fi
}

###############################################################################
# Main
###############################################################################
install_dra_driver
install_imex_channel_init
install_mpi_operator
install_jobset
verify_crds

###############################################################################
# 汇总
###############################################################################
log "环境配置完成"
echo ""
printf "  %-22s %s\n" "组件" "状态"
printf "  %-22s %s\n" "----------------------" "----------------"
for C in $COMPONENTS; do
  printf "  %-22s %s\n" "$C" "${STATUS[$C]}"
done
echo ""

if [ $FAILED -gt 0 ]; then
  echo "ERROR: ${FAILED} 个组件安装失败"
  exit 1
fi

log "全部组件就绪"
exit 0
