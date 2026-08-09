#!/usr/bin/env python3
"""Wiki Query Benchmark — 评估查询引擎的精度和性能。

指标：
  - P@1: Top-1 精确率
  - P@3: Top-3 命中率
  - MRR: Mean Reciprocal Rank
  - Latency: 平均/P95/P99 响应时间

用法:
  python3 benchmark.py
  python3 benchmark.py --verbose
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from query import query, get_index


def run_benchmark(test_file: str = None, verbose: bool = False):
    """运行 benchmark 并输出指标。"""
    if test_file is None:
        test_file = str(Path(__file__).parent / "test_queries.json")

    tests = json.loads(Path(test_file).read_text(encoding="utf-8"))

    # 预热索引
    t0 = time.perf_counter()
    idx = get_index()
    build_ms = (time.perf_counter() - t0) * 1000
    print(f"索引构建: {build_ms:.1f}ms, {idx.page_count} pages\n")

    # 运行测试
    p1_hits = 0
    p3_hits = 0
    mrr_sum = 0.0
    latencies = []
    evaluable_p1 = 0  # 有 expected_top1 的测试数
    evaluable_p3 = 0  # 有 expected_top3 的测试数
    total = len(tests)

    for test in tests:
        q = test["query"]
        expected_top1 = test.get("expected_top1")
        expected_top3 = test.get("expected_top3", [])

        t0 = time.perf_counter()
        results = query(q, top_k=5)
        dt_ms = (time.perf_counter() - t0) * 1000
        latencies.append(dt_ms)

        result_slugs = [Path(r["path"]).stem for r in results]

        # P@1
        p1_ok = False
        if expected_top1:
            evaluable_p1 += 1
            if result_slugs and result_slugs[0] == expected_top1:
                p1_ok = True
                p1_hits += 1

        # P@3
        p3_ok = False
        if expected_top3:
            evaluable_p3 += 1
            if any(slug in result_slugs[:3] for slug in expected_top3):
                p3_ok = True
                p3_hits += 1

        # MRR
        rr = 0.0
        if expected_top3:
            for i, slug in enumerate(result_slugs):
                if slug in expected_top3:
                    rr = 1.0 / (i + 1)
                    break
            mrr_sum += rr

        if verbose:
            status = "✓" if (p1_ok or p3_ok or not expected_top1 and not expected_top3) else "✗"
            print(f"  {status} {q:30s} → {dt_ms:6.2f}ms  top3={result_slugs[:3]}")
            if expected_top1 and not p1_ok:
                print(f"    expected top1: {expected_top1}, got: {result_slugs[0] if result_slugs else '(none)'}")

    # 计算指标
    latencies.sort()
    avg_lat = sum(latencies) / len(latencies) if latencies else 0
    p95_lat = latencies[int(len(latencies) * 0.95)] if latencies else 0
    p99_lat = latencies[int(len(latencies) * 0.99)] if latencies else 0

    p1_rate = p1_hits / evaluable_p1 * 100 if evaluable_p1 else 0
    p3_rate = p3_hits / evaluable_p3 * 100 if evaluable_p3 else 0
    mrr = mrr_sum / evaluable_p3 if evaluable_p3 else 0

    print(f"\n{'='*50}")
    print(f"Benchmark Results ({total} queries)")
    print(f"{'='*50}")
    print(f"  P@1:          {p1_hits}/{evaluable_p1} = {p1_rate:.1f}%")
    print(f"  P@3:          {p3_hits}/{evaluable_p3} = {p3_rate:.1f}%")
    print(f"  MRR:          {mrr:.3f}")
    print(f"  Avg Latency:  {avg_lat:.2f}ms")
    print(f"  P95 Latency:  {p95_lat:.2f}ms")
    print(f"  P99 Latency:  {p99_lat:.2f}ms")
    print(f"  Index Build:  {build_ms:.1f}ms")

    return {
        "p1": p1_rate, "p3": p3_rate, "mrr": mrr,
        "avg_latency_ms": avg_lat, "p95_latency_ms": p95_lat,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wiki Query Benchmark")
    parser.add_argument("--verbose", "-v", action="store_true", help="显示每个查询的详细结果")
    parser.add_argument("--test-file", default=None, help="测试集文件路径")
    args = parser.parse_args()
    run_benchmark(args.test_file, args.verbose)
