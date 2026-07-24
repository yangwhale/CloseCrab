---
name: forrest-gke-jumpserver
description: GB300/GB200 GKE 跳板机 forrest-gke-jumpserver — gx alias gj，位置、用法、能通哪些 cluster
metadata: 
  node_type: memory
  type: project
  originSessionId: 4d170451-1dc0-436e-8558-74d4c523fec4
---

# forrest-gke-jumpserver

**用途**: 智子（zhizi bot）跑 GB300/GB200 GKE inference benchmark 的 kubectl 入口。

## 基本信息

| 项 | 值 |
|---|---|
| GCP project | `tencent-gcp-taiji-poc` |
| Zone | `us-central1-b` |
| VM name | `forrest-gke-jumpserver` |
| Machine type | `n2-standard-4`, Rocky Linux 9 optimized, 200GB pd-balanced |
| VPC / subnet | `gb300-gke-mgmt` / `gb300-gke-sub-us-central1` (10.100.0.0/24) |
| Internal IP | `10.100.0.248`（无 external IP，走 Cloud NAT） |
| Tailscale IP | `100.89.216.19`（tag: `gcp-vm`） |
| gx alias | **`gj`**，user=`root` |
| Startup script | `~/code/tencent/gb300/scripts/startup-jumpserver.sh` |
| SA credential in VM | `/root/gb300-sa.json`（`/root/.bashrc` 已 export CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE） |

## 能通哪些 GKE cluster

| Cluster | 从 jumpserver 通不通 | 走 endpoint | 备注 |
|---|---|---|---|
| gb300-gke-test | ✅ 通 | private `172.16.0.2` | subnet `10.100.0.0/24` 已加入该集群 MAN |
| gb200-gke-test | ❌ 不通 | public `35.227.126.207` | VM Cloud NAT 出口 IP 不在 MAN；智子跑 gb200 kubectl 走本机 cloudtop（cloudtop `104.155.223.226/32` 在 gb200 MAN 里） |

**Why:** 用户明确要求"保证 gb300 通就好，其他不用管"。gb200 用本机 cloudtop kubectl 已满足。

## 用法

```bash
# SSH（走 tailscale）
gx gj <cmd>
# 或 ssh root@forrest-gke-jumpserver

# GB300 kubectl（VM 默认 context 已设为 gb300）
gx gj kubectl get nodes             # 直接跑，177 nodes
gx gj kubectl -n <ns> logs ...

# GB200 kubectl（不走 jumpserver，从本机 cloudtop 直接跑）
kubectl --context=gke_tencent-gcp-taiji-poc_us-east1_gb200-gke-test get nodes
```

**How to apply:** 需要在 jumpserver 上做 gb300 相关操作时用 `gx gj ...`；需要 gb200 时从本机 cloudtop 或 zhizi bot 的 shell 直接跑（不要 SSH 到 jumpserver）。

## 关联

- 详细部署记录（3 次 startup script 迭代、tailscale up 根因、MAN 更新等）: `docs/operations.md` 2026-07-19 ~ 2026-07-20 条目
- gx 定义: `~/code/cli/bin/gx`（HOST_MAP / USER_MAP）
- 相关坑: [[gcloud-adc-credential-not-sa-key]], [[tailscale-oauth-authkey-needs-tag]]
