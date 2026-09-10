#!/usr/bin/env python3
"""按思考档位分组统计语音桥的回话延迟。

数据来自 `⏱️ [延迟]` 那些行，由 `gemini_live_bridge._log_turn_latency` 写入：

    [2026-09-10 15:02:11] ⏱️ [延迟]: level=high ttfa=2.41 total=7.83 thoughts=118

两个数各自的含义（**都不是「模型有多快」的绝对值**）：

- `ttfa` —— 从「Discord 不再给包」到听见第一个字。这是用户真正感受到的等待。
  里面混着服务端 VAD 那 1.2 秒静音判定和网络往返，所以绝对值偏大是正常的。
- `total` —— 同一个起点到这一轮说完。它跟回答长短强相关，**不适合单独看**。

**只能横向比，不能纵向解读。** 两档之间 VAD、网络、persona 都一样，差值才是
思考档位的代价；单看一个 2.4 秒说明不了任何事。

报中位数而不只是均值：语音场景里偶尔一次重连或网络抖动会拉出一个十几秒的
离群点，均值被它一个人拽着走，中位数不会。

用法:
    scripts/voice-latency-report.py [日志路径]
"""
import re
import statistics
import sys
from collections import defaultdict

DEFAULT_LOG = "/tmp/gemini-live-delivery-bunny.log"
LINE_RE = re.compile(r"⏱️ \[延迟\]:\s*(.+)$")


def parse(path):
    rows = defaultdict(list)
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE_RE.search(line)
            if not m:
                continue
            kv = {}
            for part in m.group(1).split():
                if "=" in part:
                    k, v = part.split("=", 1)
                    kv[k] = v
            rows[kv.get("level", "?")].append(kv)
    return rows


def _nums(samples, key):
    out = []
    for s in samples:
        v = s.get(key)
        if v in (None, "n/a"):
            continue
        try:
            out.append(float(v))
        except ValueError:
            pass
    return out


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG
    rows = parse(path)
    if not rows:
        print(f"{path} 里一条延迟记录都没有 —— 先对着麦克风说几轮话。")
        return 1

    print(f"来源: {path}\n")
    print(f"{'档位':<10}{'轮次':>6}{'首音中位':>10}{'首音均值':>10}"
          f"{'整轮中位':>10}{'思考token中位':>14}")
    print("-" * 62)
    summary = {}
    for level in ("minimal", "low", "medium", "high", "?"):
        samples = rows.get(level)
        if not samples:
            continue
        ttfa = _nums(samples, "ttfa")
        total = _nums(samples, "total")
        th = _nums(samples, "thoughts")
        summary[level] = statistics.median(ttfa) if ttfa else None

        def cell(vals, fmt, width, agg=statistics.median):
            return f"{agg(vals):>{width}{fmt}}" if vals else f"{'n/a':>{width}}"

        print(
            f"{level:<10}{len(samples):>6}"
            + cell(ttfa, ".2f", 10)
            + cell(ttfa, ".2f", 10, statistics.mean)
            + cell(total, ".2f", 10)
            + cell(th, ".0f", 14)
        )

    if summary.get("low") and summary.get("high"):
        d = summary["high"] - summary["low"]
        print(f"\nhigh 比 low 首音慢 {d:+.2f} 秒"
              f"（{d / summary['low'] * 100:+.0f}%）")
    n = sum(len(v) for v in rows.values())
    if n < 10:
        print(f"\n⚠️ 一共才 {n} 轮，样本太少，别当结论 —— 每档至少说十来轮再看。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
