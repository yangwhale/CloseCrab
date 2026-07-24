# GB300 项目

GB300 集群相关的交付、测试、评估工作。属于 **张量企鹅（tensor-penguin）** work_dir 的子项目 —— 总规范、身份和 handoff protocol 见 [`../CLAUDE.md`](../CLAUDE.md)。

## 当前集群状态（2026-07-12）

| 节点 | 机型 | Sub-block | IPv4 | IPv6 ULA | GPU | k8s 状态 |
|---|---|---|---|---|---|---|
| `gb300-central-master` | n2-standard-4 | — | 10.200.0.2 | fd20:8f8:4651:2:: | — | control-plane, Ready |
| `gb300-central-b0001-d0008-w1` | a4x-maxgpu-4g-metal | 0008 | 10.200.0.10 | fd20:8f8:4651:2:0:4:: | 4× GB300 | worker, Ready |
| `gb300-central-b0001-d0009-w1` | a4x-maxgpu-4g-metal | 0009 | 10.200.0.16 | fd20:8f8:4651:2:0:6:: | 4× GB300 | worker, Ready |

k8s 组件: kubeadm 1.34.9 / Calico 3.29.3 (VXLAN) / DRA GPU Driver 0.4.1 / DRANET 1.3.0 / GFD 0.19.2 / MPI Operator 0.8.0

## GCP 操作

**GB300 POC project `tencent-gcp-taiji-poc` (594480068242)**

- k8s/kubectl 操作（自建集群）：`gx g3 "<command>"`（别名 `g3` = `gb300-central-master`，user=root，via tailscale）
- k8s/kubectl 操作（GKE `gb300-gke-test`）：本机直连 public endpoint，需要 3 步认证（见下）
- 本机 gcloud 操作：`gcloud --configuration=taiji-poc <command>`（ADC credential_file_override `keys/gb300-sa.json`）
- SSH 到 worker：`gx g3 "ssh root@<worker-IPv4或IPv6>"`（v5.4 镜像 sshd `AddressFamily any`，IPv4/IPv6 均可）
- 如涉及 `gpu-launchpad-playground`：走 `gx k8n`

### ⚠️ GKE cluster / pool 操作先看 `scripts/gke-*`

`scripts/` 里已有固化脚本：`gke-create-cluster.sh` / `gke-create-nodepool.sh` / `gke-post-install.sh` / `gke-env.sh` / `gke-run-checks.sh` / `gke-analyze-logs.sh`。**任何 GKE 侧变更（create / update pool、cluster、post-install）先 `ls scripts/gke-*` 找现成脚本**，不要看 `gcloud describe ... --format=yaml` 就自己手写 gcloud 命令。原因：现有 pool 的 yaml 里有很多字段是历史遗留的显式设置（e.g. `linuxNodeConfig.hugepages.hugepageSize2m: 4096`），GKE default 是 0；手写命令时容易漏，看不出 → 后续 asapd-lite / CUDA runtime / DRA 全线连锁失败（2026-07-24 pool-0013~0017 case）。

执行流程：先 read 脚本头部找到需要的 env vars → 补 profile / env → 跑脚本；如果脚本缺当前场景（e.g. 新 GKE flag），改脚本而不是绕开。

**Verify 立即做**：pool create/update 后立刻 `kubectl get node -o jsonpath='{.status.capacity}'` 对齐关键字段（hugepages-2Mi / nvidia.com/gpu / ephemeral-storage），跟老 pool 完全一致才算成功。

### GKE kubectl 认证 (gb300-gke-test)

认证配置持久化在 kubeconfig 中，正常情况下直接 `kubectl` 即可，**无需每次重做**。仅在 kubectl 报 Forbidden / 连接失败时才执行以下修复：

