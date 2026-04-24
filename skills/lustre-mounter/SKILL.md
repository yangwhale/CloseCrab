---
name: lustre-mounter
description: This skill should be used when users need to install the Lustre client and mount a Google Cloud Managed Lustre filesystem on GCE VMs (especially TPU VMs). It covers client installation via Artifact Registry, kernel module loading, mounting, and network troubleshooting.
---

# Lustre Mounter — Google Cloud Managed Lustre

## Overview

在 GCE VM（特别是 TPU VM）上安装 Lustre 客户端并挂载 Google Cloud Managed Lustre 文件系统。适用于需要高性能共享存储的训练/推理场景。

## When to Use This Skill

- 用户说"挂载 lustre"、"mount lustre"、"安装 lustre 客户端"
- 需要在 VM 上配置高性能共享文件系统
- Lustre 挂载失败需要排查

## Prerequisites

- VM 必须和 Lustre 实例在**同一 VPC 网络**
- 需要 **sudo 权限**
- Ubuntu 24.04 (noble) 或 22.04 (jammy)

## Installation Workflow

### Step 1: 安装 Lustre 客户端（通过 Artifact Registry）

```bash
# 安装签名密钥
curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/google-cloud.gpg
curl -fsSL https://us-apt.pkg.dev/doc/repo-signing-key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/lustre-client.gpg

# 添加 artifact registry transport
echo 'deb [signed-by=/usr/share/keyrings/google-cloud.gpg] http://packages.cloud.google.com/apt apt-transport-artifact-registry-stable main' | sudo tee /etc/apt/sources.list.d/artifact-registry.list
sudo apt-get update && sudo apt-get install -y apt-transport-artifact-registry

# 添加 Lustre repo（按 OS 版本选择）
# Ubuntu 24.04 (noble):
echo "deb [signed-by=/usr/share/keyrings/lustre-client.gpg] ar+https://us-apt.pkg.dev/projects/lustre-client-binaries lustre-client-ubuntu-noble main" | sudo tee -a /etc/apt/sources.list.d/artifact-registry.list
# Ubuntu 22.04 (jammy):
# echo "deb [signed-by=/usr/share/keyrings/lustre-client.gpg] ar+https://us-apt.pkg.dev/projects/lustre-client-binaries lustre-client-ubuntu-jammy main" | sudo tee -a /etc/apt/sources.list.d/artifact-registry.list
sudo apt-get update

# 安装客户端（注意：包名和 suite 都跟内核版本绑定）
sudo apt install -y lustre-client-modules-$(uname -r)/lustre-client-ubuntu-noble
sudo apt install -y lustre-client-utils/lustre-client-ubuntu-noble
```

**重要:** 如果是 Ubuntu 22.04 (jammy)，将上面的 `noble` 替换为 `jammy`。先用 `lsb_release -cs` 确认 OS 版本。

### Step 2: 加载内核模块

```bash
sudo modprobe lustre
sudo lctl list_nids  # 验证 — 应输出类似 10.x.x.x@tcp 的 NID
```

### Step 3: 挂载文件系统

```bash
sudo mkdir -p /mnt/lustre
sudo mount -t lustre <IP>@tcp:/<FILESYSTEM> /mnt/lustre
```

**示例（已知实例 chrisya-lustre）:**
```bash
sudo mount -t lustre 172.25.0.3@tcp:/lfs /mnt/lustre
```

验证挂载:
```bash
df -h /mnt/lustre
ls /mnt/lustre
```

### Step 4: 查看 Lustre 实例信息

如果不知道 IP 或 filesystem 名，用 gcloud 查询:

```bash
gcloud lustre instances describe <INSTANCE_NAME> \
  --location=<ZONE> \
  --project=<PROJECT> \
  --format="yaml(mountPoint,network,capacityGib)"
```

## Network Troubleshooting

### 确认 VM 和 Lustre 在同一 VPC

```bash
# 查看 Lustre 实例的网络
gcloud lustre instances describe <INSTANCE_NAME> \
  --location=<ZONE> \
  --project=<PROJECT> \
  --format="value(network)"
```

### TCP 988 连通性测试

```bash
timeout 3 bash -c 'echo > /dev/tcp/<LUSTRE_IP>/988'
# 成功无输出，失败报 Connection refused 或超时
```

**注意:** ICMP ping 可能被防火墙拦截，ping 不通**不代表**网络不通。以 TCP 988 测试结果为准。

### 常见网络问题

| 症状 | 原因 | 解决 |
|------|------|------|
| mount 超时 | VM 和 Lustre 不在同一 VPC | 检查 VPC 配置，必须同网络 |
| TCP 988 拒绝 | 防火墙规则缺失 | 添加允许 TCP 988 的 ingress 规则 |
| modprobe 失败 | 内核版本不匹配 | 重新安装对应内核版本的 lustre-client-modules |

## Known Lustre Instances

| 实例名 | Zone | Project | Mount Point | 容量 | VPC |
|--------|------|---------|-------------|------|-----|
| `chrisya-lustre` | us-central1-c | cloud-tpu-multipod-dev | `172.25.0.3@tcp:/lfs` | 36 TiB | default |

## Important Notes

- **内核版本绑定**: Lustre 客户端模块与内核版本强绑定，内核升级后**必须重新安装** `lustre-client-modules-$(uname -r)`
- **不要用 PD/GCS**: 对于大模型权重和数据集，Lustre 的带宽远超 Persistent Disk 和 GCS FUSE，应优先使用 Lustre
- **幂等检查**: 安装前先检查 `dpkg -l | grep lustre` 和 `mount | grep lustre`，避免重复操作
- **官方文档**: https://docs.cloud.google.com/managed-lustre/docs/connect-from-compute-engine

## Verification Checklist

```bash
echo "=== Lustre Client 验证 ==="
echo "1. OS: $(lsb_release -ds) ($(lsb_release -cs))"
echo "2. Kernel: $(uname -r)"
echo "3. Lustre modules: $(dpkg -l | grep lustre-client-modules | awk '{print $2, $3}' || echo 'NOT installed')"
echo "4. Lustre utils: $(dpkg -l | grep lustre-client-utils | awk '{print $2, $3}' || echo 'NOT installed')"
echo "5. Kernel module: $(lsmod | grep lustre | head -1 || echo 'NOT loaded')"
echo "6. NIDs: $(sudo lctl list_nids 2>/dev/null || echo 'lctl not available')"
echo "7. Mounts: $(mount | grep lustre || echo 'No lustre mounts')"
```
