---
name: tailscale-oauth-authkey-needs-tag
description: tskey-client-* 是 OAuth authkey，tailscale up 必须带 --advertise-tags=tag:xxx；startup script 不能吞 stderr
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 4d170451-1dc0-436e-8558-74d4c523fec4
---

# tailscale OAuth authkey (tskey-client-*) 必须带 --advertise-tags

`tskey-client-*` 前缀的 tailscale key 是 **OAuth authkey**（不是普通 pre-auth key），`tailscale up --authkey=$KEY` 必须同时传 `--advertise-tags=tag:xxx` 才能通过。否则报：

```
oauth authkeys require --advertise-tags
```

Forrest tailnet 目前使用的 tag：**`tag:gcp-vm`**（forrest-gke-jumpserver 用的），其他 tag 在 tailnet ACL 里定义。

## Startup script pattern

```bash
tailscale up --ssh --authkey="$AUTHKEY" \
  --hostname="$(hostname)" \
  --advertise-tags=tag:gcp-vm \
  --accept-routes --accept-dns=false
```

**不要用 `|| true` 吞 stderr**，否则 startup script 里 tailscale up 失败会静默，daemon 停在 `WantRunning=false / NeedsLogin`，唯一提示是 tailscaled 日志只走到 `POST /localapi/v0/check-prefs` 就结束 —— 极难定位。

## 诊断 startup script tailscale 失败的正确姿势

- 不要 `exec > /var/log/xxx.log 2>&1` 屏蔽 serial console
- 用 `exec > >(tee -a "$LOG" > /dev/ttyS0) 2>&1` 同时写 log 和 serial
- 加 retry loop + `echo rc=$?`
- 从 `gcloud compute instances get-serial-port-output <vm>` 抓 tailscale up 的具体 stderr

**Why:** forrest-gke-jumpserver 部署时踩过，`|| true` 隐藏 `oauth authkeys require --advertise-tags`，浪费两轮排查。

**How to apply:** 任何新装 tailscale 节点用 forrest tailnet 的 OAuth authkey 时，直接加 `--advertise-tags=tag:gcp-vm`；startup script 输出必须能被 serial console 看到，不吞 stderr。
