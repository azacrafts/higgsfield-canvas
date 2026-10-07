"""
context.models
==============

Pydantic data models for the context-awareness subsystem.

SemanticRecord   — one per canvas object, stored in the ContentRegistry
CanvasEvent      — append-only event log entry
SessionSummary   — high-level board state maintained by the agent
ContextPacket    — the curated context injected into each Claude prompt
CanvasDiff       — result of comparing a fresh canvas snapshot to the registry

Utility
-------
content_hash_for(shape)  — fingerprint of a raw tldraw shape's *content*
                            (ignores position / size — those only cause
                            a "moved" classification, not "updated")
"""

from __future__ import annotations

import hashlib
import json
import time
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ObjectType(str, Enum):
    sticky_note = "sticky_note"
    image = "image"
    link = "link"
    video = "video"
    shape = "shape"
    diagram = "diagram"
    arrow = "arrow"


class EventType(str, Enum):
    created = "created"
    updated = "updated"
    moved = "moved"
    deleted = "deleted"
    instruction = "instruction"    # user said something to the agent
    agent_action = "agent_action"  # agent modified the canvas
    user_feedback = "user_feedback"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class Position(BaseModel):
    x: float
    y: float


class Size(BaseModel):
    w: float
    h: float


# ---------------------------------------------------------------------------
# Core domain models
# ---------------------------------------------------------------------------


class SemanticRecord(BaseModel):
    """Lightweight semantic summary of a single canvas object.

    Created once when an object is placed / updated, then cached in
    ContentRegistry.  The LLM reads this — never the raw tldraw shape.
    """

    object_id: str
    object_type: ObjectType
    position: Position
    size: Optional[Size] = None

    # The main semantic payload — text body, image description, link title, etc.
    content_summary: str

    # 3-5 descriptive tags for fast keyword retrieval.
    tags: list[str] = Field(default_factory=list, max_length=5)

    # IDs of shapes this object is connected to via arrows.
    connections: list[str] = Field(default_factory=list)

    # For diagram shapes: {nodes: [...], edges: [...]} adjacency data.
    structural_data: Optional[dict[str, Any]] = None

    # Flexible bag for extra metadata:
    #   images    → {generation_prompt, aspect_ratio, source_url}
    #   links     → {og_title, og_description, og_image, resolved_url}
    #   videos    → {generation_prompt, duration_s, source_image_id}
    #   notes     → {color}
    meta: dict[str, Any] = Field(default_factory=dict)

    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    # SHA-256 of content-bearing fields only (text, url, type, geo, color,
    # bindings, semantic metadata).
    # Position / size changes do NOT change this hash — they trigger a
    # "moved" diff entry instead of "updated".
    content_hash: str = ""


class CanvasEvent(BaseModel):
    """One entry in the append-only event log."""

    event_type: EventType
    object_id: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)

    # Short human-readable description — used in recent_events context.
    # Example: 'Created sticky note "API rate limit"'
    summary: str


class SessionSummary(BaseModel):
    """Compressed high-level understanding of the current board session.

    Updated by the agent periodically (when needs_summary_update is True).
    Injected into ContextPacket so Claude always has the big picture without
    re-reading every object.
    """

    board_goal: Optional[str] = None
    active_topics: list[str] = Field(default_factory=list)

    # Each cluster: {"label": str, "object_ids": list[str]}
    current_clusters: list[dict[str, Any]] = Field(default_factory=list)

    open_questions: list[str] = Field(default_factory=list)
    recent_decisions: list[str] = Field(default_factory=list)


class ContextPacket(BaseModel):
    """The curated context packet assembled each agent turn.

    This — not the raw canvas — is what Claude sees.  Objects are tiered
    by relevance so the most important context is near the top of the prompt.

    Tiers
    -----
    selected_objects  — objects the user explicitly selected or mentioned
    nearby_objects    — objects in / near the current viewport (medium detail)
    retrieved_objects — off-screen but semantically relevant (short detail)
    """

    session_goal: Optional[str] = None
    user_message: str

    selected_objects: list[SemanticRecord] = Field(default_factory=list)
    nearby_objects: list[SemanticRecord] = Field(default_factory=list)
    retrieved_objects: list[SemanticRecord] = Field(default_factory=list)

    recent_events: list[CanvasEvent] = Field(default_factory=list)
    session_summary: SessionSummary = Field(default_factory=SessionSummary)

    # {"total": int, "by_type": {"sticky_note": 3, "image": 5, ...}}
    canvas_stats: dict[str, Any] = Field(default_factory=dict)


class CanvasDiff(BaseModel):
    """Result of comparing the incoming canvas snapshot against the registry.

    Produced once per turn before preprocessing runs.  Drives which records
    need to be created, updated, or evicted this turn.
    """

    # Shapes present in snapshot but absent from registry.
    new_shapes: list[dict[str, Any]] = Field(default_factory=list)

    # Shapes whose content-hash changed (text edited, URL swapped, etc.).
    updated_shapes: list[dict[str, Any]] = Field(default_factory=list)

    # Shapes where only position / size changed (content_hash identical).
    moved_shapes: list[dict[str, Any]] = Field(default_factory=list)

    # IDs present in registry but absent from snapshot → deleted.
    deleted_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def content_hash_for(shape: dict[str, Any]) -> str:
    """Return a SHA-256 fingerprint of the *content-bearing* fields of a
    raw tldraw shape dict.

    Fields included (what the object IS):
        type, geo, text, url, color, fromId, toId, meta

    Fields excluded (where / how big the object IS):
        x, y, w, h, rotation, opacity, parentId, index, …

    This lets the diff engine distinguish a pure move (hash unchanged)
    from a content edit (hash changed) without false positives.
    """
    content_fields = {
        "type": shape.get("type", ""),
        "geo": shape.get("geo", ""),
        "text": shape.get("text", ""),
        "url": shape.get("url", ""),
        "color": shape.get("color", ""),
        "fromId": shape.get("fromId", ""),
        "toId": shape.get("toId", ""),
    }
    meta = shape.get("meta")
    if isinstance(meta, dict) and meta:
        content_fields["meta"] = {k: meta[k] for k in sorted(meta)}
    serialised = json.dumps(content_fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialised.encode()).hexdigest()