```bash
# 1. 获取 kubeconfig（会覆盖已有配置）
gcloud --configuration=taiji-poc container clusters get-credentials gb300-gke-test \
  --location=us-central1 --project=tencent-gcp-taiji-poc
# 2. 改 public endpoint（get-credentials 默认指向 private endpoint，不可达）
kubectl config set-cluster gke_tencent-gcp-taiji-poc_us-central1_gb300-gke-test \
  --server=https://35.253.228.114
# 3. 注入 ADC（get-credentials 默认用 VM SA，无权限）
kubectl config set-credentials gke_tencent-gcp-taiji-poc_us-central1_gb300-gke-test \
  --exec-env=CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=$(pwd)/keys/gb300-sa.json
# 4. 清 cache
rm -f ~/.kube/gke_gcloud_auth_plugin_cache ~/.config/gcloud/gke_gcloud_auth_plugin_cache
```

注意：`get-credentials` 会重置 kubeconfig，导致步骤 2-4 的配置丢失，所以要连续执行 1-4。不执行 `get-credentials` 就不需要重做。

## 网络架构

```
本机 (cloudtop)
  │ tailscale
  ▼
gb300-central-master (g3) ── dual-stack mgmt 子网 (IPv4 + IPv6 ULA)
  │                           10.200.0.0/24 + fd20:8f8:4651:2::/64
  │ IPv4 (k8s API 10.200.0.2:6443)
  │ IPv6 ULA (SSH to workers)
  ▼
GB300 workers ── dual-stack mgmt 子网 (同上)
  │ IPv4: k8s join、containerd 拉镜像 (Cloud NAT 出站)、Calico VXLAN
  │ IPv6: g3 SSH 入口
  │
  └── RDMA NIC (8× CX-8 PF) ── vpc-roce-metal 子网 (IPv6, 自动创建)
```

| VPC | 子网 | 类型 | 用途 |
|---|---|---|---|
| `gb300-central-idpf-net` | `mgmt` | dual-stack (IPv4+IPv6) | **master + worker 生产用** |
| | `sub-0` / `sub-1` | IPv6 INTERNAL | 弃用（TLinux ping 不通） |
| | `sub-ext-0` / `sub-ext-1` | IPv6 EXTERNAL | 保留但不默认用 |
| `gb300-central-rdma-net` | `default-subnet-1-*` | IPv6 (自动创建) | 8× MRDMA 共用 |

Cloud Router `gb300-router` + Cloud NAT `gb300-nat`：给 mgmt 子网的 IPv4 出站做 NAT（worker 通过它下载 containerd/kubelet 等包）。

## 部署流程（标准做法）

### 1. 创建 worker VM

```bash
# env.sh 已配好 INTERNAL 子网变量，但实际用 mgmt 子网
source scripts/env.sh
MGMT_SUB="${IDPF_NET}-mgmt"

gcloud --configuration=taiji-poc compute instances create ${WORKER_PREFIX}-d${DOMAIN}-w${N} \
  --machine-type=a4x-maxgpu-4g-metal \
  --zone=us-central1-b --project=tencent-gcp-taiji-poc \
  --image=tlinux-server-4-gb300-v5dot3-ipv6 --image-project=tencent-gcp-taiji-poc \
  --boot-disk-type=hyperdisk-balanced --boot-disk-size=1000GB \
  --scopes=cloud-platform \
  --network-interface=nic-type=IDPF,network=${IDPF_NET},subnet=${MGMT_SUB},stack-type=IPV4_IPV6 \
  --network-interface=nic-type=IDPF,network=${IDPF_NET},subnet=${IDPF_SUB_1},stack-type=IPV6_ONLY,no-address \
  --network-interface=nic-type=MRDMA,subnet=${RDMA_SUBNET},stack-type=IPV6_ONLY \
  ... (×8 MRDMA) \
  --reservation-affinity=specific \
  --reservation=${RESERVATION_PATH}/${BLOCK_NAME}-subblock-${DOMAIN} \
  --resource-policies=${PLACEMENT_PREFIX}-${DOMAIN} \
  --metadata=ssh-keys="root:${G3_PUBKEY}"
```

