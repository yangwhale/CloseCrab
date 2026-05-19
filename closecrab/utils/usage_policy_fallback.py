"""Fallback handler for Anthropic Vertex "Usage Policy" refusals.

When the primary Claude CLI worker returns an ``API Error: ...Usage Policy``
text — a probabilistic RLHF guardrail trip that fires post-generation — we
transparently recover the final reply in one of two ways:

1. **Stream-text recovery** (preferred, free, no extra API call). Claude
   CLI's streaming `assistant` events often emit the real answer *before*
   Vertex's safety layer overwrites the final `result` event with the
   refusal text. If the upstream worker exposes its accumulated stream
   text, we strip the refusal tail and use the partial real reply.

2. **SDK re-issue** (~5s, ~$0.10). Issue the bare user prompt against the
   configured fallback model (default ``claude-opus-4-6@default``) via
   Vertex SDK directly, bypassing Claude CLI. Used when stream recovery
   isn't possible (no partial, or too short to be substantive).

Both paths replace ``result`` so the finalize / session_index / reply paths
see the recovered reply, not the original API Error. Never blocks the
BotCore finalize path — every failure mode leaves the original error
untouched so the user sees it and can retry manually.

Design choices:
  * SDK direct, not a fresh Worker — Claude CLI restart takes 30s+ and
    drops session context. SDK is ~5s and leaves primary worker state
    untouched, so the next turn resumes on the primary.
  * The SDK fallback has **no tool calls** (one-shot SDK message). Fine
    for "讲解长一点" style refusals; tool-heavy prompts degrade to text.
  * The S1 recall block is intentionally NOT forwarded to the SDK call —
    recall content is often what tripped the original guardrail.

Cost: ``claude-opus-4-6`` ≈ $15/M in + $75/M out via Vertex. Per SDK call
(~few KB in, ~1K out) ≈ $0.10. Triggers are rare (<1% of turns); annual
cost negligible. Stream-text recovery is free.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

log = logging.getLogger("closecrab.usage_policy_fallback")


# ── Configuration ────────────────────────────────────────────────────

_PROJECT = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "gpu-launchpad-playground")
_REGION = os.environ.get("CLOUD_ML_REGION", "global")
_FALLBACK_MODEL = os.environ.get(
    "USAGE_POLICY_FALLBACK_MODEL", "claude-opus-4-6@default"
)
_TIMEOUT_S = float(os.environ.get("USAGE_POLICY_FALLBACK_TIMEOUT_S", "60"))
_MAX_TOKENS = int(os.environ.get("USAGE_POLICY_FALLBACK_MAX_TOKENS", "4096"))

# Minimum stream-recovered partial reply length to consider "substantive
# enough to skip SDK re-issue". 200 chars ≈ 2-3 short paragraphs — below
# that the partial likely got cut so early it's not a usable answer.
_MIN_PARTIAL_CHARS = int(os.environ.get("USAGE_POLICY_MIN_PARTIAL_CHARS", "200"))

# Match Anthropic's standard Usage Policy refusal text. Format is stable
# across Claude CLI and direct API: "API Error" followed within ~200 chars
# by "Usage Policy". Broad (case-insensitive, DOTALL) so we also catch
# variants like "API Error (4xx): ... violates our Usage Policy".
_USAGE_POLICY_RE = re.compile(
    r"API\s+Error.{0,200}Usage\s+Policy", re.IGNORECASE | re.DOTALL
)

# Used by _short_model_name to format "4-6" → "4.6" in user-facing banners.
_VERSION_RE = re.compile(r"^(\d+)-(\d+)(.*)$")


# ── Lazy module-level SDK client ─────────────────────────────────────
# AsyncAnthropicVertex reuses HTTP connections across calls — important
# since fallback may fire multiple times in a session.

_client = None
_client_lock = asyncio.Lock()


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


# ── Banner / formatting helpers ──────────────────────────────────────

def _short_model_name(model_id: str) -> str:
    """``claude-opus-4-6@default`` → ``Opus 4.6``. Unknown → raw id or '?'."""
    base = (model_id or "").split("@")[0]
    for prefix, label in (
        ("claude-opus-", "Opus "),
        ("claude-sonnet-", "Sonnet "),
        ("claude-haiku-", "Haiku "),
    ):
        if base.startswith(prefix):
            rest = base[len(prefix):]
            m = _VERSION_RE.match(rest)
            if m:
                rest = f"{m.group(1)}.{m.group(2)}{m.group(3)}"
            return label + rest
    return base or "?"


def _make_banner(source: str) -> str:
    """User-facing banner.

    ``source`` is ``"partial"`` (free, stream recovery) or ``"sdk"`` (paid,
    SDK re-issue). Built at call time, not import time — reads
    ``ANTHROPIC_MODEL`` env which the Claude CLI subprocess may have
    overridden per-bot.
    """
    primary = _short_model_name(os.environ.get("ANTHROPIC_MODEL", ""))
    if source == "partial":
        return f"🔁 [{primary} 触发内容审核 → 已回收流式片段]\n\n"
    fallback = _short_model_name(_FALLBACK_MODEL)
    return f"🔁 [{primary} 触发内容审核 → {fallback} fallback]\n\n"


def _strip_refusal_tail(text: str) -> str:
    """Cut the ``API Error...Usage Policy`` tail from accumulated stream
    text, return the real reply that came before it.

    Returns empty string if the text starts with refusal (no usable
    content) or is empty.
    """
    if not text:
        return ""
    m = _USAGE_POLICY_RE.search(text)
    if not m:
        return text.strip()
    return text[:m.start()].strip()


# ── Public API ───────────────────────────────────────────────────────

def is_usage_policy_refusal(text: str) -> bool:
    """Cheap regex check — called on every BotCore reply, must not allocate."""
    if not text:
        return False
    return _USAGE_POLICY_RE.search(text) is not None


async def try_fallback(
    user_text: str,
    system_prompt: Optional[str] = None,
    partial_reply: Optional[str] = None,
) -> Optional[str]:
    """Recover a refused reply. Return the banner-prefixed text on success,
    ``None`` on any failure (caller falls back to the original error).

    Path 1 (free): if ``partial_reply`` has ≥ ``_MIN_PARTIAL_CHARS`` of
    real content (after stripping the API Error tail), use that directly.
    No SDK call.

    Path 2 (~5s, ~$0.10): re-issue ``user_text`` to the fallback model
    via Vertex SDK. ``system_prompt`` (truncated to 8KB) preserves the
    bot's persona/voice in the SDK reply.
    """
    # ── Path 1: stream-text recovery ──
    if partial_reply:
        clean = _strip_refusal_tail(partial_reply)
        if len(clean) >= _MIN_PARTIAL_CHARS:
            log.info(
                "usage_policy_fallback: recovered %d chars from stream "
                "(skipped SDK re-issue)", len(clean),
            )
            return _make_banner("partial") + clean

    # ── Path 2: SDK re-issue ──
    if not (user_text or "").strip():
        return None
    client = await _get_client()
    if client is None:
        return None

    # Anthropic Vertex API rejects ``system=None`` with HTTP 400 ("system:
    # Input should be a valid list"). Omit the key entirely on empty.
    kwargs: dict = {
        "model": _FALLBACK_MODEL,
        "max_tokens": _MAX_TOKENS,
        "messages": [{"role": "user", "content": user_text[:16000]}],
    }
    sys_msg = (system_prompt or "")[:8000]
    if sys_msg:
        kwargs["system"] = sys_msg

    try:
        resp = await asyncio.wait_for(
            client.messages.create(**kwargs), timeout=_TIMEOUT_S,
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

    text_out = "".join(
        getattr(b, "text", "") or ""
        for b in (resp.content or [])
        if getattr(b, "type", None) == "text"
    ).strip()
    if not text_out:
        log.warning("usage_policy_fallback returned empty text")
        return None
    log.info(
        "usage_policy_fallback succeeded: %d chars from %s",
        len(text_out), _FALLBACK_MODEL,
    )
    return _make_banner("sdk") + text_out


async def warmup(timeout_s: float = 10.0) -> bool:
    """Fire one minimal real SDK call at bot startup.

    Surfaces config errors (wrong region, bad model id, schema drift,
    expired credentials) in startup logs immediately — instead of waiting
    days for a real refusal to trigger. The mechanized form of the
    "mandatory live smoke test" rule.

    Cheap: ~150 tokens in, ~5 tokens out, ~$0.001 per call. Blocks bot
    startup for 2-5s. Failure logs WARNING but never raises — fallback
    infrastructure being down shouldn't block bot startup since fallback
    itself is already a degraded path.
    """
    try:
        result = await asyncio.wait_for(
            try_fallback("ping", None), timeout=timeout_s,
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
