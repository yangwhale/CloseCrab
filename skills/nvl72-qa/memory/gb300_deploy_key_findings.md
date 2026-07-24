---
name: gb300-deploy-key-findings
description: GB300 部署过程中的关键发现和踩坑总结，避免重复试错
metadata: 
  node_type: memory
  type: project
  originSessionId: 84c18ffc-fe2a-4786-a7c5-abc764542166
---

GB300 POC 部署关键发现（2026-07-10 ~ 07-12）。

## 网络

- IDPF 子网必须用 **mgmt dual-stack**（IPv4+IPv6），IPv6-only INTERNAL 子网 ping 不通
- Cloud NAT 配在 IDPF VPC 上，给 worker IPv4 出站（下载包、拉镜像）
- RDMA VPC 用 `vpc-roce-metal` profile（需 GCP 侧开通 feature flag），子网自动创建
- worker SSH 只能走 IPv6（sshd `AddressFamily inet6`）

## kubeadm

- **IPv4 service CIDR 必须放前面**：`--service-cidr=10.96.0.0/12,fd00:10:96::/112`
- API server advertise address 必须和首个 service CIDR 同 IP family → 用 IPv4
- cert extra SANs 加 IPv6：`--apiserver-cert-extra-sans=fd20:8f8:4651:2::`
- worker join 用 IPv4 endpoint：`kubeadm join 10.200.0.2:6443`

## Calico

- readiness probe 必须 patch 成 `-felix-ready`（VXLAN 模式不用 BIRD）
- `IP=autodetect` + `CALICO_NETWORKING_BACKEND=vxlan`
- IPv4 IPPool `vxlanMode=CrossSubnet`

## TLinux 镜像

- 首次创建后必须 reset 一次才能通网络（固件行为）
- metadata server 不可达 → startup script 不执行 → SSH key 必须 baked 在镜像里
- host key 频繁变化（kubelet/sshd 重启），SSH 前需清 known_hosts
- DNS 默认指向 metadata（不通），需手动 nmcli 配 Google IPv6 DNS
- 系统时间可能差几个月，需手动 date -s

**Why:** 这些问题在 GB200 上不存在（GB200 是 IPv4 VM + GVNIC），是 GB300 裸金属 + IPv6 + IDPF 的特有组合。

**How to apply:** 新 session 建 worker 时按 CLAUDE.md 的部署流程走，不要跳步。
