---
name: gke-imex-channel-init
description: GKE COS 上 IMEX channel 设备需要手动创建（DaemonSet mknod），否则 MNNVL 不工作
metadata: 
  node_type: memory
  type: project
  originSessionId: 1d46131b-e454-47aa-9615-b08009477d3c
---

## GKE COS IMEX Channel 初始化

GKE COS 节点上 NVIDIA driver 通过容器化 init container 安装，内核模块注册了 `nvidia-caps-imex-channels`（major 240），但**不自动创建 `/dev/nvidia-caps-imex-channels/` 设备节点**。

没有 IMEX channel 设备 → ComputeDomain daemon 无法建立 IMEX session → MNNVL 不工作。

**修复：** `kube-system/imex-channel-init` DaemonSet，在 host `/dev` 上 `mknod` 创建 256 个 channel 设备。

```bash
# 验证
kubectl exec <dra-kubelet-plugin-pod> -n nvidia-dra-driver-gpu -- \
  ls /dev/nvidia-caps-imex-channels/channel0
```

**同时需要 DRA driver pod quota 充足**（2×GPU节点数+1），否则 kubelet-plugin 无法在所有节点上运行。

**Why:** GKE COS 的 containerized driver 安装方式不创建 IMEX 设备节点，和 bare metal TLinux（自建 k8s）不同。这是 MNNVL 在 GKE 上不工作的根因。
**How to apply:** 新建 GKE GB300 集群时，必须部署 imex-channel-init DaemonSet + 设置足够的 pod quota。

相关：[[gke-dra-imex-cliqueid]] [[gb300-cluster-state]]
