"""
context.session_updater
=======================

Lightweight SessionSummary refresher.

Called by the graph's conditional summary_node only when the event counter
in SessionMemory reaches the trigger threshold (default: 5 new events).

Uses claude-haiku (fast + cheap, ~512 tokens) — this is a bookkeeping
call, not a creative one.  The output is parsed directly into a
SessionSummary Pydantic model.  On any failure the existing summary is
returned unchanged so the pipeline stays resilient.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from anthropic import AsyncAnthropic

from config import get_settings
from context.models import CanvasEvent, SessionSummary

logger = logging.getLogger(__name__)

# Haiku for speed + cost.  UPGRADE: make configurable via Settings.
_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 512

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=get_settings().anthropic_api_key)
    return _client


async def update_session_summary(
    current_summary: SessionSummary,
    recent_events: list[CanvasEvent],
    recent_actions: list[str],
) -> SessionSummary:
    """Refresh *current_summary* using recent events and agent action history.

    Parameters
    ----------
    current_summary:
        The SessionSummary currently stored in SessionMemory.
    recent_events:
        Last N CanvasEvents from the EventLog.
    recent_actions:
        Last N plain-text action summaries from SessionMemory.

    Returns
    -------
    Updated SessionSummary, or *current_summary* unchanged if the call fails.
    """
    events_text = "\n".join(
        f"  [{e.event_type.value}] {e.summary}"
        for e in recent_events[-20:]
    ) or "  (none)"

    actions_text = "\n".join(
        f"  {a}" for a in recent_actions[-5:]
    ) or "  (none)"

    current_json = current_summary.model_dump_json(indent=2)

    prompt = f"""\
You are tracking context for a collaborative visual brainstorming canvas.

Current session summary:
{current_json}

Recent canvas events:
{events_text}

Recent agent actions:
{actions_text}

Update the session summary to reflect the current board state.
Output ONLY a valid JSON object — no markdown fences, no explanation.
Match this exact schema (all fields required):
{{
  "board_goal": <string or null>,
  "active_topics": [<string>, ...],
  "current_clusters": [{{"label": <string>, "object_ids": [<string>, ...]}}],
  "open_questions": [<string>, ...],
  "recent_decisions": [<string>, ...]
}}"""

    try:
        response = await _get_client().messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw: str = response.content[0].text.strip()

        # Strip optional markdown code fences Claude sometimes adds.
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(
                l for l in lines
                if not l.startswith("```")
            ).strip()

        parsed: dict[str, Any] = json.loads(raw)
        updated = SessionSummary.model_validate(parsed)
        logger.debug(
            "session summary refreshed: goal=%r topics=%s",
            updated.board_goal,
            updated.active_topics,
        )
        return updated

    except Exception as exc:
        logger.warning("session_summary update failed (%s) — keeping existing", exc)
        return current_summary
