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
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("closecrab.gbrain_index")

DEFAULT_BASE_URL = "http://localhost:3131"
DEFAULT_CREDS_PATH = "~/.gbrain/cc-tw-claude-creds.json"
DEFAULT_TIMEOUT = 5.0
DEFAULT_LIST_LIMIT = 30
DEFAULT_SALIENCE_DAYS = 14
# Slugs starting with these prefixes are noise (bot-generated rollups / scratch),
# excluded from the index so they don't dominate the human-written knowledge.
DEFAULT_EXCLUDE_PREFIXES = ("analytics/", "testing/", "tmp/", "scratch/")
# Hard cap on final markdown size — protects bot startup from a runaway brain
# (rogue ingestion, huge titles, etc.). At ~4 chars/token this is ~5K tokens,
# enough headroom for 30 recent + 30 salient with full-ish titles. Truncating
# is safer than letting system_prompt balloon to 50KB.
DEFAULT_MAX_OUTPUT_CHARS = 20000
# Same-process cache: bot boot only fires fetch_gbrain_index() once, but cron
# / debug tools may re-call. 5-minute window matches Anthropic prompt-cache TTL.
_CACHE_TTL = 300.0
_cache: dict[tuple, tuple[float, str | None]] = {}


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


def _sanitize_for_md(s: str, max_len: int = 200) -> str:
    """Sanitize a string for safe inclusion in a markdown system-prompt fragment.

    Prevents prompt-injection / formatting-break from page slugs or titles that
    contain backticks (would break our `code` fences), newlines (would let an
    attacker inject "IGNORE ALL PREVIOUS INSTRUCTIONS"), or HTML/control chars.
    GBrain put_page validation is the first line of defense, but we never
    trust upstream data when it lands in a system prompt.
    """
    if not s:
        return ""
    # Strip control chars + newlines first (defangs jailbreak attempts).
    cleaned = "".join(ch for ch in s if ch == " " or (ch.isprintable() and ch not in "\r\n\t"))
    # Escape backticks so they can't break out of our `code` wrap.
    cleaned = cleaned.replace("`", "'")
    # Bound length to keep one row bounded even if upstream goes crazy.
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "…"
    return cleaned


def _is_noise(slug: str, exclude_prefixes: tuple[str, ...]) -> bool:
    """Filter out bot-generated rollup/scratch pages from the index.

    Why: auto-generated pages (analytics/, testing/) accumulate fast and would
    crowd out human-written knowledge from the top-30 list.
    """
    return any(slug.startswith(p) for p in exclude_prefixes)


def _is_auto_derived_title(slug: str, title: str) -> bool:
    """True if title looks auto-generated from slug (kebab/underscore → Title Case).

    e.g. slug='feedback_openclaw-gateway-hot-reload',
         title='Feedback Openclaw Gateway Hot Reload' → True.

    Saves both tokens and visual clutter — when title duplicates slug, we skip it.
    """
    if not slug or not title:
        return False
    normalized = slug.replace("/", " ").replace("_", " ").replace("-", " ")
    derived = " ".join(w.capitalize() for w in normalized.split())
    return derived == title.strip()


def _smart_title(page: dict[str, Any]) -> str:
    """Prefer human-written description; fall back to title; empty string if
    title is just auto-derived from slug (caller will skip the field)."""
    desc = (page.get("description") or "").strip()
    if desc and len(desc) <= 100:
        return desc
    slug = page.get("slug", "")
    title = (page.get("title") or "").strip()
    if not title:
        return ""
    if _is_auto_derived_title(slug, title):
        return ""  # Skip — slug already says it.
    return title


