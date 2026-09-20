"""看门狗报警之前，先分清是「worker 挂了」还是「活干完了没闭环」。

## 为什么要分

全局看门狗的 fire 条件是「有 active task ＋ 全 fleet inbox 静默」。这两个条件
**同时被两种完全不同的情况满足**：

  A. worker 真的挂了 —— 活没干完，需要人去救
  B. 活早干完了，只是**没人发那条 `done`** —— 什么都不用做，补一条就行

原来的文案两种都说「大概率有 worker 挂了」。tommy 2026-09-20 连着两次被这条
警报叫起来，两次都得手动跑一遍 registry ＋ 翻日志才能排除，两次都是 B。

> 它的原话：「这类假警报……跟真的挂掉长得一模一样。」

⚠️ **假警报的代价不是浪费一次诊断，是把真警报变得不可信。** 同一句
「大概率有 worker 挂了」重复出现而每次都是虚惊，下一次真挂的时候没人会当回事。
（同一条教训见 memory `feedback_watch-probe-degrades-to-chat`：
例行事件不许占 ERROR，下游机器会当真。）

## 判据

`registry/{bot}.last_seen` —— 每个 bot 自己在刷。这正是 tommy 每次手动查的
那个东西，把它挪到报警之前。

**不做更聪明的判断。** 比如「进程在不在」要 ssh、「有没有在干活」没有可靠信号。
心跳新鲜与否是唯一一个零成本、零歧义、而且本来就在维护的事实。
"""
from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["FleetVerdict", "classify_silence", "DEFAULT_STALE_AFTER_SEC"]

#: 心跳多久没刷算「不活」。
#:
#: 比看门狗的静默阈值（10 分钟）短，但要比心跳间隔宽裕得多 ——
#: 取 5 分钟：真挂了的 bot 一定超过它，而正常 bot 偶尔卡一下不会。
DEFAULT_STALE_AFTER_SEC = 300.0


@dataclass
class FleetVerdict:
    """这次静默到底是哪一种。"""

    #: `dangling` 活干完没闭环 / `dead` 有 worker 不动了 / `unknown` 查不到心跳
    kind: str
    alive: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def needs_rescue(self) -> bool:
        """要不要真的去救。`dangling` 不用 —— 补一条 done 就完了。"""
        return self.kind != "dangling"


def classify_silence(last_seen_ago: dict[str, float | None],
                     stale_after: float = DEFAULT_STALE_AFTER_SEC) -> FleetVerdict:
    """按心跳新鲜度给这次静默定性。

    `last_seen_ago`：worker 名 → 心跳距今多少秒。`None` 表示 registry 里查不到
    （新 bot、或者 registry 被清过）。

    ⚠️ **查不到不算「挂了」。** 查不到就是查不到 —— 报成挂了是在编，
    而这个报警的全部价值就在于它说的是真的。查不到单独一档，文案里如实写。
    """
    alive, stale, unknown = [], [], []
    for name in sorted(last_seen_ago):
        ago = last_seen_ago[name]
        if ago is None:
            unknown.append(name)
        elif ago <= stale_after:
            alive.append(name)
        else:
            stale.append(name)

    if stale:
        kind = "dead"
    elif unknown:
        # 一个都不 stale，但有查不到的 —— 不敢说挂了，也不敢说没事。
        kind = "unknown"
    else:
        kind = "dangling"
    return FleetVerdict(kind=kind, alive=alive, stale=stale, unknown=unknown)


def headline(verdict: FleetVerdict, task_count: int, silent_min: float) -> str:
    """报警的第一行。**两种情况必须一眼分得开** —— 这是整件事的重点。"""
    if verdict.kind == "dangling":
        return (f"# 📌 {task_count} 个任务没收到 done（worker 都活着）")
    if verdict.kind == "unknown":
        return f"# ⚠️ Fleet 静默 {silent_min:.0f} 分钟（有 worker 查不到心跳）"
    return f"# ⚠️ Fleet 静默 {silent_min:.0f} 分钟 —— 有 worker 不动了"


def advice(verdict: FleetVerdict, task_ids: list[str]) -> list[str]:
    """给出下一步。`dangling` 那支直接把命令写出来，别让人再去翻文档。"""
    if verdict.kind == "dangling":
        out = [
            "这些 worker 的心跳都是新鲜的，**没有挂**。活大概率已经干完了，"
            "只是没人发那条 `done`。",
            "",
            "先确认活是不是真干完了（翻一下它最后一次回报）。是的话补一条收尾：",
            "",
            "```bash",
        ]
        for tid in task_ids:
            out.append(
                f'python3 ~/CloseCrab/scripts/inbox-send.py <主bot> "<最终结论>" \\\n'
                f'    --task-id {tid} --phase done --phase-label "完成"'
            )
        out.append("```")
        out.append("")
        out.append(
            "⚠️ 要是活**没**干完，那才是真问题 —— 那就按下面一条查。"
        )
        return out

    lines = []
    if verdict.stale:
        lines.append(
            "心跳不新鲜的：" + ", ".join(f"`{w}`" for w in verdict.stale)
            + " —— **先查这几个**。"
        )
    if verdict.unknown:
        lines.append(
            "registry 里查不到的：" + ", ".join(f"`{w}`" for w in verdict.unknown)
            + "（查不到不等于挂了，可能是没注册过）"
        )
    if verdict.alive:
        lines.append("心跳正常的：" + ", ".join(f"`{w}`" for w in verdict.alive))
    lines.extend([
        "",
        "查进程 `ps aux`、查日志 `~/.claude/closecrab/<bot>/bot.log`，"
        "远程的 ssh 过去。",
    ])
    return lines
