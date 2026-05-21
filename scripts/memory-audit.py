#!/usr/bin/env python3
"""memory-audit.py — Auto memory GC audit (and optional cleanup).

Inspired by 人类睡眠时大脑做 memory consolidation: 修剪过期 / 重复 / 孤立的索引,
保留 cold 但 valuable 的深度知识. R5 教训: cold ≠ stale, 只删有证据过期的.

Usage:
    # daily audit (read-only, markdown report to stdout)
    python3 memory-audit.py

    # JSON for programmatic consumption
    python3 memory-audit.py --json

    # adjust cold window
    python3 memory-audit.py --cold-days 30

    # deep clean (dry-run by default; --apply to actually do it)
    python3 memory-audit.py --deep
    python3 memory-audit.py --deep --apply

Five signals detected:
    1. orphans         disk 上有但 MEMORY.md 没索引到的 memory 文件
    2. dead_index      MEMORY.md 索引了但 disk 上找不到
    3. duplicates      同一 slug 在 MEMORY.md 出现 2+ 次
    4. stale_timestamp 「醒来第一件事」段超过 7 天的 `(updated YYYY-MM-DD)`
    5. cold_links      索引了但近 N 天 0 个 Read tool_use 命中

R5 reference: feedback_memory-system-overfit-r1-r5.md
R6 timestamp rule: 醒来第一件事 每个 item 必须带 `(updated YYYY-MM-DD)`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
MEM_DIR = HOME / ".claude" / "projects" / "-home-chrisya" / "memory"
JSONL_DIR = HOME / ".claude" / "projects" / "-home-chrisya"
MEMORY_MD = MEM_DIR / "MEMORY.md"

# Regex matches a slug like feedback_xxx.md / project_yyy.md / user_zzz.md / reference_www.md.
# We tolerate `.` in slugs (e.g. project_deepseek-v3.2-tpuv7.md) — the v0 audit missed those.
SLUG_RE = re.compile(r"(?:feedback|project|user|reference)_[a-z0-9][a-z0-9.\-]*\.md", re.IGNORECASE)
TIMESTAMP_RE = re.compile(r"\(updated\s+(\d{4}-\d{2}-\d{2})\)")
WAKE_HEADER_RE = re.compile(r"^##\s+醒来第一件事", re.MULTILINE)
NEXT_HEADER_RE = re.compile(r"^##\s+", re.MULTILINE)


def list_disk_memories() -> set[str]:
    """All *.md files in memory/ except MEMORY.md and shared/."""
    out: set[str] = set()
    for p in MEM_DIR.glob("*.md"):
        if p.name == "MEMORY.md":
            continue
        out.add(p.name)
    return out


def list_indexed_slugs(mem_md_text: str) -> dict[str, int]:
    """Return slug -> occurrence count in MEMORY.md."""
    counts: dict[str, int] = {}
    for m in SLUG_RE.finditer(mem_md_text):
        slug = m.group(0).lower()
        counts[slug] = counts.get(slug, 0) + 1
    return counts


def find_wake_section(text: str) -> str:
    """Slice the '## 醒来第一件事' section."""
    m = WAKE_HEADER_RE.search(text)
    if not m:
        return ""
    start = m.start()
    rest = text[start + 1:]  # skip the # to find next ##
    n = NEXT_HEADER_RE.search(rest)
    end = (start + 1 + n.start()) if n else len(text)
    return text[start:end]


def detect_stale_timestamps(text: str, today: date, max_age_days: int = 7) -> list[dict]:
    """Find `(updated YYYY-MM-DD)` more than max_age_days old in 醒来段."""
    section = find_wake_section(text)
    if not section:
        return []
    found = []
    for line in section.splitlines():
        for m in TIMESTAMP_RE.finditer(line):
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            age = (today - ts).days
            if age > max_age_days:
                preview = line.strip()[:120]
                found.append({"date": m.group(1), "age_days": age, "line": preview})
    return found


def detect_orphans_and_dead(disk: set[str], indexed: dict[str, int]) -> tuple[list[str], list[str]]:
    indexed_set = {s.lower() for s in indexed}
    disk_lower = {s.lower() for s in disk}
    orphans = sorted(disk_lower - indexed_set)
    dead = sorted(indexed_set - disk_lower)
    return orphans, dead


def detect_duplicates(indexed: dict[str, int]) -> list[tuple[str, int]]:
    return sorted([(s, n) for s, n in indexed.items() if n >= 2], key=lambda x: -x[1])


def detect_cold_links(indexed: dict[str, int], cold_days: int) -> list[str]:
    """Slugs indexed in MEMORY.md but with 0 Read tool_use hits in jsonls within cold_days."""
    cutoff_sec = int(datetime.now().timestamp() - cold_days * 86400)
    # Build a set of memory filenames that were Read
    read_basenames: set[str] = set()
    try:
        jsonls = [p for p in JSONL_DIR.glob("*.jsonl") if p.stat().st_mtime >= cutoff_sec]
    except OSError:
        return []
    if not jsonls:
        return []
    # jq filter — avoid loading every line into Python
    jq_filter = (
        'select(.type=="assistant") | .message.content[]? '
        '| select(.type=="tool_use" and .name=="Read") | .input.file_path'
    )
    try:
        proc = subprocess.run(
            ["jq", "-r", jq_filter, *[str(p) for p in jsonls]],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    for line in proc.stdout.splitlines():
        if "memory/" in line:
            read_basenames.add(Path(line).name.lower())
    cold = [s for s in indexed if s.lower() not in read_basenames]
    return sorted(cold)


def to_markdown(report: dict, cold_days: int) -> str:
    today = report["today"]
    lines = [
        f"# 📋 Memory Audit — {today}",
        "",
        f"_自动 GC 体检 by `memory-audit.py` (类比人脑睡眠周期的 memory consolidation)_",
        "",
        "## 5 个健康信号",
        "",
        "| 信号 | 数量 | 含义 / 建议 |",
        "|---|---|---|",
        f"| **stale_timestamp** (醒来段 >7d) | **{len(report['stale_timestamps'])}** | "
        f"过期 item 应 demote/删除 ([R6 规则](feedback_memory-system-overfit-r1-r5.md)) |",
        f"| **duplicates** (同 slug 2+ 次) | **{len(report['duplicates'])}** | "
        "MEMORY.md 重复索引, 浪费 prompt token |",
        f"| **orphans** (disk 有 MEMORY 没) | **{len(report['orphans'])}** | "
        "孤儿文件 bot 永远找不到, 应补索引或删 disk |",
        f"| **dead_index** (MEMORY 有 disk 没) | **{len(report['dead_index'])}** | "
        "死索引指向不存在文件, click 必 404, 必须删 |",
        f"| **cold_links** (近 {cold_days}d 0 Read) | **{len(report['cold_links'])}** | "
        "cold ≠ stale: 长期保险知识可保留, 但闭环项目 pointer 应删 |",
        "",
    ]

    if report["stale_timestamps"]:
        lines += ["## ⏰ 过期时间戳 (醒来段 >7d)", ""]
        for s in report["stale_timestamps"]:
            lines.append(f"- `{s['date']}` ({s['age_days']}d ago): {s['line']}")
        lines.append("")

    if report["duplicates"]:
        lines += ["## ♊ 重复索引", ""]
        for slug, n in report["duplicates"]:
            lines.append(f"- `{slug}` × **{n}** 次")
        lines.append("")

    if report["orphans"]:
        lines += [f"## 👻 孤儿文件 ({len(report['orphans'])} 个)", ""]
        for s in report["orphans"][:20]:
            lines.append(f"- `{s}`")
        if len(report["orphans"]) > 20:
            lines.append(f"- ... 还有 {len(report['orphans']) - 20} 个")
        lines.append("")

    if report["dead_index"]:
        lines += ["## 💀 死索引 (必删)", ""]
        for s in report["dead_index"]:
            lines.append(f"- `{s}`")
        lines.append("")

    if report["cold_links"]:
        n = len(report["cold_links"])
        lines += [f"## 🧊 Cold Links (top 20 of {n}, 自己判断 cold ≠ stale)", ""]
        for s in report["cold_links"][:20]:
            lines.append(f"- `{s}`")
        if n > 20:
            lines.append(f"- ... 还有 {n - 20} 个")
        lines.append("")

    health = "🟢 健康" if (
        not report["stale_timestamps"]
        and not report["duplicates"]
        and not report["dead_index"]
        and len(report["orphans"]) <= 2
    ) else "🟡 有可清理项"
    lines += [
        "## 总评",
        f"**状态**: {health}",
        f"- MEMORY.md: {report['memory_md_lines']} 行 / {report['memory_md_bytes']} bytes",
        f"- disk 上 memory 文件: {report['disk_count']}",
        f"- MEMORY.md 索引数: {len(report['indexed'])}",
        "",
        "_建议: 看到 stale/dead/重复 立刻清; orphans/cold 周末再审_",
    ]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true", help="output JSON instead of markdown")
    p.add_argument("--cold-days", type=int, default=14,
                   help="window for cold link detection (default 14)")
    p.add_argument("--stale-days", type=int, default=7,
                   help="醒来段 timestamp stale threshold (default 7)")
    p.add_argument("--deep", action="store_true",
                   help="(future) execute cleanup actions; currently report-only")
    p.add_argument("--apply", action="store_true",
                   help="(with --deep) actually modify files; without --apply = dry-run")
    args = p.parse_args()

    if not MEMORY_MD.exists():
        print(f"ERROR: {MEMORY_MD} not found", file=sys.stderr)
        sys.exit(1)

    text = MEMORY_MD.read_text()
    disk = list_disk_memories()
    indexed = list_indexed_slugs(text)
    today = date.today()

    orphans, dead = detect_orphans_and_dead(disk, indexed)
    duplicates = detect_duplicates(indexed)
    stale = detect_stale_timestamps(text, today, args.stale_days)
    cold = detect_cold_links(indexed, args.cold_days)

    report = {
        "today": today.isoformat(),
        "memory_md_lines": text.count("\n"),
        "memory_md_bytes": len(text.encode("utf-8")),
        "disk_count": len(disk),
        "indexed": sorted(indexed),
        "orphans": orphans,
        "dead_index": dead,
        "duplicates": duplicates,
        "stale_timestamps": stale,
        "cold_links": cold,
    }

    if args.deep:
        # Reserved for future weekly cleanup. Currently report-only.
        print("# NOTE: --deep currently report-only; cleanup actions TBD\n", file=sys.stderr)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(to_markdown(report, args.cold_days))


if __name__ == "__main__":
    main()
