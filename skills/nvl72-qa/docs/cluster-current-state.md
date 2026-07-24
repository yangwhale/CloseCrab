# Cluster / Pool 当前状态快照

**抓取时间**：2026-07-24（chris 接手前一刻）

## Cluster
```
gb300-gke-test	1.36.0-gke.4681000	270	RAPID	RUNNING	35.253.228.114	35.253.228.114
```

## Node Pools（全部）
```
NAME             MACHINE_TYPE         INITIAL_NODE_COUNT  NODE_VERSION        STATUS   AUTO_UPGRADE  AUTO_REPAIR
default-pool     e2-standard-4        3                   1.36.0-gke.4681000  RUNNING  True          True
gb300-pool-0001  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
gb300-pool-0002  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
gb300-pool-0003  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
gb300-pool-0004  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
gb300-pool-0005  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
gb300-pool-0006  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
gb300-pool-0009  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
gb300-pool-0010  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
gb300-pool-0012  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
gb300-pool-0007  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
gb300-pool-0013  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
gb300-pool-0014  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  ERROR    True
gb300-pool-0015  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
gb300-pool-0016  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
gb300-pool-0017  a4x-maxgpu-4g-metal  18                  1.36.0-gke.4681000  RUNNING  True
```

## Pool-0013~0017 的实际节点 kubelet version

**关键：pool NODE_VERSION 显示 nominal (RAPID auto-upgrade target)，实际看 node kubeletVersion**

### gb300-pool-0013
```
NAME                                               STATUS   KUBELET               KERNEL     SCHED
gke-gb300-gke-test-gb300-pool-0013-52c35462-0199   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-03st   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-6c93   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-6w3d   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-85h8   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-9pkk   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-bnv9   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-dk25   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-jpzz   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-ljbz   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-m8pc   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-qp6m   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-rsld   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-sb4n   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-vs8c   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-vvv4   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-wzf1   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0013-52c35462-xh3h   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
```

### gb300-pool-0014
```
NAME                                               STATUS   KUBELET               KERNEL     SCHED
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-1vw7   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-2pp1   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-g934   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-kdm2   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-ms9p   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-qpkn   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-r2vc   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-rc7m   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-rdrq   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-tpt5   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-vfl6   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-vtl9   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-w0m5   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-wg1d   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0014-28e4a89f-z5xh   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
```

### gb300-pool-0015
```
NAME                                               STATUS   KUBELET               KERNEL     SCHED
gke-gb300-gke-test-gb300-pool-0015-261298ee-1g7b   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-1txr   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-3j80   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-4h66   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-4js1   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-6bq5   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-8k8d   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-cw89   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-fkcp   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-gpmp   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-hgk6   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-m19f   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-m76n   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-nb8g   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-pzh1   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-q1jc   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-t2mx   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0015-261298ee-vrts   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
```

### gb300-pool-0016
```
NAME                                               STATUS   KUBELET               KERNEL     SCHED
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-0s5x   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-4zq3   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-5wf1   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-cbx7   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-chcz   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-f0lx   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-fp4v   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-gfxm   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-hmxn   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-m08n   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-mbtq   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-qnq3   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-smsk   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-vs19   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-wfnc   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-xhj4   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-xprx   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0016-7c7ba395-zbjj   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
```

### gb300-pool-0017
```
NAME                                               STATUS   KUBELET               KERNEL     SCHED
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-0rb8   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-15wv   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-4f21   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-88fk   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-8s84   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-bknr   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-gcjk   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-k6cv   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-kfz5   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-kn0h   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-ks42   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-lr7v   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-n5n0   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-nqgr   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-q3n7   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-s30l   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-xn1v   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
gke-gb300-gke-test-gb300-pool-0017-7401d1ca-z4dq   Ready    v1.36.0-gke.4681000   6.12.90+   <none>
```


## 参考：pool-0002 / 0003 / 0005 / 0007（老 image 工作 pool）节点 kubelet

### gb300-pool-0002（前 3 台样例）
```
NAME                                               KUBELET
gke-gb300-gke-test-gb300-pool-0002-c2cb19f4-1zt9   v1.36.0-gke.4447000
gke-gb300-gke-test-gb300-pool-0002-c2cb19f4-3r0c   v1.36.0-gke.4447000
gke-gb300-gke-test-gb300-pool-0002-c2cb19f4-6hqw   v1.36.0-gke.4447000
```

### gb300-pool-0003（前 3 台样例）
```
NAME                                               KUBELET
gke-gb300-gke-test-gb300-pool-0003-4de40eaf-04fk   v1.36.0-gke.4447000
gke-gb300-gke-test-gb300-pool-0003-4de40eaf-0l5w   v1.36.0-gke.4447000
gke-gb300-gke-test-gb300-pool-0003-4de40eaf-0zpg   v1.36.0-gke.4447000
```

### gb300-pool-0005（前 3 台样例）
```
NAME                                               KUBELET
gke-gb300-gke-test-gb300-pool-0005-bf2e7216-090v   v1.36.0-gke.4447000
gke-gb300-gke-test-gb300-pool-0005-bf2e7216-0v60   v1.36.0-gke.4447000
gke-gb300-gke-test-gb300-pool-0005-bf2e7216-29j1   v1.36.0-gke.4447000
```

### gb300-pool-0007（前 3 台样例）
```
NAME                                               KUBELET
gke-gb300-gke-test-gb300-pool-0007-5b205810-4dxq   v1.36.0-gke.4447000
gke-gb300-gke-test-gb300-pool-0007-5b205810-519k   v1.36.0-gke.4447000
gke-gb300-gke-test-gb300-pool-0007-5b205810-7h61   v1.36.0-gke.4447000
```

