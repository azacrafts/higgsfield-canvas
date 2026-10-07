"""
context.retrieval
=================

Relevance-based retrieval over the ContentRegistry.

No vector DB.  Each record in the registry is scored against six signals,
combined as a weighted sum, and the top-K results are returned.

Public API
----------
    retrieve_relevant(
        user_message, viewport, selected_ids,
        registry, event_log, k=20
    ) → list[SemanticRecord]

Scoring weights (must sum to 1.0)
----------------------------------
    Spatial proximity to viewport centre   0.20
    Proximity to selected shapes           0.25
    Recency (updated_at decay)             0.15
    Structural linkage to selection        0.20
    Tag overlap with user message          0.15
    Type keyword boost                     0.05
                                          -----
    Total                                  1.00
"""

from __future__ import annotations

import math
import re
import time
import logging
from typing import Any

from context.models import ObjectType, SemanticRecord
from context.storage import ContentRegistry, EventLog

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scoring weights — must sum to 1.0
# ---------------------------------------------------------------------------
_W_SPATIAL = 0.20
_W_SELECTION = 0.25
_W_RECENCY = 0.15
_W_LINKAGE = 0.20
_W_TAGS = 0.15
_W_TYPE = 0.05

# Recency half-life: a shape updated 5 minutes ago scores 0.5 on this signal.
_RECENCY_HALFLIFE_S: float = 300.0

# Default viewport used when the caller doesn't provide one.
# Matches the typical tldraw canvas bounds used in the rest of the codebase
# (x: 50-1100, y: 50-700 per the agent prompts).
_DEFAULT_VIEWPORT: dict[str, float] = {"x": -200.0, "y": -200.0, "w": 2400.0, "h": 1600.0}

# ---------------------------------------------------------------------------
# Stopwords (shared with preprocessors; kept local to avoid circular imports)
# ---------------------------------------------------------------------------
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "but", "or", "nor", "for", "yet", "so",
    "is", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "shall", "can", "need", "just", "very", "also",
    "to", "of", "in", "on", "at", "by", "up", "as", "if", "it",
    "its", "this", "that", "these", "those", "from", "with", "about",
    "into", "than", "then", "when", "where", "how", "what", "which",
    "who", "not", "no", "all", "any", "each", "few", "more", "most",
    "other", "own", "same", "such", "both", "only", "here", "there",
    "too", "out", "use", "used", "using", "get", "got", "new", "one",
    "two", "three", "four", "five", "six", "per", "via", "i", "me",
    "we", "you", "he", "she", "they", "them", "our", "your", "their",
})