### 2. 首次 reset（已知行为）

TLinux v9 镜像在 GB300 裸金属上**首次创建后必须 reset 一次**才能通网络。这是固件/内核行为，不是镜像 bug。

```bash
gcloud --configuration=taiji-poc compute instances reset <VM_NAME> --zone=us-central1-b
```

等 ~7 分钟后 SSH 应该通。

### 3. SSH 到 worker

```bash
gx g3 "ssh -o StrictHostKeyChecking=no root@<worker-IPv6>"
```

**注意**：worker sshd 配了 `AddressFamily inet6`，IPv4 SSH 会被拒（`Connection refused`）。必须用 IPv6 地址连。

**注意**：worker 的 SSH host key 在每次 reset / kubelet 重启后都会变，`known_hosts` 需要清理。

### 4. 配置 worker

```bash
# 从 g3 上远程执行
gx g3 "ssh root@<worker-IPv6> '
  # DNS (NM 永久配置)
  nmcli connection modify gcp-default ipv6.dns 2001:4860:4860::8888,2001:4860:4860::8844 ipv4.dns 8.8.8.8,8.8.4.4 ipv6.ignore-auto-dns yes ipv4.ignore-auto-dns yes

  # 时间 (裸金属时间可能差几个月)
  date -s \"$(date -u +%Y-%m-%dT%H:%M:%S)\"

  # 禁用无效 repo
  for repo in cuda-rhel9-sbsa google-compute-engine; do
    sed -i \"s/enabled=1/enabled=0/\" /etc/yum.repos.d/${repo}*.repo 2>/dev/null
  done

  # containerd + nvidia-ctk + kubelet
  dnf install -y --enablerepo=AppStream container-selinux containerd.io
  containerd config default > /etc/containerd/config.toml
  sed -i \"s/SystemdCgroup = false/SystemdCgroup = true/\" /etc/containerd/config.toml
  systemctl enable --now containerd
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo | tee /etc/yum.repos.d/nvidia-container-toolkit.repo
  dnf install -y nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=containerd --set-as-default
  systemctl restart containerd
  dnf install -y --enablerepo=AppStream kubelet-1.34.9 kubeadm-1.34.9 conntrack-tools socat iptables ethtool
  systemctl enable kubelet

  # sysctl + imex
  swapoff -a; modprobe overlay br_netfilter
  systemctl disable --now nvidia-imex.service; systemctl mask nvidia-imex.service
'"
```

脚本版：`scripts/setup-worker.sh`（需要适配 mgmt 子网的 NM connection 名）

### 5. Join k8s

```bash
# 在 g3 上
JOIN_CMD=$(kubeadm token create --print-join-command)
# 提取 TOKEN 和 HASH
gx g3 "ssh root@<worker-IPv6> 'kubeadm join 10.200.0.2:6443 --token <TOKEN> --discovery-token-ca-cert-hash sha256:<HASH> --node-name <VM_NAME> --ignore-preflight-errors=all'"

# GPU label
kubectl label node <VM_NAME> feature.node.kubernetes.io/pci-10de.present=true nvidia.com/gpu.present=true --overwrite
```

脚本版：`scripts/join-worker.sh <worker-IPv6> <node-name>`

### 6. k8s master 一键配置

```bash
gx g3 "bash /root/scripts/setup-master-k8s.sh"
```

包含：kubeadm init (dual-stack, IPv4 primary) + Calico + DRA + DRANET + GFD + MPI Operator

## 关键配置和踩坑

### kubeadm init

- **必须 IPv4 primary**：`--service-cidr=10.96.0.0/12,fd00:10:96::/112`，`--apiserver-advertise-address=10.200.0.2`
- IPv6 放前面会导致 pod 内部连不上 IPv6 service ClusterIP（pod 只有 IPv4 网络）
- cert extra SANs 包含 IPv6：`--apiserver-cert-extra-sans=fd20:8f8:4651:2::`

