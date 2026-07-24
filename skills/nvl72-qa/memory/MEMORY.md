# Memory Index

- [ops-log-discipline](ops_log_discipline.md) — 每次 GCP 操作后必须追加记录到 docs/operations.md，不攒批
- [gb300-deploy-key-findings](gb300_deploy_key_findings.md) — GB300 部署关键发现：mgmt 双栈子网、kubeadm IPv4 优先、Calico readiness patch、TLinux 首次 reset、metadata 不可达
- [logs-directory-convention](logs_directory_convention.md) — 日志文件放 logs/ 目录，不放 docs/

- [gb300-cluster-state](gb300_cluster_state.md) — 集群当前状态：节点分布、k8s 组件、已知问题、cordon 节点
- [gb300-nccl-benchmark-status](gb300_nccl_benchmark_status.md) — ~~旧~~ NCCL/cuBLAS 测试进展（质检已迁移到 qa/，见 qa-toolkit-design）
- [gke-kubectl-auth](gke_kubectl_auth.md) — GKE kubectl 认证：需手动设 public endpoint + ADC credential + 清 cache
- [gke-fault-node-physicalhost](gke_fault_node_physicalhost.md) — 故障节点必须记录 physicalHost（/block/subblock/host），重建 pool 后追踪同一物理机

- [gke-dra-imex-cliqueid](gke_dra_imex_cliqueid.md) — GKE DRA v0.4.1 升级踩坑：CRD 缺失、IMEX channels 未初始化、channel template domainID、bash GROUPS 变量
- [ar-secret-refresher-pattern](ar_secret_refresher_pattern.md) — AR pull secret 自动刷新标准方案（CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE），新集群部署 checklist
- [gke-dsv3-training-lessons](gke_dsv3_training_lessons.md) — GKE GB300 DSv3 256GPU 训练全流程踩坑：DRA/CD/NCCL/graph模式/launcher/性能数据
- [qa-toolkit-design](qa_toolkit_design.md) — **质检 skill /gpu-qa** — 提到"质检"触发此 skill，覆盖 setup→测试→日志→分析→报告→cordon 全流程，profile 绑定 kubectl context
- [feedback-verify-before-delete](feedback_verify_before_delete.md) — 删除 ns/pod/DS 前必须先确认日志已完整收集

- [gke-imex-channel-init](gke_imex_channel_init.md) — GKE COS 上 IMEX channel 设备需手动 mknod 创建，否则 MNNVL 不工作
- [reservation-health-query](reservation_health_query.md) — Reservation degrade / GCE 创建失败 / 质检故障的查询方法和数据来源

- [forrest-gke-jumpserver](forrest_gke_jumpserver.md) — GB300 GKE 跳板机（gx alias `gj`），gb300 通 gb200 不通、SA path、用法
- [gcloud-adc-credential-not-sa-key](gcloud_adc_credential_not_sa_key.md) — keys/gb300-sa.json 是 ADC authorized-user 不是 SA key，用 CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE env
- [tailscale-oauth-authkey-needs-tag](tailscale_oauth_authkey_needs_tag.md) — tskey-client-* OAuth authkey 必须 --advertise-tags=tag:gcp-vm；startup script 不吞 stderr

（GB200 经验参考：`~/.claude/projects/-home-admin-maxwellx-altostrat-com-code-tencent-gb200/memory/MEMORY.md`）

- [megatron-iter-log-at-rank-last](megatron_iter_log_at_rank_last.md) — Megatron print_rank_last 默认：多机 iter timing log 在 pod-<N-1>，不是 pod-0
- [log-pull-before-delete-gate](log_pull_before_delete_gate.md) — benchmark cleanup 前必须做 log 拉齐 gate（每 pod + rank-last iter 完整），失败 abort delete
- [feedback-no-unverified-attribution](feedback_no_unverified_attribution.md) — 报告禁止未验证性能归因（vboost / 锁频等），差距只列事实数值