# ---------------------------------------------------------------------------
# Type keyword → ObjectType mapping (for the type boost signal)
# ---------------------------------------------------------------------------
_TYPE_KEYWORD_MAP: dict[str, ObjectType] = {
    "image":    ObjectType.image,
    "images":   ObjectType.image,
    "photo":    ObjectType.image,
    "photos":   ObjectType.image,
    "picture":  ObjectType.image,
    "pictures": ObjectType.image,
    "visual":   ObjectType.image,
    "visuals":  ObjectType.image,
    "note":     ObjectType.sticky_note,
    "notes":    ObjectType.sticky_note,
    "sticky":   ObjectType.sticky_note,
    "stickies": ObjectType.sticky_note,
    "text":     ObjectType.sticky_note,
    "video":    ObjectType.video,
    "videos":   ObjectType.video,
    "clip":     ObjectType.video,
    "animation":ObjectType.video,
    "animate":  ObjectType.video,
    "link":     ObjectType.link,
    "links":    ObjectType.link,
    "url":      ObjectType.link,
    "website":  ObjectType.link,
    "arrow":    ObjectType.arrow,
    "arrows":   ObjectType.arrow,
    "connection":ObjectType.arrow,
    "diagram":  ObjectType.diagram,
    "diagrams": ObjectType.diagram,
    "uml":      ObjectType.diagram,
    "shape":    ObjectType.shape,
    "shapes":   ObjectType.shape,
    "box":      ObjectType.shape,
    "boxes":    ObjectType.shape,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_center(record: SemanticRecord) -> tuple[float, float]:
    cx = record.position.x + (record.size.w / 2 if record.size else 0.0)
    cy = record.position.y + (record.size.h / 2 if record.size else 0.0)
    return cx, cy


def _extract_message_keywords(message: str) -> set[str]:
    """Lowercase word-set from the user message, stopwords removed."""
    words = re.findall(r"\b[a-zA-Z]{3,}\b", message.lower())
    return {w for w in words if w not in _STOPWORDS}


def _viewport_radius(viewport: dict[str, Any]) -> float:
    """Characteristic radius of the viewport (half the diagonal)."""
    w = float(viewport.get("w", _DEFAULT_VIEWPORT["w"]))
    h = float(viewport.get("h", _DEFAULT_VIEWPORT["h"]))
    return math.hypot(w, h) / 2.0


# ---------------------------------------------------------------------------
# Individual scoring signals — each returns a float in [0, 1]
# ---------------------------------------------------------------------------


def _score_spatial(
    record: SemanticRecord,
    viewport: dict[str, Any],
    vp_radius: float,
) -> float:
    """How close is the record's centre to the viewport centre?"""
    vp_cx = float(viewport.get("x", 0)) + float(viewport.get("w", 0)) / 2.0
    vp_cy = float(viewport.get("y", 0)) + float(viewport.get("h", 0)) / 2.0
    rx, ry = _record_center(record)
    dist = math.hypot(rx - vp_cx, ry - vp_cy)
    # Full score at centre, zero when two viewport-radii away.
    return max(0.0, 1.0 - dist / max(1.0, vp_radius * 2.0))


def _score_selection(
    record: SemanticRecord,
    selected_centers: list[tuple[float, float]],
    selected_ids_set: set[str],
    vp_radius: float,
) -> float:
    """How close is the record to the nearest selected shape?"""
    if record.object_id in selected_ids_set:
        return 1.0                         # the record itself is selected
    if not selected_centers:
        return 0.0
    rx, ry = _record_center(record)
    min_dist = min(math.hypot(rx - sx, ry - sy) for sx, sy in selected_centers)
    return max(0.0, 1.0 - min_dist / max(1.0, vp_radius * 2.0))


def _score_recency(record: SemanticRecord) -> float:
    """Exponential decay based on time since last update."""
    age = max(0.0, time.time() - record.updated_at)
    # exp(-age * ln2 / halflife): score = 1 at age=0, 0.5 at age=halflife
    return math.exp(-age * math.log(2) / _RECENCY_HALFLIFE_S)


def _score_linkage(
    record: SemanticRecord,
    selected_ids_set: set[str],
    selected_records: list[SemanticRecord],
) -> float:
    """Is the record directly connected (via arrows) to any selected shape?"""
    if not selected_ids_set:
        return 0.0
    # This record's arrow connections point to selected shapes.
    if set(record.connections) & selected_ids_set:
        return 1.0
    # A selected shape's connections include this record.
    for sel in selected_records:
        if record.object_id in sel.connections:
            return 1.0
    return 0.0


def _score_tags(
    record: SemanticRecord,
    message_keywords: set[str],
) -> float:
    """Word intersection between message keywords and the record's tags."""
    if not message_keywords or not record.tags:
        return 0.0
    record_tag_words = set(re.findall(r"\b[a-zA-Z]{3,}\b", " ".join(record.tags).lower()))
    intersection = len(message_keywords & record_tag_words)
    # Normalize: 3+ matching words → score 1.0
    return min(1.0, intersection / max(1, min(3, len(message_keywords))))


def _score_type_boost(
    record: SemanticRecord,
    message_keywords: set[str],
) -> float:
    """Boost records whose ObjectType is explicitly mentioned in the message."""
    boosted: set[ObjectType] = set()
    for kw in message_keywords:
        ot = _TYPE_KEYWORD_MAP.get(kw)
        if ot is not None:
            boosted.add(ot)
    return 1.0 if record.object_type in boosted else 0.0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def retrieve_relevant(
    user_message: str,
    viewport: dict[str, Any] | None,
    selected_ids: list[str],
    registry: ContentRegistry,
    event_log: EventLog,
    k: int = 20,
) -> list[SemanticRecord]:
    """Score every record in the registry and return the top-*k* by relevance.

    Parameters
    ----------
    user_message:
        The raw text of the user's current message.
    viewport:
        The visible canvas region as ``{"x": float, "y": float,
        "w": float, "h": float}``.  Pass *None* to use the default
        broad viewport covering the typical canvas bounds.
    selected_ids:
        IDs of shapes the user currently has selected in tldraw.
    registry:
        The room's ContentRegistry.
    event_log:
        The room's EventLog (reserved for future event-based boosting).
    k:
        Maximum number of records to return.

    Returns
    -------
    list[SemanticRecord] sorted by descending relevance score, length ≤ k.
    """
    if not registry.get_all():
        return []

    vp: dict[str, Any] = viewport if viewport else _DEFAULT_VIEWPORT
    vp_radius = _viewport_radius(vp)

    selected_ids_set = set(selected_ids)
    selected_records: list[SemanticRecord] = [
        r for sid in selected_ids
        if (r := registry.get(sid)) is not None
    ]
    selected_centers = [_record_center(r) for r in selected_records]
    message_keywords = _extract_message_keywords(user_message)

    scored: list[tuple[float, SemanticRecord]] = []

    for record in registry.get_all():
        s_spatial   = _score_spatial(record, vp, vp_radius)
        s_selection = _score_selection(record, selected_centers, selected_ids_set, vp_radius)
        s_recency   = _score_recency(record)
        s_linkage   = _score_linkage(record, selected_ids_set, selected_records)
        s_tags      = _score_tags(record, message_keywords)
        s_type      = _score_type_boost(record, message_keywords)

        score = (
            _W_SPATIAL    * s_spatial
            + _W_SELECTION * s_selection
            + _W_RECENCY   * s_recency
            + _W_LINKAGE   * s_linkage
            + _W_TAGS      * s_tags
            + _W_TYPE      * s_type
        )
        scored.append((score, record))

    scored.sort(key=lambda t: t[0], reverse=True)

    results = [r for _, r in scored[:k]]
    logger.debug(
        "retrieve_relevant: scored %d records, returning top %d (k=%d)",
        len(scored),
        len(results),
        k,
    )
    return results
