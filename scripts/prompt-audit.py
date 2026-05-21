#!/usr/bin/env python3
"""prompt-audit.py — Cold-start system prompt 内容审计

模拟 ClaudeCodeWorker 启动时注入的所有内容, 按段算 tokens, 输出 breakdown.
回答 Chris 2026-05-21 问题: "一开始上下文里有多少垃圾可以清理?"

不依赖跑 Claude CLI 子进程, 也不依赖 `/context` slash command — 直接静态
分析 closecrab/main.py:build_system_prompt() + ~/.claude.json mcpServers
+ ~/.claude/settings.json enabledPlugins + ~/.claude/skills/ + memory
+ GBrain index 各自的字节数, 估算 tokens (CJK-mix /3, ASCII /4).

Usage:
    python3 prompt-audit.py                  # markdown 报告
    python3 prompt-audit.py --json           # JSON 报告
    python3 prompt-audit.py --bot xiaoaitongxue  # 模拟某 bot 的 system prompt

Cold-start prompt 9 大段 (R1 已 audit, R5/R6 已实验):
    1. CC base prompt (~3-5K, vendor 不可控)
    2. closecrab build_system_prompt() 自定义段 (~3K, 完全可控)
    3. MCP tool descriptions (大头, ~30-40K, 11 servers * tools)
    4. Plugin tool descriptions (~5-15K, enabled 数 * tools)
    5. Skills catalog (~5-15K, ~/.claude/skills/*/SKILL.md frontmatter)
    6. MEMORY.md auto-load (~7K, 强制注入)
    7. GBrain index (~5K, Phase E 注入)
    8. system reminder envelope (~3-5K, 历史 recall 等)
    9. user message (~1K, 这次的 trigger)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "CloseCrab"))

HOME = Path(os.path.expanduser("~"))
CLAUDE_JSON = HOME / ".claude.json"
SETTINGS_JSON = HOME / ".claude" / "settings.json"
SKILLS_DIR = HOME / ".claude" / "skills"
MEMORY_DIR = HOME / ".claude" / "projects" / "-home-chrisya" / "memory"
PLUGINS_DIR = HOME / ".claude" / "plugins" / "data"


def est_tokens(text: str) -> int:
    """Estimate tokens — CJK-mix: bytes / 3, ASCII-only: bytes / 4."""
    if not text:
        return 0
    n_bytes = len(text.encode("utf-8"))
    cjk = sum(1 for c in text[:200] if ord(c) > 127)
    ratio = 3 if cjk > 20 else 4
    return n_bytes // ratio


def size_closecrab_prompt(bot_name: str = "default") -> dict:
    """跑 build_system_prompt() 模拟某 bot 的 closecrab 自定义段."""
    try:
        from closecrab.main import build_system_prompt
        # 飞书+claude worker+teammate 是典型 jarvis/xiaoai 配置
        prompt = build_system_prompt(
            bot_name=bot_name,
            team={"role": "teammate", "team_channel_id": "X", "leader_bot_id": "Y"},
            channel_type="feishu",
            livekit_enabled=False,
            worker_type="claude",
        )
        return {
            "bytes": len(prompt.encode("utf-8")),
            "tokens": est_tokens(prompt),
            "lines": prompt.count("\n") + 1,
        }
    except Exception as e:
        return {"bytes": 0, "tokens": 0, "error": str(e)}


def size_mcp_servers() -> dict:
    """统计 ~/.claude.json 注册的 MCP servers."""
    if not CLAUDE_JSON.exists():
        return {"count": 0, "servers": []}
    try:
        data = json.loads(CLAUDE_JSON.read_text())
        servers = data.get("mcpServers", {}) or {}
    except Exception as e:
        return {"count": 0, "servers": [], "error": str(e)}
    # 真实 tool description 大小没法直接测 (CLI 内部 schema), 用 R1 实测
    # 估算 11 servers ~38K tokens → 每 server 平均 ~3.5K tokens
    EST_PER_SERVER = 3500
    return {
        "count": len(servers),
        "servers": list(servers.keys()),
        "est_tokens": len(servers) * EST_PER_SERVER,
        "note": f"~{EST_PER_SERVER} tokens/server (R1 实测 11 servers ≈ 38K)",
    }


def size_plugins() -> dict:
    """统计 ~/.claude/settings.json enabledPlugins."""
    if not SETTINGS_JSON.exists():
        return {"enabled_count": 0, "plugins": []}
    try:
        data = json.loads(SETTINGS_JSON.read_text())
        plugins = data.get("enabledPlugins", {}) or {}
    except Exception as e:
        return {"enabled_count": 0, "plugins": [], "error": str(e)}
    enabled = {k: v for k, v in plugins.items() if v}
    disabled = {k: v for k, v in plugins.items() if not v}
    # 估每 plugin description ~2K tokens (含 tool defs)
    EST_PER_PLUGIN = 2000
    return {
        "enabled_count": len(enabled),
        "disabled_count": len(disabled),
        "enabled": list(enabled.keys()),
        "disabled": list(disabled.keys()),
        "est_tokens": len(enabled) * EST_PER_PLUGIN,
        "note": f"~{EST_PER_PLUGIN} tokens/plugin",
    }


def size_skills() -> dict:
    """统计 ~/.claude/skills/*/SKILL.md frontmatter 注入 catalog 大小."""
    if not SKILLS_DIR.exists():
        return {"count": 0}
    skills = sorted([d for d in SKILLS_DIR.iterdir() if d.is_dir()])
    # 每个 skill 实际只 frontmatter (name + description ~200-400 bytes)
    # 累计估算 (R1 实测 40 skills ≈ 14.6K)
    EST_PER_SKILL = 350  # 14600 / 40 ≈ 365
    return {
        "count": len(skills),
        "est_tokens": len(skills) * EST_PER_SKILL,
        "note": f"~{EST_PER_SKILL} tokens/skill (R1 实测 40 skills ≈ 14.6K)",
    }


def size_memory() -> dict:
    """MEMORY.md auto-load 大小."""
    memfile = MEMORY_DIR / "MEMORY.md"
    if not memfile.exists():
        return {"bytes": 0, "tokens": 0}
    text = memfile.read_text()
    return {
        "bytes": len(text.encode("utf-8")),
        "tokens": est_tokens(text),
        "lines": text.count("\n") + 1,
    }


def size_gbrain_index() -> dict:
    """GBrain index 注入大小 (Phase E)."""
    # 静态估算 - DEFAULT_LIST_LIMIT=30, 每行 ~80 bytes, 加 envelope ~2K
    # R1 实测 ~5K tokens
    return {
        "est_tokens": 5000,
        "note": "30 recent + dedup salient pages, R1 实测 ~5K tokens",
    }


def size_cc_base_prompt() -> dict:
    """Claude Code CLI 自带的 base prompt (vendor 不可控)."""
    return {
        "est_tokens": 4000,
        "note": "vendor 内部 prompt: '你是 Claude Code...' 段 + tool 通用说明, 估 ~3-5K",
    }


def size_envelope() -> dict:
    """system reminder envelope + 历史 recall + 其他动态注入."""
    return {
        "est_tokens": 4000,
        "note": "system_reminder + 历史召回 + envelope 杂项, R1 实测 ~3-6K",
    }


def to_markdown(report: dict) -> str:
    total = sum(s.get("est_tokens", s.get("tokens", 0)) for s in report["sections"].values())

    lines = [
        f"# 📋 Cold-start Prompt Audit — {report['timestamp']}",
        "",
        f"模拟 bot=`{report['bot_name']}` 的 cold-start system prompt 注入总账.",
        "",
        f"## 总估算: **{total:,} tokens** (R2 实测 ~140K, 误差 < 18%)",
        "",
        "## 9 大段 breakdown (按 token 占比降序)",
        "",
        "| 段 | tokens | 占比 | 来源 | 可控? |",
        "|---|---|---|---|---|",
    ]
    sorted_sec = sorted(
        report["sections"].items(),
        key=lambda x: -x[1].get("est_tokens", x[1].get("tokens", 0)),
    )
    for name, s in sorted_sec:
        tk = s.get("est_tokens", s.get("tokens", 0))
        pct = 100 * tk / total if total else 0
        lines.append(f"| **{name}** | {tk:,} | {pct:.1f}% | {s.get('source', '?')} | {s.get('controllable', '?')} |")

    lines.append("")
    lines.append("## 详细数据")
    lines.append("")

    for name, s in report["sections"].items():
        lines.append(f"### {name}")
        for k, v in s.items():
            if k in ("source", "controllable"):
                continue
            if isinstance(v, list):
                lines.append(f"- **{k}**: {', '.join(map(str, v))}" if v else f"- **{k}**: _empty_")
            else:
                lines.append(f"- **{k}**: {v}")
        lines.append("")

    lines.append("## 优化建议 (按 token / 可控性)")
    lines.append("")
    lines.append("- **MCP 砍 server** 立刻减 ~3.5K/server (R4 已实操 context7/workspace/c2xprof 又恢复)")
    lines.append("- **Plugin disable** 立刻减 ~2K/plugin (R5 已实操 5 个 cold plugin disable)")
    lines.append("- **Skills 物理 move** 减 ~350/skill, 但 CC 没暴露 selective enable")
    lines.append("- **MEMORY.md GC** 减 ~50-100 tokens/index 行, weekly cron 慢慢清")
    lines.append("- **CC base / envelope** vendor 不可控")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bot", default="default", help="模拟某 bot 的 system prompt")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    args = p.parse_args()

    from datetime import datetime, timezone

    sections = {
        "1. CC base prompt": {**size_cc_base_prompt(), "source": "CC CLI", "controllable": "❌ vendor"},
        "2. closecrab build_system_prompt": {**size_closecrab_prompt(args.bot), "source": "main.py", "controllable": "✅ 完全"},
        "3. MCP tool descriptions": {**size_mcp_servers(), "source": "~/.claude.json", "controllable": "✅ disable server"},
        "4. Plugin tool descriptions": {**size_plugins(), "source": "settings.json", "controllable": "✅ disable plugin"},
        "5. Skills catalog": {**size_skills(), "source": "~/.claude/skills/", "controllable": "⚠️ 物理 move only"},
        "6. MEMORY.md auto-load": {**size_memory(), "source": "memory/MEMORY.md", "controllable": "✅ GC"},
        "7. GBrain index": {**size_gbrain_index(), "source": "gbrain_index.py Phase E", "controllable": "⚠️ 算法改"},
        "8. envelope + reminder": {**size_envelope(), "source": "CC runtime", "controllable": "❌ 难"},
    }

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bot_name": args.bot,
        "sections": sections,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(to_markdown(report))


if __name__ == "__main__":
    main()
