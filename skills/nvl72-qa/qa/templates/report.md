# ${R_GPU_TYPE} 质检报告 — ${R_POOL_NAMES} (${R_SUB_LABEL}) — ${R_TODAY}

## 结论

${R_VERDICT_TEXT}

| 指标 | 值 |
|---|---|
| 测试范围 | ${R_POOL_NAMES}, ${R_TOTAL_TESTED} 节点 / ${R_TOTAL_GPU} GPU |
| 结果 | **${R_VERDICT}** |
| 故障节点 | ${R_TOTAL_FAIL} |
| 未执行/未完成 | ${R_TOTAL_NOTRUN} |
| NCCL all_reduce avg | ${R_NCCL_AR_AVG} GB/s |
| cuBLAS FP4 avg | ${R_CUBLAS_FP4_AVG} TFLOPS |

## 测试覆盖

| 测试项 | 日志数 | 执行 | 未完成 | 未执行 | PASS | FAIL |
|---|---|---|---|---|---|---|
| hw-check | ${R_HW_NODES} | ${R_HW_RAN} | ${R_HW_INCOMPLETE} | ${R_HW_NOTRUN} | ${R_HW_PASS} | ${R_HW_FAIL} |
| DCGM L${R_DCGM_LEVEL} | ${R_DCGM_NODES} | ${R_DCGM_RAN} | ${R_DCGM_INCOMPLETE} | ${R_DCGM_NOTRUN} | ${R_DCGM_PASS} | ${R_DCGM_FAIL} |
| NCCL 单机 | ${R_NCCL_NODES} | ${R_NCCL_RAN} | ${R_NCCL_INCOMPLETE} | ${R_NCCL_NOTRUN} | ${R_NCCL_PASS} | ${R_NCCL_FAIL} |
| cuBLAS | ${R_CUBLAS_NODES} | ${R_CUBLAS_RAN} | ${R_CUBLAS_INCOMPLETE} | ${R_CUBLAS_NOTRUN} | ${R_CUBLAS_PASS} | ${R_CUBLAS_FAIL} |

${R_NOTRUN_SECTION}

## 行动建议

${R_ACTIONS_SECTION}

## 详细结果

### hw-check

${R_HW_DETAIL}

### DCGM

${R_DCGM_DETAIL}

### NCCL 单机

${R_NCCL_DETAIL}

### cuBLAS

${R_CUBLAS_DETAIL}

### NCCL 单域多节点 (MNNVL on/off)

${R_NCCL_MULTI_DETAIL}

### nvidia-bug-report 关键发现

${R_BUGREPORT_DETAIL}

---

## 集群信息

| 项目 | 值 |
|---|---|
| 集群 | ${R_GKE_CLUSTER} |
| GCP Project | ${R_GCP_PROJECT} |
| Zone | ${R_ZONE} |
| GKE 版本 | ${R_GKE_VERSION} |
| GPU | 4x NVIDIA ${R_GPU_TYPE} / node |
| RDMA NIC | ${R_RDMA_NICS}x CX-8 / node |
| Driver | ${R_DRIVER_VERSION} |
| 测试镜像 | ${R_IMAGE} |
| Profile | ${R_PROFILE} |

*报告由 qa/gen-report.sh + qa/templates/report.md 自动生成 -- ${R_TIMESTAMP}*