### Calico

- **readiness probe 必须 patch**：标准 manifest 检查 BIRD，VXLAN 模式不用 BIRD → probe 永远失败
  ```bash
  kubectl patch daemonset calico-node -n kube-system --type=json \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/exec/command","value":["/bin/calico-node","-felix-ready"]}]'
  ```
- `IP=autodetect`（worker 有 IPv4）+ `CALICO_NETWORKING_BACKEND=vxlan` + `FELIX_IPV6SUPPORT=true`
- IPv4 IPPool `vxlanMode=CrossSubnet`

### TLinux v9 镜像 (`tlinux-server-4-gb300-v5dot3-ipv6`)

基于 `tlinux-server-4-gb200-v5dot3` offline patch（`tlinux-patch-disk-v2`），改动：
- NM：删 `enp0s3-gvnic.nmconnection`，建 `gcp-default.nmconnection`（IPv4/IPv6 dual auto + may-fail）
- sshd：独立 `sshd-ipv6.service`（Type=simple，无 google-guest-agent 依赖）+ `Port 22` + `AddressFamily inet6` + `PermitRootLogin prohibit-password`
- 删 `sshd.service.wants`（去掉 google-guest-agent 依赖）
- mask `google-oslogin-cache`
- baked SSH key（g3 master 公钥）

#### v5.4 变更（相对 v5.3）

- sshd_config: `AddressFamily inet6` → `AddressFamily any`（双栈 SSH）
- 删除 `sshd-ipv6.service`（unit 文件 + enable 软链接）
- 启用 `sshd.service`（标准 systemd 单元）+ `sshd.service.d/10-network-online.conf` drop-in
- 结果：sshd 同时监听 `0.0.0.0:22` 和 `[::]:22`，IPv4/IPv6 SSH 均可
- **Startup script 已验证可用**：google-startup-scripts.service 在 boot 时自动从 metadata 下载并执行

**已知问题**：
- 首次创建需 reset（固件行为），有时需要多次 reset（IDPF NIC 初始化不确定）
- serial console kexec 后无内核输出
- guest-agent 尝试管理 RDMA NIC 会报错（非致命，cosmetic）

### 镜像信息

| 镜像 | Project | 用途 |
|---|---|---|
| `tlinux-server-4-gb300-v5dot4-ipv6` | `tencent-gcp-taiji-poc` | **GB300 worker 生产用**（推荐） |
| `tlinux-server-4-gb300-v5dot3-ipv6` | `tencent-gcp-taiji-poc` | 旧版（sshd-ipv6 冲突，已废弃） |
| `tlinux-server-4-gb200-v5dot3` | `tencent-gcp-taiji-poc` | 原始 TLinux（GB200 用，GB300 需 patch） |
| `rocky-linux-10-optimized-gcp-nvidia-580-arm64-v20260615` | `rocky-linux-accelerator-cloud` | Rocky 10 + NVIDIA 580（可对比参考） |

### Reservation

| 字段 | 值 |
|---|---|
| Owner project | `tencent-gcp-taiji` |
| Name | `nvidia-gb300-dxkhoz4ypk4mh` |
| Block | `block-0001` (1 block, 216 VM) |
| Sub-blocks | 12 × 18 VM = 864 GPU |
| Zone | `us-central1-b` |

Placement policy: `gb300-central-nvl72-policy-0001` ~ `0012`（4 位对齐 sub-block）

## 脚本

| 脚本 | 在哪跑 | 用途 |
|---|---|---|
| `scripts/env.sh` | source | 共享环境变量 |
| `scripts/setup-network.sh` | 本机 | VPC + 子网 + 防火墙 + placement policy |
| `scripts/create-master.sh` | 本机 | 创建 master VM |
| `scripts/startup-master.sh` | master VM | master startup: tailscale + containerd + kubeadm |
| `scripts/setup-master-k8s.sh` | g3 | **一键 kubeadm init + 全部 k8s 组件** |
| `scripts/create-vms.sh` | 本机 | 批量创建 worker VM（当前 INTERNAL 子网，需手动改 mgmt） |
| `scripts/setup-worker.sh` | worker VM | DNS + 时间 + containerd + kubelet |
| `scripts/join-worker.sh` | g3 | worker join k8s + GPU label |

