"""GBrain index fetcher — Phase E memory-bank always-in-context injection.

Fetches a compact GBrain index (recent pages + salient pages) at bot boot
and formats it as markdown suitable for system-prompt injection. Modeled on
Auto Memory's MEMORY.md pattern: give the LLM a stable directory it can scan
without an explicit query.

Failure mode is silent: any error (brain down, creds invalid, timeout,
malformed response) returns ``None``; caller skips injection.

GBrain MCP HTTP is stateless — no session handshake required, just
``POST /mcp`` with ``Authorization: Bearer <token>`` and a JSON-RPC body.
Response is SSE (one ``data: <json>`` line per message).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("closecrab.gbrain_index")

DEFAULT_BASE_URL = "http://localhost:3131"
DEFAULT_CREDS_PATH = "~/.gbrain/cc-tw-claude-creds.json"
DEFAULT_TIMEOUT = 5.0
DEFAULT_LIST_LIMIT = 30
DEFAULT_SALIENCE_DAYS = 14


def _parse_sse_payload(body: str) -> Any:
    """GBrain MCP wraps JSON-RPC responses in SSE. Extract the JSON-RPC result."""
    for line in body.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            if "error" in payload:
                raise RuntimeError(f"MCP error: {payload['error']}")
            return payload.get("result")
    raise RuntimeError(f"no data line in SSE body: {body[:200]!r}")


async def _get_token(client: httpx.AsyncClient, base_url: str, client_id: str, client_secret: str) -> str:
    resp = await client.post(
        f"{base_url}/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


async def _call_tool(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    name: str,
    arguments: dict[str, Any],
    request_id: int = 1,
) -> Any:
    resp = await client.post(
        f"{base_url}/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    resp.raise_for_status()
    result = _parse_sse_payload(resp.text)
    if not result or "content" not in result:
        raise RuntimeError(f"tool {name} returned no content: {result!r}")
    text = result["content"][0]["text"]
    return json.loads(text)


def _format_markdown(
    recent: list[dict[str, Any]],
    salient: list[dict[str, Any]],
    list_limit: int,
    salience_days: int,
) -> str:
    """Build a compact markdown index. Goal: ~150 lines so it sits in
    system prompt without dominating it."""
    lines: list[str] = [
        "## GBrain Memory Bank（持久化结构化记忆）",
        "",
        "**重要**：这是一个跨 bot/跨 session 共享的结构化记忆库，所有 bot 写入的页面都对你可见。",
        "下面是当前 brain 里 **最近活跃 + 显著（salient）的页面索引**——遇到相关话题时主动查询。",
        "",
        "**触发场景**：",
        '- 用户问「你之前 / 之前我们 / 上次」这类回顾性问题 → 用 `mcp__gbrain__get_page` 或 `query`',
        "- 用户提到索引里出现过的实体/项目名 → 直接 `get_page`",
        "- 你产出了有持久价值的分析/经验 → 用 `put_page` 写入（slug 用 kebab-case，记得加 frontmatter）",
        "- 想知道现在 brain 全貌 → 用 `list_pages`（不要每次都从这里看）",
        "",
        "**主要工具**：`get_page` / `query`（语义搜索） / `list_pages` / `put_page` / `get_recent_salience` / `find_experts` / `recall`",
        "",
        f"### 最近活跃页面（top {list_limit}, by updated_at）",
        "",
    ]

    if recent:
        for p in recent[:list_limit]:
            slug = p.get("slug", "")
            ptype = p.get("type", "?")
            title = p.get("title", "")
            updated = (p.get("updated_at") or "")[:10]
            lines.append(f"- `{slug}` ({ptype}) — {title} _{updated}_")
    else:
        lines.append("- _(空)_")

    lines += [
        "",
        f"### 近 {salience_days} 天显著（salient）页面",
        "",
    ]

    if salient:
        for p in salient[:list_limit]:
            slug = p.get("slug", "")
            score = p.get("score", 0)
            title = p.get("title", "")
            lines.append(f"- `{slug}` — {title} _(salience={score:.2f})_")
    else:
        lines.append("- _(空)_")

    return "\n".join(lines)


async def fetch_gbrain_index(
    creds_path: str | Path = DEFAULT_CREDS_PATH,
    base_url: str = DEFAULT_BASE_URL,
    *,
    list_limit: int = DEFAULT_LIST_LIMIT,
    salience_days: int = DEFAULT_SALIENCE_DAYS,
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """Fetch + format GBrain index. Returns markdown string, or None on any failure.

    Designed to be called once at bot boot before BotCore construction.
    Total wall-time budget is ~3× timeout (one /token + two /mcp calls).
    """
    creds_file = Path(str(creds_path)).expanduser()
    if not creds_file.exists():
        log.info(f"GBrain index: creds file not found ({creds_file}), skipping")
        return None

    try:
        creds = json.loads(creds_file.read_text())
        client_id = creds["client_id"]
        client_secret = creds["client_secret"]
    except (OSError, KeyError, json.JSONDecodeError) as e:
        log.warning(f"GBrain index: failed to read creds: {e}")
        return None

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            token = await _get_token(client, base_url, client_id, client_secret)
            recent_task = _call_tool(
                client, base_url, token,
                "list_pages",
                {"limit": list_limit, "sort": "updated_desc"},
                request_id=1,
            )
            salient_task = _call_tool(
                client, base_url, token,
                "get_recent_salience",
                {"days": salience_days, "limit": list_limit},
                request_id=2,
            )
            recent, salient = await asyncio.gather(
                recent_task, salient_task, return_exceptions=True,
            )

        if isinstance(recent, Exception):
            log.warning(f"GBrain index: list_pages failed: {recent}")
            recent = []
        if isinstance(salient, Exception):
            log.warning(f"GBrain index: get_recent_salience failed: {salient}")
            salient = []

        if not recent and not salient:
            log.info("GBrain index: both queries empty, skipping injection")
            return None

        md = _format_markdown(recent or [], salient or [], list_limit, salience_days)
        log.info(
            f"GBrain index: fetched {len(recent)} recent + {len(salient)} salient "
            f"= {len(md)} chars"
        )
        return md
    except (httpx.HTTPError, asyncio.TimeoutError, RuntimeError) as e:
        log.warning(f"GBrain index: fetch failed ({type(e).__name__}: {e}), skipping")
        return None
