---
name: maxdiffusion-trainer
description: |
  Train diffusion models (Wan 2.1, Stable Diffusion, SDXL) on TPU using MaxDiffusion framework.
  Use when the user says "训练 diffusion 模型", "跑 MaxDiffusion", "Wan 训练", "MaxDiffusion benchmark",
  "视频生成模型训练", "TPU 训练 Wan", "复现 benchmark", "跑 Wan 2.1", "MaxDiffusion training",
  or any task involving MaxDiffusion on TPU/GPU.
---

# MaxDiffusion Trainer

Train diffusion models on TPU/GPU using the [MaxDiffusion](https://github.com/AI-Hypercomputer/maxdiffusion) framework.

## Supported Models

| Model | Config | Notes |
|-------|--------|-------|
| Wan 2.1 14B (T2V) | `base_wan_14b.yml` | 视频生成，v3 分支推荐 |
| Wan 2.1 1.3B (T2V) | `base_wan_1.3b.yml` | 小模型 |
| Stable Diffusion XL | `base_xl.yml` | 图像生成 |

## Branch Selection

- **Repo**: `https://github.com/AI-Hypercomputer/maxdiffusion`
- **v3 分支**: 3D mesh `[data, fsdp, tensor]`，无 Context Parallelism，官方 Ironwood benchmark 用此分支
- **main 分支**: 4D mesh `[data, fsdp, context, tensor]`，有 ring attention / CP，但性能低 40-80%

**规则**：TPU v7 Ironwood benchmark → **v3**；需要超长序列 Context Parallelism → **main**

## Wan 2.1 14B on TPU v7 Quick Reference

```
分辨率: 1280x720x81 帧 (序列长度 75,600)
并行策略: DP=32, FSDP=4, TP=1 (v3 分支, 无 CP)
per_device_batch_size: 0.25 (GBS=32)
remat_policy: FULL
attention: flash
镜像: us-docker.pkg.dev/tpu-prod-env-multipod/jax-stable-stack/tpu/jax_nightly:latest
scan_layers: False (展开 40 层, +7.6%)
DVFS: --xla_tpu_dvfs_p_state=7 (锁最高频, +11.2%)
预期 step time: ~18.8s (优化后) / ~23s (默认) (64 chip, 4x4x4)
```

## Workflow

### 1. Data Preparation (PusaV1)

PusaV1: HuggingFace `AI-Hypercomputer/PusaV1`, 3860 video-text pairs, ~85 GB raw → ~100 GB tfrecord.

```bash
# Download (需 HF token, ~10 min on us-central1 VM)
huggingface-cli download AI-Hypercomputer/PusaV1 \
  --repo-type dataset --local-dir ~/datasets/PusaV1_training --token $HF_TOKEN

# Convert to tfrecord (CPU VM 即可, ~15 min)
cd /tmp && git clone https://github.com/AI-Hypercomputer/maxdiffusion.git && cd maxdiffusion && git checkout v3
pip install torch jax flax chex accelerate tensorflow && pip install -r requirements.txt
python3 -m src.maxdiffusion.data_preprocessing.wan_pusav1_to_tfrecords \
  src/maxdiffusion/configs/base_wan_14b.yml \
  train_data_dir=$HOME/datasets/PusaV1_training \
  tfrecords_dir=$HOME/datasets/PusaV1_tfrecords \
  no_records_per_shard=100 skip_jax_distributed_system=True

# Upload to GCS (必须与 GKE 集群同项目 bucket!)
gsutil -m cp -r ~/datasets/PusaV1_tfrecords/ gs://BUCKET/datasets/PusaV1_training/PusaV1_tfrecords/
```

### 2. Create & Submit JobSet

Use `references/wan14b-v3-jobset-template.yaml` as template. Replace:
- `JOB_NAME` → JobSet 名称
- `DATASET_DIR` → GCS tfrecord 路径 (e.g. `gs://bucket/datasets/PusaV1_training/PusaV1_tfrecords`)
- `MAX_TRAIN_STEPS` → 训练步数 (benchmark: 10)

```bash
kubectl apply -f jobset.yaml
kubectl get pods -l jobset.sigs.k8s.io/jobset-name=JOB_NAME
```

### 3. Monitor

Metrics 只在 `jax.process_index()==0` 的 Pod 输出（不一定是 Pod-0）。

```bash
# 找 metrics pod
for pod in $(kubectl get pods -l jobset.sigs.k8s.io/jobset-name=JOB_NAME \
  --no-headers -o custom-columns=NAME:.metadata.name); do
  if kubectl logs $pod 2>/dev/null | grep -q "completed step"; then
    kubectl logs $pod | grep "completed step"; break
  fi
done
```

## Critical Pitfalls

1. **GCS bucket 必须与 GKE 集群同项目** — 否则 403 scope error
2. **`dataset_save_location` 必须含 `gs://`** — 否则 tfrecord 走错误 feature schema（要求 `clip_embeddings` 而非 `latents`）
3. **v3 用 DP=32，main 需 DP=8+CP=4** — 混用会报错
4. **`output_dir` 可用本地路径** — 避免跨项目 GCS 写权限，benchmark 不需 checkpoint
5. **process_index=0 ≠ Pod-0** — grep `"completed step"` 定位 metrics pod

## XLA Flags

22 个 XLA flags 通过 `LIBTPU_INIT_ARGS` 设置，详见 `references/xla-flags.md`。

**关键优化 flag**：`--xla_tpu_dvfs_p_state=7` 锁定 TPU v7 最大频率（单独提升 ~11%，TPU v7 专有）。

## Optimization Summary

最佳配置 = `scan_layers=False` + `--xla_tpu_dvfs_p_state=7`，比官方快 **23.6%**，零代码修改。

| 优化项 | Step Time | TFLOP/s/dev | vs 官方 | 原理 |
|--------|-----------|-------------|---------|------|
| 官方 Benchmark | 24.58s | 209.6 | — | tpu-recipes 基准 |
| Baseline 复现 | 22.92s | 213.5 | 快 6.8% | JAX nightly + 数据缓存 |
| + scan_layers=False | 21.18s | 231.1 | 快 13.8% | 展开 40 层, XLA 跨层优化 |
| **+ DVFS p_state=7** | **18.82s** | **260.1** | **快 23.6%** | 锁最高频率 |

## Failed Experiments (不要再试)

| 实验 | 结果 | 原因 |
|------|------|------|
| flash block=1024 | 慢 24% | backward kernel 最优 block 不同于 forward |
| fused_bwd_kernel | 慢 49% | v7 VMEM 布局不适合 |
| 2-level blocking (kv_compute=256) | 慢 105% | backward 计算/内存 trade-off 不同 |
| bwd blocks=256 | 慢 278% | tile 迭代次数暴增 |
| exp2 custom Pallas kernel | 报错 | Pallas AD 限制, 无法训练 |
| remat MATMUL_WITHOUT_BATCH | OOM | HBM 不足 |

## Detailed Docs

- End-to-end guide: `$CC_PAGES_URL_PREFIX/assets/wan21-reproduce-guide-public.html`
- Benchmark report: `$CC_PAGES_URL_PREFIX/pages/wan21-benchmark-20260402.html`
