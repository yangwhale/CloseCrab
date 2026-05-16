"""Text chunking — 长文本按边界切分发送。

镜像 OpenClaw 的 chunkTextByBreakResolver / chunkTextForOutbound
(src/shared/text-chunking.ts + src/plugin-sdk/text-chunking.ts)。

典型场景：飞书 / Discord 消息有长度上限（飞书 text 4000 字符更稳，Discord 2000），
模型生成长文必须切片。简单按 limit 硬切会切断单词、列表项、代码块；
本模块优先在换行处切，其次空格，保持 markdown 结构尽量完整。

设计：
- chunk_text_by_break_resolver: 通用滑动窗口切分，断点策略由 resolver 注入
- chunk_text_for_outbound: outbound 默认策略（markdown 模式：先换行后空格）
- 每个 chunk 自动 trimEnd / trimStart，避免边界粘连空白
"""

from __future__ import annotations

from typing import Callable


def chunk_text_by_break_resolver(
    text: str,
    limit: int,
    resolve_break_index: Callable[[str], int],
) -> list[str]:
    """通用滑动窗口切分。

    resolve_break_index(window) → 在 [0, limit) 内返回断点位置（-1 / 0 / 越界
    都视为找不到，强制按 limit 硬切）。
    """
    if not text:
        return []
    if limit <= 0 or len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        candidate_break = resolve_break_index(window)
        break_idx = (
            candidate_break
            if isinstance(candidate_break, int) and 0 < candidate_break <= limit
            else limit
        )
        raw_chunk = remaining[:break_idx]
        chunk = raw_chunk.rstrip()
        if chunk:
            chunks.append(chunk)
        broke_on_separator = (
            break_idx < len(remaining) and remaining[break_idx].isspace()
        )
        next_start = min(len(remaining), break_idx + (1 if broke_on_separator else 0))
        remaining = remaining[next_start:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


def chunk_text_for_outbound(text: str, limit: int) -> list[str]:
    """优先在换行处切，其次空格（markdown 友好）。"""

    def resolver(window: str) -> int:
        last_newline = window.rfind("\n")
        last_space = window.rfind(" ")
        return last_newline if last_newline > 0 else last_space

    return chunk_text_by_break_resolver(text, limit, resolver)
