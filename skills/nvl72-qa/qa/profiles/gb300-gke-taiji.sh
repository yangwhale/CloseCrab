#!/bin/bash
# GB300 GKE 质检 Profile — tencent-gcp-taiji-poc
# source 后所有 QA_* 变量就位

# === GPU 硬件 ===
export QA_GPU_TYPE=gb300
export QA_GPUS_PER_NODE=4
export QA_GPU_IDS="0 1 2 3"
export QA_NVLINK_TYPE=NV18
export QA_NVLINK_PAIRS=12           # 4 GPU × 3 方向
export QA_NVLINK_TOPO_COLS=5        # awk 列上界 = GPUS + 1
export QA_RDMA_NICS=8
export QA_GPU_MEM_MIN_MB=100000     # ~100 GB
export QA_GPU_TEMP_WARN=85
export QA_ARCH=arm64

# === 容器 ===
export QA_IMAGE="us-docker.pkg.dev/gce-ai-infra/gpudirect-gib/nccl-plugin-gib-diagnostic-arm64:v1.1.2"
export QA_DRIVER_ROOT="/home/kubernetes/bin/nvidia"
export QA_PRIVILEGED=true

# === 集群 ===
export QA_CLUSTER_TYPE=gke
export QA_NAMESPACE=gpu-qa
export QA_NODE_SELECTOR_KEY="cloud.google.com/gke-nodepool"
export QA_KUBE_CONTEXT="gke_tencent-gcp-taiji-poc_us-central1_gb300-gke-test"

# === GCP ===
export QA_GCLOUD_CONFIG=taiji-poc
export QA_PROJECT=tencent-gcp-taiji-poc
export QA_ZONE=us-central1-b
export QA_RESERVATION_LABEL_KEY="cloud.google.com/reservation-subblocks"
export QA_RESERVATION_PREFIX="nvidia-gb300-dxkhoz4ypk4mh-block-0001-subblock"
export QA_POOL_FALLBACK_PREFIX="gb300-pool"

# === 组件版本 (setup-env.sh 使用) ===
export QA_DRA_VERSION=${QA_DRA_VERSION:-0.4.1}
export QA_MPI_VERSION=${QA_MPI_VERSION:-0.8.2}
export QA_JOBSET_VERSION=${QA_JOBSET_VERSION:-0.12.0}
export QA_DCGM_IMAGE=${QA_DCGM_IMAGE:-"nvcr.io/nvidia/cloud-native/dcgm:4.6.0-1-ubuntu24.04"}

# === NCCL ===
export QA_NCCL_ARGS="-b 512M -e 16G -f 2 -g ${QA_GPUS_PER_NODE} -w 20 -n 50"
export QA_NCCL_COLLECTIVES="all_reduce all_gather reduce_scatter alltoall"
export QA_NCCL_MSG_SIZE=17179869184

# === cuBLAS ===
export QA_CUBLAS_BIN="cublasMatmulBench_gb2_3"
export QA_CUBLAS_URL="https://raw.githubusercontent.com/compute-dev/ai_infra_perf_prepare/main/cublas_bench/cublasMatmulBench_gb2_3"
export QA_CUBLAS_TESTS='FP4|-P=nvoohso -m=9728 -n=16384 -k=8192 -ta=1 -tb=0 -A=1 -B=0 -T=1000 -W=10000 -p=t -sf_p=u\nFP8|-P=qqssq -m=9728 -n=2048 -k=32768 -ta=1 -tb=0 -A=1 -B=0 -T=1000 -W=10000 -p=t\nFP16|-P=hsh -m=8192 -n=9728 -k=16384 -ta=0 -tb=1 -A=1 -B=0 -T=1000 -W=10000 -p=t\nBF16|-P=tst -m=8192 -n=9728 -k=16384 -ta=0 -tb=1 -A=1 -B=0 -T=1000 -W=10000 -p=t\nTF32|-P=sss_fast_tf32 -m=8192 -n=9728 -k=16384 -ta=0 -tb=1 -A=1 -B=0 -T=1000 -W=10000 -p=t\nFP32|-P=sss -m=8192 -n=9728 -k=16384 -ta=0 -tb=1 -A=1 -B=0 -T=1000 -W=1000 -p=t'

