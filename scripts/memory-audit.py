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
MEMORY_ARCHIVE_MD = MEM_DIR / "MEMORY-archive.md"
SHARED_DIR = MEM_DIR / "shared"

# Regex matches a slug like feedback_xxx.md / project_yyy.md / user_zzz.md / reference_www.md.
# We tolerate `.` in slugs (e.g. project_deepseek-v3.2-tpuv7.md) — the v0 audit missed those.
SLUG_RE = re.compile(r"(?:feedback|project|user|reference)_[a-z0-9][a-z0-9.\-]*\.md", re.IGNORECASE)
# Regex matches shared/X.md references in MEMORY.md trigger-keyword routing table.
SHARED_RE = re.compile(r"shared/([a-z0-9_-]+\.md)", re.IGNORECASE)
TIMESTAMP_RE = re.compile(r"\(updated\s+(\d{4}-\d{2}-\d{2})\)")
WAKE_HEADER_RE = re.compile(r"^##\s+醒来第一件事", re.MULTILINE)
NEXT_HEADER_RE = re.compile(r"^##\s+", re.MULTILINE)


def list_disk_memories() -> set[str]:
    """All *.md files in memory/ except MEMORY.md / MEMORY-archive.md / shared/."""
    out: set[str] = set()
    for p in MEM_DIR.glob("*.md"):
        if p.name in ("MEMORY.md", "MEMORY-archive.md"):
            continue
        out.add(p.name)
    return out


def list_indexed_slugs(mem_md_text: str, archive_text: str = "") -> dict[str, int]:
    """Return slug -> occurrence count across MEMORY.md + MEMORY-archive.md.

    MEMORY-archive.md (created 2026-05-22) is the second-tier index for
    long-tail archive content. Without including it, all archived projects
    appear as orphans.
    """
    counts: dict[str, int] = {}
    combined = mem_md_text + "\n" + archive_text
    for m in SLUG_RE.finditer(combined):
        slug = m.group(0).lower()
        counts[slug] = counts.get(slug, 0) + 1
    return counts


def detect_shared_dead_links(mem_md_text: str) -> list[str]:
    """Find shared/X.md references in MEMORY.md that don't exist on disk.

    Added 2026-05-22 after discovering 9 dead shared/* references that had
    been silently broken — MEMORY.md trigger-keyword routing pointed bots to
    files that didn't exist on jarvis (they only lived in OpenClaw bot
    workspaces). Bots followed the index and Read 404'd every time.
    """
    dead: list[str] = []
    referenced = {m.group(1).lower() for m in SHARED_RE.finditer(mem_md_text)}
    for fname in sorted(referenced):
        if not (SHARED_DIR / fname).exists():
            dead.append(f"shared/{fname}")
    return dead


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


