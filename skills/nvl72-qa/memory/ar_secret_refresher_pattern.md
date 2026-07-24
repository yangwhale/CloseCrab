---
name: ar-secret-refresher-pattern
description: GKE/k8s AR 镜像 pull secret 自动刷新标准方案，含凭证类型适配和踩坑
metadata: 
  node_type: memory
  type: reference
  originSessionId: bd18a3c2-5c82-45a6-becf-760d64047093
---

## AR Pull Secret 自动刷新标准方案

**文档**: `docs/ar-secret-refresher-guide.md`

### 核心要点

- CronJob 每 45 分钟刷新 `ar-pull-secret`，用 `google/cloud-sdk:latest`（不是 slim，slim 没 kubectl）
- 凭证通过 `adc-credential` secret 挂载到 `/adc/adc.json`
- **关键**: 用 `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=/adc/adc.json` 环境变量，兼容 `authorized_user` 和 `service_account` 两种凭证类型
- 不要用 `GOOGLE_APPLICATION_CREDENTIALS`（对 `authorized_user` 无效）
- 不要用 `gcloud auth activate-service-account`（只支持 SA key）
- 不要用 `gcloud auth login --cred-file`（只支持 SA key 和 external account）
- GKE 上 nodeSelector 用 `kubernetes.io/arch: amd64`；自建 k8s 用 `node-role.kubernetes.io/control-plane`

### 新集群部署 checklist

1. `kubectl create secret generic adc-credential --from-file=adc.json=<凭证文件>`
2. 从 `docs/ar-secret-refresher-guide.md` 复制 YAML，修改 `--docker-server` 为目标 AR 区域
3. `kubectl apply -f`
4. `kubectl create job --from=cronjob/ar-secret-refresher ar-secret-init`
5. 验证 `kubectl get secret ar-pull-secret`

### 踩坑记录 [[gke-dra-imex-cliqueid]]

2026-07-15 在 GKE gb300-gke-test 上首次部署时：
- 用了 `slim` 镜像 → kubectl not found
- 用了 `GOOGLE_APPLICATION_CREDENTIALS` + `activate-service-account` → `authorized_user` 凭证静默失败（`|| true` 吞掉错误），fallback 到无权限的默认 SA
- 最终改用 `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` 解决
