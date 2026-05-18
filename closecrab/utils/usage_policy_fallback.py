"""Fallback handler for Anthropic Vertex "Usage Policy" refusals.

When the primary worker (Opus 4.7 via Claude CLI) returns an
``API Error: ...Usage Policy`` text — typically a probabilistic
RLHF refusal on a long session whose accumulated context tripped a
guardrail — we transparently re-issue the user's prompt against
Opus 4.6 using the Anthropic Vertex SDK **directly**, bypassing
Claude CLI. The 4.6 response replaces the broken reply before it
reaches the user.

Design choices:
  * SDK direct, not a new ClaudeCodeWorker — restarting Claude CLI
    takes 30s+, drops session context, and forces a global model
    switch. SDK fallback is ~5s, leaves the primary worker untouched
    so the next turn goes back to 4.7.
  * Trade-off: the fallback reply has **no tool calls** (it's a
    one-shot SDK message). For "讲解长一点" style refusals this is
    fine; for tool-heavy prompts the fallback degrades to text-only.
  * Failure mode: SDK call also fails / times out → return original
    reply unchanged so the user at least sees the API Error and can
    retry manually. Never block the BotCore finalize path.

Cost: claude-opus-4-6 is ~$15/M in + $75/M out via Vertex. Per
fallback call (a few KB in, ~1K out) ≈ $0.10. Triggers are rare
(<1% of turns historically); annual cost negligible.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

log = logging.getLogger("closecrab.usage_policy_fallback")

_PROJECT = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "gpu-launchpad-playground")
_REGION = os.environ.get("CLOUD_ML_REGION", "global")
_FALLBACK_MODEL = os.environ.get(
    "USAGE_POLICY_FALLBACK_MODEL", "claude-opus-4-6@20251001"
)
_TIMEOUT_S = float(os.environ.get("USAGE_POLICY_FALLBACK_TIMEOUT_S", "60"))
_MAX_TOKENS = int(os.environ.get("USAGE_POLICY_FALLBACK_MAX_TOKENS", "4096"))

# Match Anthropic's standard Usage Policy refusal text. The error format
# is stable across both Claude CLI and direct API: the phrase
# "API Error" followed somewhere by "Usage Policy" within a few hundred
# chars. We keep the pattern broad (case-insensitive, DOTALL) so it also
# catches variants like "API Error (4xx): ... violates our Usage Policy".
_USAGE_POLICY_RE = re.compile(
    r"API\s+Error.{0,200}Usage\s+Policy", re.IGNORECASE | re.DOTALL
)

_FALLBACK_PREFIX = "🔁 [4.7 触发内容审核 → Opus 4.6 fallback]\n\n"

# Lazy module-level client. AsyncAnthropicVertex reuses HTTP connections
# across calls — important since fallback may fire multiple times in a
# session.
_client = None
_client_lock = asyncio.Lock()


def is_usage_policy_refusal(text: str) -> bool:
    """Cheap regex check — call on every BotCore reply, must not allocate."""
    if not text:
        return False
    return _USAGE_POLICY_RE.search(text) is not None


async def _get_client():
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            try:
                from anthropic import AsyncAnthropicVertex
            except ImportError:
                log.warning(
                    "anthropic SDK missing; usage_policy_fallback disabled"
                )
                return None
            try:
                _client = AsyncAnthropicVertex(
                    project_id=_PROJECT, region=_REGION
                )
            except Exception as e:
                log.warning("AsyncAnthropicVertex init failed: %s", e)
                return None
    return _client


async def try_fallback(
    user_text: str,
    system_prompt: Optional[str] = None,
) -> Optional[str]:
    """Issue ``user_text`` to Opus 4.6 via Vertex SDK. Return the reply
    text on success, ``None`` on any failure (caller falls back to the
    original error message).

    ``system_prompt`` is the bot's persona/style prompt — passed through
    so the fallback reply matches the bot's voice. Truncated to 8KB to
    avoid token waste on the fallback path; the long S1-recall block
    is intentionally NOT included because recall might be exactly what
    tripped the original guardrail.
    """
    if not (user_text or "").strip():
        return None
    client = await _get_client()
    if client is None:
        return None

    sys_msg = (system_prompt or "")[:8000]
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=_FALLBACK_MODEL,
                max_tokens=_MAX_TOKENS,
                system=sys_msg if sys_msg else None,
                messages=[{"role": "user", "content": user_text[:16000]}],
            ),
            timeout=_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        log.warning(
            "usage_policy_fallback timed out after %.0fs (model=%s)",
            _TIMEOUT_S, _FALLBACK_MODEL,
        )
        return None
    except Exception as e:
        log.warning(
            "usage_policy_fallback call failed (model=%s): %s",
            _FALLBACK_MODEL, e,
        )
        return None

    text_out = ""
    for block in (resp.content or []):
        if getattr(block, "type", None) == "text":
            text_out += getattr(block, "text", "") or ""
    text_out = text_out.strip()
    if not text_out:
        log.warning("usage_policy_fallback returned empty text")
        return None
    log.info(
        "usage_policy_fallback succeeded: %d chars from %s",
        len(text_out), _FALLBACK_MODEL,
    )
    return _FALLBACK_PREFIX + text_out


__all__ = ["is_usage_policy_refusal", "try_fallback"]