**注意**：`create-vms.sh` 和 `env.sh` 的 `IDPF_SUB_0/1` 指向 INTERNAL 子网（`sub-0/sub-1`），但实际生产用 `mgmt` 子网。手动创建 worker 时需指定 `subnet=${IDPF_NET}-mgmt,stack-type=IPV4_IPV6`。

## 基础参考文档（3 篇）

GB300 部署的全部决策和验证基于以下 3 篇文档，新 session 需要了解背景时优先读这些：

| 文档 | 路径 | 内容 |
|---|---|---|
| GB200 部署指南 | `docs/gb200_deploy_guide.md` | 完整的 GB200 A4X 自建 k8s 部署方案（网络/VM/k8s/DRA/NCCL），GB300 的基础模板 |
| GB300 差异分析 | `docs/gb300_diff.md` | GB300 vs GB200 的所有差异点（IDPF/CX-8/IPv6/裸金属/8 MRDMA/asapd-lite），含官方文档链接 |
| TLinux 镜像构建指南 | `docs/tlinux_image_guide_v2.md` | 镜像预装组件清单、构建流程、v5.3 changelog，原始 URL: https://gcp.totorochina.com/tlinux_image_guide-v2.html |

## 文件布局

```
gb300/
├── CLAUDE.md              # 本文件
├── .claude/settings.json  # hook: 每次 Bash 后提醒记操作日志
├── keys/                  # ADC credential（.gitignore）
├── docs/
│   ├── gb200_deploy_guide.md   # GB200 部署指南（参考）
│   ├── gb300_diff.md           # GB300 vs GB200 差异分析
│   └── operations.md           # 操作记录（交付文档依据）
├── logs/
│   └── iperf3-*                # 带宽测试日志
└── scripts/
    ├── env.sh                  # 共享变量
    ├── setup-network.sh        # 网络基础设施
    ├── create-master.sh        # master VM
    ├── startup-master.sh       # master startup
    ├── setup-master-k8s.sh     # k8s 一键配置
    ├── create-vms.sh           # worker VM 批量创建
    ├── setup-worker.sh         # worker 配置
    └── join-worker.sh          # worker join k8s
```

## 操作记录规则

**每次执行 GCP 基础设施操作后，必须立即追加到 `docs/operations.md`。**

覆盖范围：gcloud create/delete/update、VM 创建/删除、网络变更、权限变更、quota 调整、reservation 操作、镜像操作等一切改变 GCP 资源状态的命令。

格式参照已有条目：日期、命令、结果、踩坑。不攒批，每次操作完成后立即记录，和回复用户同步进行。

这个文件是最终交付文档的依据，漏记 = 交付文档不完整。

## 与 GB200 的关系

GB300 是 GB200 的下一代平台（NVLink domain 从 72 GPU 扩展到 144 GPU，单机架密度更高）。

- **可直接复用**：k8s DRA 部署流程、NCCL 调优参数、check-k8s-dra-health.sh 诊断工具、Megatron/DeepEP 测试方法论、ComputeDomain CRD 用法
- **需要重新验证**：driver 版本兼容性、NVLink/NVSwitch 拓扑差异、NCCL baseline 数值、GIB 版本、散热 / 功耗 envelope
- **GB200 记忆索引**：`~/.claude/projects/-home-admin-maxwellx-altostrat-com-code-tencent-gb200/memory/MEMORY.md`

## 关联项目

- 同级 [`../gb200/`](../gb200/) —— GB200 集群专项
- 同级 [`../h100/`](../h100/) —— H100 集群部署
