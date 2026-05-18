"""S1.2: LLM-based information density scoring for session recall.

Augments the regex filter (`_is_substantive`) with a continuous LLM-rated
score 0..1 stored as ``messages.info_density``. Recall ranking multiplies
this in: a 20-char "已 push abc123" (regex-rejected by length<80) can still
get density 0.4 and survive, while a 300-char car-wheel-talk reply gets
0.2 and sinks despite passing the regex filter.

Failure mode: caller treats ``None`` as "neutral 0.5" — we never block
turn finalize on scorer failure.

Cost rough order: haiku-4.5 at ~$0.80/M in + $4/M out, single call
≈ $0.00015. 100 turns/day per bot × 8 bots × 30 days = ~$3.6/month.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Optional

log = logging.getLogger("closecrab.info_scorer")

_PROJECT = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "gpu-launchpad-playground")
_REGION = os.environ.get("CLOUD_ML_REGION", "global")
_MODEL = os.environ.get("INFO_SCORER_MODEL", "claude-haiku-4-5@20251001")
_TIMEOUT_S = float(os.environ.get("INFO_SCORER_TIMEOUT_S", "20"))
_MAX_CHARS = 2000  # truncate inputs so prompt stays small

# Lazy module-level client. AsyncAnthropicVertex re-uses HTTP connections.
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
                log.warning("anthropic SDK missing; info_scorer disabled")
                return None
            try:
                _client = AsyncAnthropicVertex(project_id=_PROJECT, region=_REGION)
            except Exception as e:
                log.warning("AsyncAnthropicVertex init failed: %s", e)
                return None
    return _client


_PROMPT = """\
You are scoring how much information a conversation turn carries, used to \
rank chat history rows for automatic context recall. Score from 0.0 to 1.0:

0.0–0.2  pure greetings / acknowledgments / API errors / status pings
         (e.g. "hi", "好的", "嗯嗯", "API Error: ...", "在的有啥事")
0.3–0.5  short but informative (e.g. "已 push abc123", "30 step 跑完",
         "Claude Code 版本 2.1.88", "好，我选 B")
0.6–0.8  technical answer / discussion / one main idea
0.9–1.0  dense technical content (architecture, debugging, design tradeoffs)

You will be given a user message and the assistant's reply. Score each \
independently. Reply with ONLY a JSON object on one line, no markdown:

{"user": 0.X, "assistant": 0.Y}

---
USER:
%(user)s

---
ASSISTANT:
%(assistant)s
"""


_JSON_RE = re.compile(r"\{[^{}]*\}")


def _parse_response(text: str) -> tuple[Optional[float], Optional[float]]:
    """Extract user/assistant density from the model's JSON reply."""
    if not text:
        return None, None
    # Sometimes the model adds prose around the JSON; grab the first {...}
    m = _JSON_RE.search(text)
    if not m:
        return None, None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, None

    def _clamp(v) -> Optional[float]:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f != f:  # NaN
            return None
        return max(0.0, min(1.0, f))

    return _clamp(data.get("user")), _clamp(data.get("assistant"))


async def score_turn(
    user_text: str, assistant_text: str
) -> tuple[Optional[float], Optional[float]]:
    """Score a (user, assistant) pair with haiku. Returns (None, None) on failure.

    Single LLM call evaluates both messages together so we save one RTT.
    Empty input on either side returns None for that side (caller writes NULL).
    """
    if not (user_text or "").strip() and not (assistant_text or "").strip():
        return None, None
    client = await _get_client()
    if client is None:
        return None, None

    prompt = _PROMPT % {
        "user": (user_text or "(empty)")[:_MAX_CHARS],
        "assistant": (assistant_text or "(empty)")[:_MAX_CHARS],
    }
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=_MODEL,
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        log.debug("info_scorer timed out after %.0fs", _TIMEOUT_S)
        return None, None
    except Exception as e:
        log.debug("info_scorer call failed: %s", e)
        return None, None

    # Pull the text from the first text block
    text_out = ""
    for block in (resp.content or []):
        bt = getattr(block, "type", None)
        if bt == "text":
            text_out += getattr(block, "text", "") or ""
    user_s, asst_s = _parse_response(text_out)
    return (user_s if user_text else None,
            asst_s if assistant_text else None)


__all__ = ["score_turn"]
