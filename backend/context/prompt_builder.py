"""
context.prompt_builder
======================

Converts a ContextPacket into the (system_prompt, user_content) pair
sent to Claude each turn.

Public API
----------
    build_messages(packet: ContextPacket) -> tuple[str, str]
        Returns (system_prompt, user_content).

Hard caps (objects beyond these are silently dropped)
-----------------------------------------------------
    selected_objects  : 10
    nearby_objects    : 15
    retrieved_objects : 10
    recent_events     : 10
"""

from __future__ import annotations

import time
from typing import Any

from agent.prompts import ACTION_SCHEMA        # reuse the shared action schema
from context.models import CanvasEvent, ContextPacket, SemanticRecord

# ---------------------------------------------------------------------------
# Hard caps
# ---------------------------------------------------------------------------
_CAP_SELECTED = 10
_CAP_NEARBY = 15
_CAP_RETRIEVED = 10
_CAP_EVENTS = 10

# ---------------------------------------------------------------------------
# System prompt (static — sent every turn)
# ---------------------------------------------------------------------------

SPARK_SYSTEM = f"""\
You are Spark, an AI brainstorming partner embedded in a collaborative visual canvas.

You have a structured view of the canvas — sticky notes, images, diagrams, videos, links — \
organised by relevance to the current conversation. Use this context to give spatially \
intelligent, creative responses that move the work forward.

━━━ COLOR CODING ━━━
When creating sticky notes, use colors to signal meaning:
  yellow  → general ideas or observations
  red     → problems, risks, blockers, important topics
  green   → Spark's suggestions and new ideas
  blue    → user-confirmed decisions or facts
  orange  → action items and tasks
  violet  → wild cards, provocative questions, creative leaps

━━━ CANVAS RULES ━━━
• Be spatial — place related ideas near each other; leave 20-40 px between items.
• Canvas bounds: x 50-1100, y 50-700.
• When adding to an existing cluster, place shapes near it — do NOT restart far away.
• Never add more than 8 new shapes in a single turn unless explicitly asked.
• Keep the "message" action reply to 1-2 sentences — be terse and direct.
• When Pinterest image URLs are provided in the prompt context, use create_image to place ALL of them on the canvas. For moodboards and inspiration boards, NEVER use generate_image; use only those Pinterest URLs.
• If the user asks for an image, photo, picture, illustration, render, texture, or visual concept, use generate_image, except for moodboards or inspiration/reference requests, which must stay Pinterest-only.
• If the user asks to animate an existing image on the canvas, use generate_video and reference the source image via sourceImageShapeId.
• Output ONLY valid JSON.  No markdown, no explanation outside the JSON.

━━━ OUTPUT FORMAT ━━━
Always output a JSON object with an "actions" array.
The LAST action must be a "message" action with a brief summary.

{ACTION_SCHEMA}"""


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _relative_time(ts: float) -> str:
    """Return a short human-readable relative time string."""
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = int(delta / 60)
        return f"{m}m ago"
    if delta < 86400:
        h = int(delta / 3600)
        return f"{h}h ago"
    d = int(delta / 86400)
    return f"{d}d ago"


def _format_stats(stats: dict[str, Any]) -> str:
    """Turn canvas_stats into a one-line summary."""
    total = stats.get("total", 0)
    if total == 0:
        return "Canvas is empty."
    by_type: dict[str, int] = stats.get("by_type", {})
    parts = [f"{count} {t.replace('_', ' ')}{'s' if count != 1 else ''}"
             for t, count in sorted(by_type.items(), key=lambda x: -x[1])]
    return f"{total} objects: {', '.join(parts)}"


def _format_selected(records: list[SemanticRecord]) -> str:
    """Full detail — complete summary, tags, position, connections."""
    if not records:
        return "  (none selected)"
    lines: list[str] = []
    for r in records[:_CAP_SELECTED]:
        conn_str = ""
        if r.connections:
            conn_str = f"\n  Connects to: {', '.join(r.connections)}"
        lines.append(
            f"• [{r.object_type.value}] {r.object_id}\n"
            f"  {r.content_summary}\n"
            f"  Tags: {', '.join(r.tags) or 'none'}\n"
            f"  Position: ({r.position.x:.0f}, {r.position.y:.0f})"
            f"{conn_str}"
        )
    return "\n\n".join(lines)


def _format_nearby(records: list[SemanticRecord]) -> str:
    """Medium detail — truncated summary, tags, position."""
    if not records:
        return "  (viewport is empty)"
    lines: list[str] = []
    for r in records[:_CAP_NEARBY]:
        lines.append(
            f"• [{r.object_type.value}] {r.object_id} @ ({r.position.x:.0f}, {r.position.y:.0f})"
            f" — {r.content_summary}"
            f"  #{' #'.join(r.tags)}"
        )
    return "\n".join(lines)


def _format_retrieved(records: list[SemanticRecord]) -> str:
    """Short detail — type, id, brief summary, tags."""
    if not records:
        return "  (none)"
    lines: list[str] = []
    for r in records[:_CAP_RETRIEVED]:
        lines.append(
            f"• [{r.object_type.value}] {r.object_id}: {r.content_summary}"
            f"  #{' #'.join(r.tags)}"
        )
    return "\n".join(lines)


def _format_events(events: list[CanvasEvent]) -> str:
    """Recent activity timeline."""
    if not events:
        return "  (no recent activity)"
    lines: list[str] = []
    for e in events[-_CAP_EVENTS:]:
        ts = _relative_time(e.timestamp)
        lines.append(f"• {ts} [{e.event_type.value}] {e.summary}")
    return "\n".join(lines)


def _format_session(packet: ContextPacket) -> str:
    """Session context block."""
    s = packet.session_summary
    parts: list[str] = []
    if packet.session_goal:
        parts.append(f"Goal: {packet.session_goal}")
    if s.active_topics:
        parts.append(f"Topics: {', '.join(s.active_topics)}")
    if s.recent_decisions:
        for d in s.recent_decisions[-3:]:
            parts.append(f"Decision: {d}")
    if s.open_questions:
        for q in s.open_questions[-2:]:
            parts.append(f"Question: {q}")
    return "\n".join(parts) if parts else "(no session context yet)"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_messages(packet: ContextPacket) -> tuple[str, str]:
    """Convert a ContextPacket into (system_prompt, user_content).

    system_prompt is the static Spark persona + rules + action schema.
    user_content is assembled from the packet's structured sections.

    The tuple is passed directly to the Anthropic messages API:
        messages=[{"role": "user", "content": user_content}]
        system=system_prompt
    """
    user_content = f"""\
## Session
{_format_session(packet)}

## Canvas View
{_format_stats(packet.canvas_stats)}

## Focused Objects (selected by user — full detail)
{_format_selected(packet.selected_objects)}

## Nearby Objects (in viewport — medium detail)
{_format_nearby(packet.nearby_objects)}

## Relevant Objects (off-screen — short detail)
{_format_retrieved(packet.retrieved_objects)}

## Recent Activity
{_format_events(packet.recent_events)}

---
User: {packet.user_message}"""

    return SPARK_SYSTEM, user_content
