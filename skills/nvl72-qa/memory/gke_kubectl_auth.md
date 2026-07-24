---
name: gke-kubectl-auth
description: GKE kubectl 认证流程：gb300-gke-test 集群需要手动设 public endpoint + ADC credential + 清 cache
metadata: 
  node_type: memory
  type: reference
  originSessionId: 1d46131b-e454-47aa-9615-b08009477d3c
---

## GKE kubectl 认证 (gb300-gke-test)

`gcloud container clusters get-credentials` 默认指向 private endpoint `172.16.0.2`（本机不可达），且 `gke-gcloud-auth-plugin` 默认用 VM SA（无 k8s RBAC 权限）。需要 3 步修复：

```bash
# 1. 获取 kubeconfig（会指向 private endpoint）
gcloud --configuration=taiji-poc container clusters get-credentials gb300-gke-test \
  --location=us-central1 --project=tencent-gcp-taiji-poc

# 2. 改为 public endpoint
kubectl config set-cluster gke_tencent-gcp-taiji-poc_us-central1_gb300-gke-test \
  --server=https://35.253.228.114

# 3. 注入 ADC credential（maxwellx@google.com，有 container.admin 权限）
kubectl config set-credentials gke_tencent-gcp-taiji-poc_us-central1_gb300-gke-test \
  --exec-env=CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=/home/admin_maxwellx_altostrat_com/code/tencent/gb300/keys/gb300-sa.json

# 4. 清 gke auth plugin cache（否则可能用旧 token）
rm -f ~/.kube/gke_gcloud_auth_plugin_cache ~/.config/gcloud/gke_gcloud_auth_plugin_cache
```

**Why:**
- `taiji-poc` gcloud config 的 `account` 是 VM SA `cloudtop@forrest-test-project-333203.iam.gserviceaccount.com`，无 GKE RBAC
- `credential_file_override` 是 `authorized_user` 类型（maxwellx@google.com），gcloud API 调用走它，但 `gke-gcloud-auth-plugin` 不自动继承
- 集群是 private cluster，`get-credentials` 总是指向 private endpoint
- 每次 `get-credentials` 会重置 kubeconfig，exec env 丢失，需要重新注入

**How to apply:** 每次 kubectl 不通时按上述 4 步操作。特别是在 `get-credentials` 之后必须重做 2-4 步。
