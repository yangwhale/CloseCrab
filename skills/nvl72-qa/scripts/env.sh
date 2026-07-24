#!/bin/bash
# GB300 (A4X MAX) 集群环境变量
# 所有脚本通过 source scripts/env.sh 引用，修改一处全局生效
#
# 对齐官方文档: https://docs.cloud.google.com/ai-hypercomputer/docs/create/create-a4xmax-instance

###############################################################################
# 项目 & 凭证
###############################################################################
export PROJECT="tencent-gcp-taiji-poc"
export GCLOUD_CONFIG="taiji-poc"

###############################################################################
# 区域
###############################################################################
export REGION="us-central1"
export ZONE="us-central1-b"

###############################################################################
# 集群规模
# 完整 block: 25 sub-blocks × 18 nodes × 4 GPU = 1800 GPU = 450 VM
# 当前 reservation 12 sub-blocks (216 VM = 864 GPU), 后续可扩至 25
# 网络按满配设计, placement policy / VM 创建按当前实际 sub-block 数
###############################################################################
export TOTAL_GPUS=864
export CLUSTER_PREFIX="gb300-central"
export MACHINE_TYPE="a4x-maxgpu-4g-metal"

###############################################################################
# 拓扑计算
###############################################################################
export GPUS_PER_NODE=4
export NODES_PER_DOMAIN=18
export NUM_DOMAINS=$((TOTAL_GPUS / GPUS_PER_NODE / NODES_PER_DOMAIN))

###############################################################################
# 管理网络（IDPF）
# 1 个 VPC + 2 个子网（官方做法）
###############################################################################
export IDPF_NET="${CLUSTER_PREFIX}-idpf-net"
export IDPF_SUB_0="${IDPF_NET}-sub-0"
export IDPF_SUB_1="${IDPF_NET}-sub-1"

###############################################################################
# RDMA 网络
# vpc-roce-metal profile, 子网自动创建, 8 MRDMA 共用 1 个子网
###############################################################################
export RDMA_NET="${CLUSTER_PREFIX}-rdma-net"
export RDMA_PROFILE="${ZONE}-vpc-roce-metal"
export RDMA_SUBNET="default-subnet-1-${RDMA_NET}"

###############################################################################
# Reservation（从 tencent-gcp-taiji 共享）
# block-0001 下当前 12 个 sub-block，编号 0001..0012
###############################################################################
export RESERVATION_OWNER_PROJECT="tencent-gcp-taiji"
export RESERVATION_NAME="nvidia-gb300-dxkhoz4ypk4mh"
export BLOCK_NUM="0001"
export BLOCK_NAME="${RESERVATION_NAME}-block-${BLOCK_NUM}"
export RESERVATION_PATH="projects/${RESERVATION_OWNER_PROJECT}/reservations/${RESERVATION_NAME}/reservationBlocks/${BLOCK_NAME}/reservationSubBlocks"
export WORKER_PREFIX="${CLUSTER_PREFIX}-b${BLOCK_NUM}"

###############################################################################
# 镜像
###############################################################################
export IMAGE="tlinux-server-4-gb300-v5dot4-ipv6"
export IMAGE_PROJECT="tencent-gcp-taiji-poc"

###############################################################################
# Placement Policy
# 编号 0001..NNNN 与 sub-block 后缀一一对齐
###############################################################################
export PLACEMENT_PREFIX="${CLUSTER_PREFIX}-nvl72-policy"

###############################################################################
# Kubernetes
###############################################################################
export K8S_VERSION="1.34"
export K8S_PATCH="1.34.9"
export POD_CIDR="10.244.0.0/16"
export POD_CIDR_V6="fd10:244::/64"
export SVC_CIDR="10.96.0.0/12"
export SVC_CIDR_V6="fd00:10:96::/112"
export CALICO_VERSION="3.29.3"

###############################################################################
# SSH（g3 master 免密登录 worker）
###############################################################################
export G3_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA223xIp1+ZyQZi24m7s/Wb4g9qWXM7Xb4ZytheIly3V root@gb300-central-master"

###############################################################################
# 辅助
###############################################################################
export G="gcloud --configuration=${GCLOUD_CONFIG}"

log() { echo "=== [$(date +%H:%M:%S)] $* ==="; }
