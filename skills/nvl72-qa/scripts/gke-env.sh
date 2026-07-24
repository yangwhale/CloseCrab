#!/bin/bash
# GB300 GKE 部署环境变量
# 基于 env.sh 扩展，GKE 特有配置
#
# 部署顺序:
#   1. bash scripts/gke-create-cluster.sh       # GKE 集群 + default pool（复用已有 VPC）
#   2. bash scripts/gke-create-nodepool.sh 0001 # GPU 节点池（每 sub-block 一个）
#   3. bash scripts/gke-post-install.sh         # asapd-lite + DRA driver
#
# 参考:
#   官方文档: https://docs.cloud.google.com/ai-hypercomputer/docs/create/gke-ai-hypercompute-custom-a4x-max
#   已验证集群: gb300-gke-test (project: tencent-gcp-taiji-poc, 2026-07-13)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/env.sh"

###############################################################################
# GKE 集群
###############################################################################
export GKE_CLUSTER="gb300-gke-test"
export GKE_RELEASE_CHANNEL="rapid"
# GKE 版本要求: 1.35.0-gke.2745000+ 或 1.34.3-gke.1318000+
# RAPID channel 当前: 1.36.0-gke.4447000 (gb300-gke-test 已验证)

###############################################################################
# ⚠️ Node version PIN（避免 broken node image）
#
# 背景 (2026-07-24)：GKE RAPID channel default 现在是 1.36.0-gke.4681000，
# 该版本 COS image `19506.224.80` 上 NVIDIA 580.159.04 open kernel module
# (build Jun 27) 有 CUDA context-create regression：
#   cuCtxCreate_v2 / cuDevicePrimaryCtxRetain → CUDA_ERROR_INVALID_VALUE (1)
# 所有需要 cudaSetDevice/cudaMalloc 的 workload (DCGM Level 2 / NCCL-tests /
# cuBLAS / 训练脚本) 全部挂，dmesg 无 NVRM 报错（driver 静默 refuse）。
#
# workaround: pin 到 1.36.0-gke.4447000 (COS 19506.224.49, nvidia.ko Jun 18)，
# 已在 pool-0002/0003/0005/0007 等验证过 CUDA API 全通、cuBLAS/NCCL/DCGM 均正常。
#
# 前提校验：
#   1. 用 `gcloud container get-server-config` 确认 4447000 仍在 cluster channel
#      的 validVersions 里（RAPID channel 可能会 deprecate 老版本）
#   2. 若 GKE 拒绝（channel mismatch），退路：
#      (a) 把 cluster 从 RAPID 切到 REGULAR channel（cluster-wide 变更，慎）
#      (b) 或用 --release-channel=None 让 pool opt-out channel
#      (c) 或选 RAPID 里更旧的稳定版（如 1.35.6-gke.1258000）测试是否也无此 bug
#
# 什么时候可以放开：GKE / NVIDIA 出新 image (nvidia.ko build 修复) 后，跑一次
# probe (cuCtxCreate_v2 + cuDevicePrimaryCtxRetain) 全通 → 可清空此 pin。
###############################################################################
export GKE_NODE_VERSION="${GKE_NODE_VERSION:-1.36.0-gke.4447000}"

###############################################################################
# 网络（直接复用 gb300-gke-test 已建好的 VPC + 子网 + Cloud NAT）
# RDMA 由 --accelerator-network-profile=auto 自动创建
###############################################################################
export GKE_VPC="gb300-gke-mgmt"                 # mtu=8896, custom, Cloud Router+NAT 已有
export GKE_SUBNET="gb300-gke-sub-${REGION}"     # 10.100.0.0/24, IPv4-only, Private Google Access
export GKE_POD_CIDR="10.76.0.0/14"              # 不与 gke-test 10.72.0.0/14 重叠
export GKE_SVC_CIDR="10.80.0.0/20"
export GKE_MASTER_CIDR="172.16.1.0/28"          # gke-test 用了 172.16.0.0/28

###############################################################################
# 授权网络（Master API 访问控制）
# 设为你的出口 IP，例如:
#   export GKE_AUTHORIZED_NETWORK=$(curl -s ifconfig.me)/32
# 不设则脚本自动检测
###############################################################################
export GKE_AUTHORIZED_NETWORK="${GKE_AUTHORIZED_NETWORK:-}"

###############################################################################
# 节点池
###############################################################################
export GKE_NODEPOOL_PREFIX="gb300-pool"
export GKE_NODES_PER_POOL=18            # 18 nodes = 72 GPU = 完整 1x72 NVLink domain
export GKE_HUGEPAGE_2M_COUNT=4096       # 2MB hugepages 预分配数

###############################################################################
# Sub-block 分配
#
# 总 12 个 sub-block (0001-0012)，每个 18 VM × 4 GPU = 72 GPU
#
# 已占用:
#   self-managed k8s:       0008, 0009
#   gke-test 已有节点池:    0003 (pool-3), 0004 (pool-4), 0005 (pool/ERROR), 0006 (pool-2)
#
# 可用: 0001, 0002, 0007, 0010, 0011, 0012
###############################################################################
export GKE_SUBBLOCKS="${GKE_SUBBLOCKS:-0011 0012}"

###############################################################################
# NVIDIA DRA Driver
# chart: nvidia/dra-driver-nvidia-gpu (0.4.x 系列)
# 注意: 官方 GKE 文档用 nvidia/nvidia-dra-driver-gpu v25.8.0，这里对齐自管集群用 0.4.1
###############################################################################
export DRA_DRIVER_VERSION="0.4.1"
export DRA_CHART="nvidia/dra-driver-nvidia-gpu"
export DRA_NS="nvidia-dra-driver-gpu"

###############################################################################
# asapd-lite（MRDMA NIC 配置 DaemonSet）
###############################################################################
export ASAPD_MANIFEST="https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/refs/heads/master/asapd-lite-installer/asapd-lite-installer-a4x-max-bm-cos.yaml"
