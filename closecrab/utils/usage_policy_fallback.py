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
    "USAGE_POLICY_FALLBACK_MODEL", "claude-opus-4-6@default"
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

_VERSION_RE = re.compile(r"^(\d+)-(\d+)(.*)$")


def _short_model_name(model_id: str) -> str:
    """``claude-opus-4-6@default`` → ``Opus 4.6``. Unknown → original id."""
    base = (model_id or "").split("@")[0]
    for prefix, label in (
        ("claude-opus-", "Opus "),
        ("claude-sonnet-", "Sonnet "),
        ("claude-haiku-", "Haiku "),
    ):
        if base.startswith(prefix):
            rest = base[len(prefix):]
            # "4-6" → "4.6", "4-5-20251001" → "4.5-20251001"
            m = _VERSION_RE.match(rest)
            if m:
                rest = f"{m.group(1)}.{m.group(2)}{m.group(3)}"
            return label + rest
    return base or "?"


def _make_fallback_prefix() -> str:
    """Build the user-facing banner at fallback time, NOT at import time.

    Reads ``ANTHROPIC_MODEL`` env (the Claude CLI primary model — may have
    been overridden per-bot in ``ClaudeCodeWorker._start_process``) so the
    banner reflects reality. The legacy hardcoded string assumed primary
    was always Opus 4.7 and got stale once jarvis switched to 4.6.
    """
    primary = _short_model_name(os.environ.get("ANTHROPIC_MODEL", ""))
    fallback = _short_model_name(_FALLBACK_MODEL)
    return f"🔁 [{primary} 触发内容审核 → {fallback} fallback]\n\n"

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
    # Anthropic Vertex API rejects ``system=None`` with HTTP 400
    # ("system: Input should be a valid list"). The kwarg must be either
    # omitted entirely or a non-empty string/list. Build kwargs
    # conditionally so an empty / None system_prompt drops the key.
    kwargs: dict = {
        "model": _FALLBACK_MODEL,
        "max_tokens": _MAX_TOKENS,
        "messages": [{"role": "user", "content": user_text[:16000]}],
    }
    if sys_msg:
        kwargs["system"] = sys_msg
    try:
        resp = await asyncio.wait_for(
            client.messages.create(**kwargs),
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
    return _make_fallback_prefix() + text_out


async def warmup(timeout_s: float = 10.0) -> bool:
    """Fire one minimal real call against Vertex at bot startup.

    Purpose: surface configuration errors (wrong region, wrong model id,
    bad SDK schema, expired credentials) in the startup log instead of
    waiting for an actual Usage Policy refusal — which is a probabilistic
    RLHF event that may take days to trigger and is impossible to
    reproduce on demand. This is the mechanized version of the
    "mandatory live smoke test" rule.

    Cheap: ~150 tokens in, ~5 tokens out, ~$0.001 per call. Synchronously
    blocks bot startup for ~2-5s. Failure logs a WARNING but never raises
    — the bot continues to start even if fallback infrastructure is down,
    since fallback itself is a degraded path.
    """
    try:
        result = await asyncio.wait_for(
            try_fallback("ping", None),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        log.warning(
            "usage_policy_fallback warmup TIMED OUT after %.0fs — "
            "fallback may be misconfigured (model=%s region=%s)",
            timeout_s, _FALLBACK_MODEL, _REGION,
        )
        return False
    except Exception as e:
        log.warning(
            "usage_policy_fallback warmup FAILED (model=%s region=%s): %s",
            _FALLBACK_MODEL, _REGION, e,
        )
        return False
    if result:
        log.info(
            "usage_policy_fallback warmup OK (model=%s region=%s, %d chars)",
            _FALLBACK_MODEL, _REGION, len(result),
        )
        return True
    log.warning(
        "usage_policy_fallback warmup returned None — see prior log line "
        "for root cause (model=%s region=%s)",
        _FALLBACK_MODEL, _REGION,
    )
    return False


__all__ = ["is_usage_policy_refusal", "try_fallback", "warmup"]