# === 多节点 NCCL ===
export QA_NCCL_MULTI_ARGS="-b 1M -e 16G -f 2 -g 1 -w 50 -n 100"
export QA_NCCL_GIB_ENVS="\
-x NCCL_NET_PLUGIN=/usr/local/gib/lib64/libnccl-net.so \
-x NCCL_ENV_PLUGIN=gcp \
-x NCCL_IB_GID_INDEX=3 \
-x NCCL_IB_QPS_PER_CONNECTION=4 \
-x NCCL_IB_TC=52 \
-x NCCL_IB_FIFO_TC=84 \
-x NCCL_IB_ADAPTIVE_ROUTING=1 \
-x NCCL_PXN_C2C=1 \
-x NCCL_IB_MERGE_NICS=0 \
-x NCCL_SOCKET_IFNAME=eth0 \
-x UCX_NET_DEVICES=gpu0rdma0,gpu0rdma1,gpu1rdma0,gpu1rdma1,gpu2rdma0,gpu2rdma1,gpu3rdma0,gpu3rdma1 \
-x NCCL_DEBUG=WARN"
export QA_NCCL_MULTI_CPU="96"
export QA_NCCL_MULTI_MEM="800Gi"
export QA_NCCL_MULTI_SHM="250Gi"

# === DCGM ===
export QA_DCGM_LEVEL=2               # 2: PCIe+显存+HBM带宽, 3: +GEMM+stress+power
export QA_TIMEOUT_DCGM=900

# === 并行限流 ===
export QA_PREFLIGHT_STAGGER_S=2     # 并行时按 subblock 号 × 此值错开启动（秒）
export QA_GCLOUD_BATCH_SIZE=5       # gcloud 调用每批数量
export QA_GCLOUD_BATCH_DELAY=1      # 批间等待（秒）
export QA_POLL_INTERVAL=30           # wait_completion 轮询间隔（秒）
export QA_LOG_BATCH_SIZE=6           # 日志收集并发数（防 API 连接耗尽）
export QA_LOG_MAX_RETRY=10           # 日志收集最大重试次数
export QA_LOG_SOURCE=${QA_LOG_SOURCE:-cloud-logging}   # cloud-logging | kubectl
export QA_GKE_CLUSTER=${QA_GKE_CLUSTER:-gb300-gke-test}
export QA_CLOUD_LOG_FLUSH_DELAY=${QA_CLOUD_LOG_FLUSH_DELAY:-60}  # cloud-logging 模式 cleanup 前等待 flush（秒）

# === 超时 (秒) ===
export QA_TIMEOUT_HW=180
export QA_TIMEOUT_NCCL=300
export QA_TIMEOUT_CUBLAS=1800
export QA_TIMEOUT_NCCL_MULTI=1800

# === 离群阈值 (%) ===
export QA_OUTLIER_NCCL_PCT=5
export QA_OUTLIER_CUBLAS_PCT=3

# === 绝对下限 (299N/267N fleet 统计，p5 打八折) ===
export QA_NCCL_MIN_BUSBW=650       # all_reduce; all_gather/reduce_scatter 630, alltoall 640
export QA_CUBLAS_MIN_FP4=7500
export QA_CUBLAS_MIN_FP8=3300
export QA_CUBLAS_MIN_FP16=1600
export QA_CUBLAS_MIN_BF16=1700
export QA_CUBLAS_MIN_TF32=800
export QA_CUBLAS_MIN_FP32=70

# === 资源请求 ===
export QA_HW_CPU="2"
export QA_HW_MEM="4Gi"
export QA_NCCL_CPU="48"
export QA_NCCL_MEM="200Gi"
export QA_NCCL_SHM="64Gi"
export QA_CUBLAS_CPU="4"
export QA_CUBLAS_MEM="16Gi"
