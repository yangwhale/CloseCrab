---
name: gcloud-adc-credential-not-sa-key
description: keys/gb300-sa.json 其实是 authorized-user ADC credential 不是 service account key；用 CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE 别用 activate-service-account
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 4d170451-1dc0-436e-8558-74d4c523fec4
---

# gb300-sa.json 是 authorized-user credential 不是 SA key

尽管文件名叫 `gb300-sa.json`，`~/code/tencent/gb300/keys/gb300-sa.json` 里的实际内容是 **authorized-user ADC credential**（用户 refresh token 格式），不是 service account JSON。特征：

```json
{
  "account": "",
  "client_id": "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com",
  "client_secret": "d-FL95Q19q7MQmFpd7hHD0Ty",
  "quota_project_id": "tencent-gcp-taiji-poc",
  "refresh_token": "...",
  "type": "authorized_user"
}
```

**用法**:

- ✅ **对** — 用 env: `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=/path/to/gb300-sa.json`
  - gcloud CLI 会用它
  - kubectl exec-plugin 通过 kubeconfig 里的 `--exec-env=CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=...` 也会用它
- ❌ **错** — `gcloud auth activate-service-account --key-file=/path/to/gb300-sa.json`
  报错：`The .json key file is not in a valid format.` — 因为 activate-service-account 只接受 `type: service_account` 的 JSON key（有 `private_key` 字段）

**Why:** 部署 forrest-gke-jumpserver 时踩过一次，浪费一轮排查。

**How to apply:** 在任何新机器上配 gb300/GKE kubectl 时，直接 `export CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=$KEY`，不要走 activate-service-account 路径。同一份 credential 复制到别处时同理（比如 ai_infra 里的备份）。