def to_markdown(report: dict, cold_days: int, action_only: bool = False) -> str:
    """
    Output 3 分类 (R6 设计哲学纠正 2026-05-21):
      🚨 ACTIONABLE: dead_index / duplicates / stale_timestamp — 必清, 无风险
      👀 NEEDS REVIEW: orphans — 需 case-by-case 判断 active/废弃
      ℹ️ INFO ONLY: cold_links — 绝不强清, 仅 INFO. cold ≠ garbage.

    --action-only mode: 只显示 🚨 段; 如果 0 → 输出 "🟢 健康无需 cleanup"; 跳过 cold/orphan/info.
    cron 用 --action-only, 防止形成"每天清 5%"的冲动.
    """
    today = report["today"]
    action_count = (
        len(report["stale_timestamps"])
        + len(report["duplicates"])
        + len(report["dead_index"])
        + len(report.get("shared_dead_links", []))
    )

    # action-only mode: 健康就 silent done
    if action_only and action_count == 0:
        return (
            f"# 🟢 Memory Audit — {today}\n\n"
            f"**actionable=0, 无需 cleanup**. "
            f"MEMORY.md {report['memory_md_lines']} 行 / {len(report['indexed'])} 索引 / "
            f"{report['disk_count']} disk 文件.\n\n"
            "_(cold ≠ garbage. 没新东西进来时, 不要找事清. 类比真正的睡眠 — 没学新知识的晚上, "
            "consolidation 是 maintenance 不是 cleanup.)_"
        )

    lines = [
        f"# 📋 Memory Audit — {today}",
        "",
        "_类比人脑睡眠 consolidation: 修剪噪音/重复, 但 **不删稳定记忆**_",
        "",
    ]

    # === 🚨 ACTIONABLE (必清, 无风险) ===
    lines.append("## 🚨 ACTIONABLE (必清, 无风险)")
    lines.append("")
    if action_count == 0:
        lines.append("**🟢 0 个 — 不需要做任何 cleanup**. 不要去翻下面的 INFO 段找事清.")
        lines.append("")
    else:
        lines.append(f"**{action_count} 个 actionable 信号**. 这些是「明确证据过期」, 清了无风险:")
        lines.append("")
        if report["stale_timestamps"]:
            lines += ["### ⏰ 过期时间戳 (醒来段 >7d)", ""]
            for s in report["stale_timestamps"]:
                lines.append(f"- `{s['date']}` ({s['age_days']}d ago): {s['line']}")
            lines.append("")
        if report["duplicates"]:
            lines += ["### ♊ 重复索引 (同 slug 2+ 次)", ""]
            for slug, n in report["duplicates"]:
                lines.append(f"- `{slug}` × **{n}** 次")
            lines.append("")
        if report["dead_index"]:
            lines += ["### 💀 死索引 (MEMORY 有 disk 没)", ""]
            for s in report["dead_index"]:
                lines.append(f"- `{s}`")
            lines.append("")
        if report.get("shared_dead_links"):
            lines += [
                "### 🪦 shared/ 死链 (索引指向但本机 disk 没该文件)",
                "",
                "MEMORY.md 触发关键词路由表里写了 `shared/X.md` 但本机 disk 不存在. "
                "bot 跟着索引去 Read 会 404. 修复: `rsync ~/.closecrab/openclaw-workspace/bunny/memory/shared/ "
                f"{SHARED_DIR}/` 或删 MEMORY.md 里的 shared/ 索引段.",
                "",
            ]
            for s in report["shared_dead_links"]:
                lines.append(f"- `{s}`")
            lines.append("")

    if action_only:
        return "\n".join(lines)

    # === 👀 NEEDS REVIEW (case-by-case) ===
    if report["orphans"]:
        lines += [
            "## 👀 NEEDS REVIEW (孤儿文件 — case-by-case 判断)",
            "",
            f"**{len(report['orphans'])} 个** disk 有但 MEMORY 没索引. "
            "active 则补索引, 废弃才删 disk. 看 head + mtime 决定.",
            "",
        ]
        for s in report["orphans"][:20]:
            lines.append(f"- `{s}`")
        if len(report["orphans"]) > 20:
            lines.append(f"- ... 还有 {len(report['orphans']) - 20} 个")
        lines.append("")

    # === ℹ️ INFO ONLY (cold links — 绝不强清) ===
    if report["cold_links"]:
        n = len(report["cold_links"])
        lines += [
            f"## ℹ️ INFO ONLY ({n} 个 cold links, 近 {cold_days}d 0 Read)",
            "",
            "**⚠️ 不要因为 cold 就清!** cold ≠ garbage. 大部分是「平时不 trigger, 一旦 trigger "
            "就救命」的深度知识 (TPU/vendor quirk/平台 bug). R5 实证: cold shared/ 改触发关键词后 "
            "命中率从 17%→83%, **而不是该删**.",
            "",
            "_只在 user 主动 ask 'check cold links' 或周末手工审时才看. cron 报告默认不展开._",
            "",
            "<details><summary>展开 (top 10 of " + str(n) + ")</summary>",
            "",
        ]
        for s in report["cold_links"][:10]:
            lines.append(f"- `{s}`")
        lines += ["", "</details>", ""]

    # 总评
    health = "🟢 健康" if action_count == 0 else "🟡 有 actionable 项需要清"
    lines += [
        "## 总评",
        f"**状态**: {health}",
        f"- MEMORY.md: {report['memory_md_lines']} 行 / {report['memory_md_bytes']} bytes",
        f"- disk 上 memory 文件: {report['disk_count']}",
        f"- MEMORY.md 索引数: {len(report['indexed'])}",
        f"- ACTIONABLE: {action_count} | NEEDS REVIEW: {len(report['orphans'])} | "
        f"INFO ONLY: {len(report['cold_links'])}",
        "",
        "_GC 哲学: pressure-driven, 不是 time-driven. 没积累就不该清. cold ≠ garbage._",
    ]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true", help="output JSON instead of markdown")
    p.add_argument("--cold-days", type=int, default=14,
                   help="window for cold link detection (default 14)")
    p.add_argument("--stale-days", type=int, default=7,
                   help="醒来段 timestamp stale threshold (default 7)")
    p.add_argument("--action-only", action="store_true",
                   help="only show 🚨 ACTIONABLE section (dead/dup/stale). "
                        "If empty -> '🟢 健康无需 cleanup'. cron uses this to avoid "
                        "creating 'must-clean-daily' compulsion. cold ≠ garbage.")
    p.add_argument("--deep", action="store_true",
                   help="(future) execute cleanup actions; currently report-only")
    p.add_argument("--apply", action="store_true",
                   help="(with --deep) actually modify files; without --apply = dry-run")
    args = p.parse_args()

    if not MEMORY_MD.exists():
        print(f"ERROR: {MEMORY_MD} not found", file=sys.stderr)
        sys.exit(1)

    text = MEMORY_MD.read_text()
    archive_text = MEMORY_ARCHIVE_MD.read_text() if MEMORY_ARCHIVE_MD.exists() else ""
    disk = list_disk_memories()
    indexed = list_indexed_slugs(text, archive_text)
    today = date.today()

    orphans, dead = detect_orphans_and_dead(disk, indexed)
    duplicates = detect_duplicates(indexed)
    stale = detect_stale_timestamps(text, today, args.stale_days)
    cold = detect_cold_links(indexed, args.cold_days)
    shared_dead = detect_shared_dead_links(text)

    report = {
        "today": today.isoformat(),
        "memory_md_lines": text.count("\n"),
        "memory_md_bytes": len(text.encode("utf-8")),
        "disk_count": len(disk),
        "indexed": sorted(indexed),
        "orphans": orphans,
        "dead_index": dead,
        "shared_dead_links": shared_dead,
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
        print(to_markdown(report, args.cold_days, action_only=args.action_only))


if __name__ == "__main__":
    main()