def _format_markdown(
    recent: list[dict[str, Any]],
    salient: list[dict[str, Any]],
    list_limit: int,
    salience_days: int,
    exclude_prefixes: tuple[str, ...] = DEFAULT_EXCLUDE_PREFIXES,
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
        f"### 最近活跃页面（top {list_limit}, by updated_at, 已过滤 noise）",
        "",
    ]

    recent_clean = [p for p in recent if not _is_noise(p.get("slug", ""), exclude_prefixes)]
    if recent_clean:
        for p in recent_clean[:list_limit]:
            slug = _sanitize_for_md(p.get("slug", ""), max_len=80)
            ptype = _sanitize_for_md(p.get("type", "?"), max_len=20)
            title = _sanitize_for_md(_smart_title(p), max_len=100)
            updated = _sanitize_for_md((p.get("updated_at") or "")[:10], max_len=10)
            tail = f" — {title}" if title else ""
            lines.append(f"- `{slug}` ({ptype}){tail} _{updated}_")
    else:
        lines.append("- _(空)_")

    lines += [
        "",
        f"### 近 {salience_days} 天显著（salient）页面",
        "",
    ]

    # Salient often overlaps heavily with recent (salience degrades to recency
    # when no take/emotional signals exist). Dedup to surface genuinely-novel
    # salient pages — the ones the LLM might miss from the recent list alone.
    shown_slugs = {p.get("slug", "") for p in recent_clean[:list_limit]}
    salient_clean = [
        p for p in salient
        if not _is_noise(p.get("slug", ""), exclude_prefixes)
        and p.get("slug", "") not in shown_slugs
    ]
    if salient_clean:
        for p in salient_clean[:list_limit]:
            slug = _sanitize_for_md(p.get("slug", ""), max_len=80)
            score = p.get("score", 0)
            title = _sanitize_for_md(_smart_title(p), max_len=100)
            tail = f" — {title}" if title else ""
            lines.append(f"- `{slug}`{tail} _(salience={score:.2f})_")
    else:
        lines.append("- _(与最近活跃高度重叠，已合并)_")

    return "\n".join(lines)


async def fetch_gbrain_index(
    creds_path: str | Path = DEFAULT_CREDS_PATH,
    base_url: str = DEFAULT_BASE_URL,
    *,
    list_limit: int = DEFAULT_LIST_LIMIT,
    salience_days: int = DEFAULT_SALIENCE_DAYS,
    timeout: float = DEFAULT_TIMEOUT,
    exclude_prefixes: tuple[str, ...] = DEFAULT_EXCLUDE_PREFIXES,
    use_cache: bool = True,
) -> str | None:
    """Fetch + format GBrain index. Returns markdown string, or None on any failure.

    Designed to be called once at bot boot before BotCore construction.
    Total wall-time budget is ~3× timeout (one /token + two /mcp calls).

    Cache: same-process 5min memo keyed on (base_url, list_limit, salience_days,
    exclude_prefixes). Pass ``use_cache=False`` to force refresh.
    """
    cache_key = (str(base_url), int(list_limit), int(salience_days), tuple(exclude_prefixes))
    if use_cache:
        entry = _cache.get(cache_key)
        if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
            log.debug(f"GBrain index: cache hit (age={time.monotonic() - entry[0]:.1f}s)")
            return entry[1]

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

        md = _format_markdown(
            recent or [], salient or [], list_limit, salience_days, exclude_prefixes,
        )
        # Hard cap on output: protect against runaway page counts / huge titles.
        if len(md) > DEFAULT_MAX_OUTPUT_CHARS:
            log.warning(
                f"GBrain index: output {len(md)} chars exceeds cap "
                f"{DEFAULT_MAX_OUTPUT_CHARS}, truncating"
            )
            md = md[:DEFAULT_MAX_OUTPUT_CHARS] + "\n\n_(已截断: 上限 20K chars)_"
        log.info(
            f"GBrain index: fetched {len(recent)} recent + {len(salient)} salient "
            f"= {len(md)} chars"
        )
        if use_cache:
            _cache[cache_key] = (time.monotonic(), md)
        return md
    except (httpx.HTTPError, asyncio.TimeoutError, RuntimeError) as e:
        log.warning(f"GBrain index: fetch failed ({type(e).__name__}: {e}), skipping")
        return None
