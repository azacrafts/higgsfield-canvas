"""
context.assembly
================

Assembles the final ContextPacket that is injected into each Claude prompt.

The LLM never sees the raw canvas.  It sees a ContextPacket produced here:
objects are tiered by relevance, summaries are truncated by tier, and
everything is drawn from the pre-built ContentRegistry — never re-derived
from scratch on every turn.

Public API
----------
    build_context_packet(
        user_message, canvas_snapshot,
        registry, event_log, session_memory
    ) → ContextPacket

canvas_snapshot format
-----------------------
A dict with optional keys:

    {
        "shapes":       list[dict],      # simplified shape list (may be absent)
        "viewport":     {"x", "y", "w", "h"},   # visible region (optional)
        "selected_ids": list[str],       # currently selected shapes (optional)
    }

If "viewport" is absent, retrieval uses a broad default viewport.
If "selected_ids" is absent, selection-proximity score is zero for all records.
"""

from __future__ import annotations

import logging
from typing import Any

from context.models import ContextPacket, SemanticRecord, SessionSummary
from context.retrieval import retrieve_relevant
from context.storage import ContentRegistry, EventLog, SessionMemory

logger = logging.getLogger(__name__)

# How many recent events to include in the context packet.
_RECENT_EVENT_COUNT = 10

# How many records to retrieve from the registry each turn.
_RETRIEVAL_K = 20

# Content summary lengths by tier.
_NEARBY_SUMMARY_LEN = 50     # medium detail: truncate to ~50 chars
_RETRIEVED_SUMMARY_LEN = 30  # short detail:  truncate to ~30 chars


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, max_len: int) -> str:
    """Truncate *text* to *max_len* characters, appending '…' if cut."""
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "…"


def _in_viewport(record: SemanticRecord, viewport: dict[str, Any]) -> bool:
    """Return True if the record's centre falls inside *viewport*."""
    vx = float(viewport.get("x", 0))
    vy = float(viewport.get("y", 0))
    vw = float(viewport.get("w", 1920))
    vh = float(viewport.get("h", 1080))
    cx = record.position.x + (record.size.w / 2 if record.size else 0.0)
    cy = record.position.y + (record.size.h / 2 if record.size else 0.0)
    return vx <= cx <= vx + vw and vy <= cy <= vy + vh


def _clip_nearby(record: SemanticRecord) -> SemanticRecord:
    """Return a copy with content_summary truncated for the nearby tier."""
    truncated = _truncate(record.content_summary, _NEARBY_SUMMARY_LEN)
    if truncated == record.content_summary:
        return record
    return record.model_copy(update={"content_summary": truncated})


def _clip_retrieved(record: SemanticRecord) -> SemanticRecord:
    """Return a copy with content_summary truncated for the retrieved tier.

    Also strips meta and structural_data — the prompt formatter only needs
    object_type, the short summary, tags, and position at this tier.
    """
    return record.model_copy(update={
        "content_summary": _truncate(record.content_summary, _RETRIEVED_SUMMARY_LEN),
        "meta": {},
        "structural_data": None,
    })


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_context_packet(
    user_message: str,
    canvas_snapshot: dict[str, Any],
    registry: ContentRegistry,
    event_log: EventLog,
    session_memory: SessionMemory,
) -> ContextPacket:
    """Assemble the ContextPacket for one agent turn.

    Parameters
    ----------
    user_message:
        The raw user text for this turn.
    canvas_snapshot:
        Dict with optional keys "viewport", "selected_ids", "shapes".
        See module docstring for the full schema.
    registry:
        The room's ContentRegistry (already up-to-date after preprocessing).
    event_log:
        The room's EventLog.
    session_memory:
        The room's SessionMemory.

    Returns
    -------
    ContextPacket ready to be formatted into a system prompt.
    """
    # ------------------------------------------------------------------
    # 1. Extract viewport and selected_ids from snapshot
    # ------------------------------------------------------------------
    viewport: dict[str, Any] | None = canvas_snapshot.get("viewport")
    selected_ids: list[str] = canvas_snapshot.get("selected_ids") or []

    # ------------------------------------------------------------------
    # 2. Selected objects (full detail — highest priority tier)
    # ------------------------------------------------------------------
    # Resolve selected IDs against the registry.  IDs not yet registered
    # (e.g. just created this turn, before preprocessing ran) are silently
    # skipped.
    selected_objects: list[SemanticRecord] = [
        r for sid in selected_ids
        if (r := registry.get(sid)) is not None
    ]
    selected_id_set = {r.object_id for r in selected_objects}

    # ------------------------------------------------------------------
    # 3. Retrieve top-K relevant records (excludes already-selected ones)
    # ------------------------------------------------------------------
    retrieved_pool = retrieve_relevant(
        user_message=user_message,
        viewport=viewport,
        selected_ids=selected_ids,
        registry=registry,
        event_log=event_log,
        k=_RETRIEVAL_K,
    )
    # Drop records already surfaced in selected_objects to avoid duplication.
    retrieved_pool = [r for r in retrieved_pool if r.object_id not in selected_id_set]

    # ------------------------------------------------------------------
    # 4. Tier retrieved records by viewport membership
    # ------------------------------------------------------------------
    nearby_objects: list[SemanticRecord] = []
    retrieved_objects: list[SemanticRecord] = []

    for record in retrieved_pool:
        if viewport and _in_viewport(record, viewport):
            nearby_objects.append(_clip_nearby(record))
        else:
            retrieved_objects.append(_clip_retrieved(record))

    # If no viewport was provided, treat all retrieved records as "nearby"
    # so Claude gets medium-detail summaries rather than the shortest tier.
    if viewport is None:
        nearby_objects = [_clip_nearby(r) for r in retrieved_pool]
        retrieved_objects = []

    # ------------------------------------------------------------------
    # 5. Recent events
    # ------------------------------------------------------------------
    recent_events = event_log.get_recent(_RECENT_EVENT_COUNT)

    # ------------------------------------------------------------------
    # 6. Session summary
    # ------------------------------------------------------------------
    session_summary: SessionSummary = session_memory.get_summary()

    # ------------------------------------------------------------------
    # 7. Canvas stats
    # ------------------------------------------------------------------
    canvas_stats = registry.stats()

    # ------------------------------------------------------------------
    # 8. Assemble and return
    # ------------------------------------------------------------------
    packet = ContextPacket(
        session_goal=session_summary.board_goal,
        user_message=user_message,
        selected_objects=selected_objects,
        nearby_objects=nearby_objects,
        retrieved_objects=retrieved_objects,
        recent_events=recent_events,
        session_summary=session_summary,
        canvas_stats=canvas_stats,
    )

    logger.debug(
        "context packet: selected=%d nearby=%d retrieved=%d events=%d total=%d",
        len(selected_objects),
        len(nearby_objects),
        len(retrieved_objects),
        len(recent_events),
        canvas_stats.get("total", 0),
    )
    return packet
