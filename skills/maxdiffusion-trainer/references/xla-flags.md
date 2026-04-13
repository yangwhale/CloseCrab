# XLA Flags for TPU v7 Ironwood Training

21 flags set via `LIBTPU_INIT_ARGS` environment variable. Source: tpu-recipes official recipe.

## Async Communication (7 flags)
```
--xla_enable_async_all_gather=true
--xla_tpu_enable_async_collective_fusion=true
--xla_tpu_enable_async_collective_fusion_fuse_all_gather=true
--xla_enable_async_all_reduce=true
--xla_max_concurrent_async_all_gathers=4
--xla_tpu_enable_async_all_to_all=true
--xla_tpu_prefer_async_allgather_to_allreduce=true
```

## Sparse Core Offloading (7 flags)
```
--xla_tpu_enable_sparse_core_collective_offload_all_reduce=true
--xla_tpu_enable_sparse_core_reduce_scatter_v2=true
--xla_tpu_enable_sparse_core_collective_offload_all_gather=true
--xla_tpu_enable_sparse_core_collective_offload_2d_all_gather=true
--xla_tpu_enable_all_gather_offload_tracing=true
--xla_tpu_use_tc_device_shape_on_sc=true
--xla_tpu_enable_sparse_core_collective_offload_reduce_scatter=true
```

## Scheduling & Memory (3 flags)
```
--xla_latency_hiding_scheduler_rerun=5
--xla_tpu_scoped_vmem_limit_kib=65536
--xla_tpu_enable_tpu_custom_call_scoped_vmem_adjustments=true
```

## Disabled Optimizations (4 flags)
```
--xla_tpu_rwb_fusion=false
--xla_tpu_enable_sublane_major_scaling_bitcast_fusion=false
--xla_tpu_impure_enable_packed_bf16_math_ops=false
--xla_enable_transpose_trace=false
```

## Full String (copy-paste ready)

```
--xla_enable_async_all_gather=true --xla_tpu_enable_async_collective_fusion=true --xla_tpu_enable_async_collective_fusion_fuse_all_gather=true --xla_enable_async_all_reduce=true --xla_tpu_enable_sparse_core_collective_offload_all_reduce=true --xla_max_concurrent_async_all_gathers=4 --xla_tpu_enable_async_all_to_all=true --xla_latency_hiding_scheduler_rerun=5 --xla_tpu_rwb_fusion=false --xla_tpu_enable_sublane_major_scaling_bitcast_fusion=false --xla_tpu_impure_enable_packed_bf16_math_ops=false --xla_tpu_enable_sparse_core_reduce_scatter_v2=true --xla_tpu_enable_sparse_core_collective_offload_all_gather=true --xla_tpu_enable_sparse_core_collective_offload_2d_all_gather=true --xla_tpu_enable_all_gather_offload_tracing=true --xla_tpu_use_tc_device_shape_on_sc=true --xla_tpu_prefer_async_allgather_to_allreduce=true --xla_tpu_enable_sparse_core_collective_offload_reduce_scatter=true --xla_tpu_scoped_vmem_limit_kib=65536 --xla_tpu_enable_tpu_custom_call_scoped_vmem_adjustments=true --xla_enable_transpose_trace=false
```
