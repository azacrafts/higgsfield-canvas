"""
context.preprocessors
=====================

Type-specific preprocessors that turn raw tldraw shape dicts into
SemanticRecords suitable for storage in the ContentRegistry.

Public API
----------
    async preprocess_shape(shape_data, existing_record) → SemanticRecord
    should_reprocess(existing_record, new_shape_data) → bool

Routing
-------
    "note"  / "text"            → _preprocess_note()
    "image"                     → _preprocess_image()   ← EXPENSIVE (Vision API)
    "arrow"                     → _preprocess_arrow()
    "bookmark"                  → _preprocess_link()
    "video"                     → _preprocess_video()
    "geo" (UML-style text)      → _preprocess_diagram()
    "geo" (plain)               → _preprocess_geo()
    anything else               → _preprocess_generic()

Vision cache
------------
Image descriptions are cached by content_hash so the Vision API is called
at most once per unique image URL.  The cache is process-scoped (all rooms
share it) which is correct — the same image URL always produces the same
description.

# UPGRADE: persist the vision cache to Redis or Postgres so it survives
#          restarts and is shared across horizontally-scaled workers.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import unquote, urlparse

from anthropic import AsyncAnthropic

from config import get_settings
from context.models import (
    ObjectType,
    Position,
    SemanticRecord,
    Size,
    content_hash_for,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vision model — use the cheapest model; haiku is sufficient for captions.
# UPGRADE: make configurable via Settings
# ---------------------------------------------------------------------------
_VISION_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# Vision result cache: content_hash → (summary, tags)
# UPGRADE: replace with Redis/Postgres for persistence + cross-worker sharing
# ---------------------------------------------------------------------------
_vision_cache: dict[str, tuple[str, list[str]]] = {}

# ---------------------------------------------------------------------------
# Lazy Anthropic client
# ---------------------------------------------------------------------------
_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=get_settings().anthropic_api_key)
    return _client


# ---------------------------------------------------------------------------
# Stopwords + keyword extraction
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


def _extract_keywords(text: str, max_tags: int = 5) -> list[str]:
    """Extract the top *max_tags* content keywords from *text*.

    Process: lowercase → regex-split into words (3+ chars) → drop stopwords
             → frequency-rank → return top N.
    """
    words = re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())
    filtered = [w for w in words if w not in _STOPWORDS]
    if not filtered:
        return []
    freq: dict[str, int] = {}
    for w in filtered:
        freq[w] = freq.get(w, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: -kv[1])
    return [w for w, _ in ranked[:max_tags]]


# ---------------------------------------------------------------------------
# Object-type detector
# ---------------------------------------------------------------------------

def _detect_object_type(shape: dict) -> ObjectType:
    """Map a raw tldraw shape dict to the closest ObjectType."""
    t = shape.get("type", "")
    if t == "note":
        return ObjectType.sticky_note
    if t == "text":
        return ObjectType.sticky_note
    if t == "image":
        return ObjectType.image
    if t == "arrow":
        return ObjectType.arrow
    if t == "bookmark":
        return ObjectType.link
    if t == "video":
        return ObjectType.video
    if t == "geo":
        # Shapes whose text follows the UML "\n---\n" separator convention
        # are treated as diagram nodes.
        text = shape.get("text", "")
        if text.count("\n---\n") >= 1:
            return ObjectType.diagram
        return ObjectType.shape
    return ObjectType.shape


def _make_size(shape: dict) -> Size | None:
    w = shape.get("w")
    h = shape.get("h")
    if w is not None and h is not None:
        return Size(w=float(w), h=float(h))
    return None


def _unwrap_proxy_url(url: str) -> str:
    """Extract the original URL if it was wrapped by the backend proxy."""
    for prefix in ("/api/proxy-image?url=", "/api/proxy-media?url="):
        idx = url.find(prefix)
        if idx != -1:
            return unquote(url[idx + len(prefix):])
    return url


# ---------------------------------------------------------------------------
# Type-specific preprocessors
# ---------------------------------------------------------------------------


def _preprocess_note(shape: dict, existing: SemanticRecord | None) -> SemanticRecord:
    """Sticky note or free-floating text label."""
    text = shape.get("text", "").strip()
    content_summary = text or "(empty note)"
    tags = _extract_keywords(text)
    meta: dict[str, Any] = {}
    if shape.get("color"):
        meta["color"] = shape["color"]
    return SemanticRecord(
        object_id=shape["id"],
        object_type=ObjectType.sticky_note,
        position=Position(x=float(shape.get("x", 0)), y=float(shape.get("y", 0))),
        size=_make_size(shape),
        content_summary=content_summary,
        tags=tags,
        meta=meta,
        content_hash=content_hash_for(shape),
        created_at=existing.created_at if existing else time.time(),
        updated_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Image preprocessor — EXPENSIVE: calls Claude Vision API
# ---------------------------------------------------------------------------

async def _preprocess_image(
    shape: dict,
    existing: SemanticRecord | None,
) -> SemanticRecord:
    """
    # EXPENSIVE PREPROCESSOR — calls Claude Vision API.
    #
    # Cost: ~1–3 Haiku input tokens per image (vision pricing).
    # Latency: ~1-3 seconds per uncached image.
    # Cache: result is stored in _vision_cache keyed by content_hash.
    #        The same image URL is never described twice in the same process.
    #
    # Fallback: if the Vision call fails for any reason, returns a minimal
    #           record with a position-based placeholder summary.
    """
    c_hash = content_hash_for(shape)
    url = _unwrap_proxy_url(shape.get("url", ""))

    # ------------------------------------------------------------------
    # Cache hit — skip Vision call entirely
    # ------------------------------------------------------------------
    if c_hash in _vision_cache:
        cached_summary, cached_tags = _vision_cache[c_hash]
        logger.debug("vision cache hit for shape %s", shape.get("id"))
        return SemanticRecord(
            object_id=shape["id"],
            object_type=ObjectType.image,
            position=Position(x=float(shape.get("x", 0)), y=float(shape.get("y", 0))),
            size=_make_size(shape),
            content_summary=cached_summary,
            tags=cached_tags,
            meta={"source_url": url},
            content_hash=c_hash,
            created_at=existing.created_at if existing else time.time(),
            updated_at=time.time(),
        )

    # ------------------------------------------------------------------
    # Fallback: no URL or data-URL (can't send to Vision)
    # ------------------------------------------------------------------
    if not url or url.startswith("data:"):
        fallback_summary = (
            f"Image at ({shape.get('x', 0):.0f}, {shape.get('y', 0):.0f})"
        )
        return SemanticRecord(
            object_id=shape["id"],
            object_type=ObjectType.image,
            position=Position(x=float(shape.get("x", 0)), y=float(shape.get("y", 0))),
            size=_make_size(shape),
            content_summary=fallback_summary,
            tags=[],
            meta={"source_url": url},
            content_hash=c_hash,
            created_at=existing.created_at if existing else time.time(),
            updated_at=time.time(),
        )

    # ------------------------------------------------------------------
    # Vision API call
    # ------------------------------------------------------------------
    summary = ""
    tags: list[str] = []
    try:
        response = await _get_client().messages.create(
            model=_VISION_MODEL,
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "url", "url": url},
                        },
                        {
                            "type": "text",
                            "text": (
                                "Describe this image for a visual brainstorming canvas in 1-2 sentences. "
                                "Then list 3-5 short tags.\n\n"
                                "Use this exact format:\n"
                                "SUMMARY: <1-2 sentence description>\n"
                                "TAGS: <tag1, tag2, tag3>"
                            ),
                        },
                    ],
                }
            ],
        )
        raw = response.content[0].text if response.content else ""
        summary, tags = _parse_vision_response(raw)
        _vision_cache[c_hash] = (summary, tags)
        logger.debug("vision described shape %s: %r tags=%s", shape.get("id"), summary[:60], tags)

    except Exception as exc:
        logger.warning(
            "vision call failed for shape %s (%s): %s",
            shape.get("id"),
            url[:80],
            exc,
        )
        summary = f"Image at ({shape.get('x', 0):.0f}, {shape.get('y', 0):.0f})"
        tags = []

    return SemanticRecord(
        object_id=shape["id"],
        object_type=ObjectType.image,
        position=Position(x=float(shape.get("x", 0)), y=float(shape.get("y", 0))),
        size=_make_size(shape),
        content_summary=summary,
        tags=tags,
        meta={"source_url": url},
        content_hash=c_hash,
        created_at=existing.created_at if existing else time.time(),
        updated_at=time.time(),
    )


def _parse_vision_response(text: str) -> tuple[str, list[str]]:
    """Parse the structured Vision response into (summary, tags)."""
    summary = ""
    tags: list[str] = []
    for line in text.strip().splitlines():
        line = line.strip()
        if line.startswith("SUMMARY:"):
            summary = line[len("SUMMARY:"):].strip()
        elif line.startswith("TAGS:"):
            raw_tags = line[len("TAGS:"):].strip()
            tags = [t.strip().lower() for t in raw_tags.split(",") if t.strip()][:5]
    return summary or "Image on canvas", tags


# ---------------------------------------------------------------------------


def _preprocess_link(shape: dict, existing: SemanticRecord | None) -> SemanticRecord:
    """Bookmark / link shape.  Expects OG metadata in shape.meta (from frontend)."""
    url = shape.get("url", "")
    meta_in: dict = shape.get("meta", {})

    og_title: str = meta_in.get("og_title") or meta_in.get("title", "")
    og_description: str = meta_in.get("og_description") or meta_in.get("description", "")
    domain = ""
    if url:
        try:
            domain = urlparse(url).netloc
        except Exception:
            pass

    if og_title:
        if og_description:
            content_summary = f"{og_title} ({domain}): {og_description}"
        else:
            content_summary = f"{og_title} ({domain})"
    else:
        content_summary = f"Link: {url}" if url else "Link (no URL)"

    tags = _extract_keywords(f"{og_title} {og_description}")

    return SemanticRecord(
        object_id=shape["id"],
        object_type=ObjectType.link,
        position=Position(x=float(shape.get("x", 0)), y=float(shape.get("y", 0))),
        size=_make_size(shape),
        content_summary=content_summary,
        tags=tags,
        meta={
            "url": url,
            "domain": domain,
            "og_title": og_title,
            "og_description": og_description,
            "og_image": meta_in.get("og_image", ""),
        },
        content_hash=content_hash_for(shape),
        created_at=existing.created_at if existing else time.time(),
        updated_at=time.time(),
    )


def _preprocess_video(shape: dict, existing: SemanticRecord | None) -> SemanticRecord:
    """Video shape (generated by Higgsfield or placed manually)."""
    meta_in: dict = shape.get("meta", {})
    gen_prompt: str = meta_in.get("generation_prompt") or meta_in.get("prompt", "")
    why: str = meta_in.get("why", "")

    if gen_prompt:
        content_summary = gen_prompt
        if why:
            content_summary = f"{gen_prompt} — {why}"
    else:
        content_summary = shape.get("text", "").strip() or "Video clip"

    tags = _extract_keywords(content_summary)

    return SemanticRecord(
        object_id=shape["id"],
        object_type=ObjectType.video,
        position=Position(x=float(shape.get("x", 0)), y=float(shape.get("y", 0))),
        size=_make_size(shape),
        content_summary=content_summary,
        tags=tags,
        meta={
            "generation_prompt": gen_prompt,
            "duration_s": meta_in.get("duration_s"),
            "source_image_id": meta_in.get("source_image_id", ""),
            "url": shape.get("url", ""),
        },
        content_hash=content_hash_for(shape),
        created_at=existing.created_at if existing else time.time(),
        updated_at=time.time(),
    )


def _preprocess_arrow(shape: dict, existing: SemanticRecord | None) -> SemanticRecord:
    """Arrow shape.  Extracts fromId/toId to build the canvas connection graph."""
    # tldraw stores bindings at top level (our simplified format) or in props.
    props: dict = shape.get("props", {})
    from_id: str = (
        shape.get("fromId")
        or shape.get("startId")
        or props.get("start", {}).get("boundShapeId", "")
        or ""
    )
    to_id: str = (
        shape.get("toId")
        or shape.get("endId")
        or props.get("end", {}).get("boundShapeId", "")
        or ""
    )
    connections = [c for c in [from_id, to_id] if c]

    label = shape.get("text", "").strip()
    parts = ["Arrow"]
    if label:
        parts.append(f'"{label}"')
    if from_id:
        parts.append(f"from:{from_id}")
    if to_id:
        parts.append(f"to:{to_id}")
    content_summary = " ".join(parts)

    return SemanticRecord(
        object_id=shape["id"],
        object_type=ObjectType.arrow,
        position=Position(x=float(shape.get("x", 0)), y=float(shape.get("y", 0))),
        size=_make_size(shape),
        content_summary=content_summary,
        tags=_extract_keywords(label) if label else [],
        connections=connections,
        meta={"from_id": from_id, "to_id": to_id, "label": label},
        content_hash=content_hash_for(shape),
        created_at=existing.created_at if existing else time.time(),
        updated_at=time.time(),
    )


def _preprocess_diagram(shape: dict, existing: SemanticRecord | None) -> SemanticRecord:
    """Geo shape whose text follows the UML "\n---\n" convention.

    Does NOT attempt to parse UML semantics; it lightly structures the text
    into sections and stores it in structural_data for the prompt formatter.

    Example text: "Order\\n---\\n- orderId: int\\n---\\n+ createOrder()"
    structural_data: {"sections": ["Order", "- orderId: int", "+ createOrder()"]}
    """
    text = shape.get("text", "").strip()
    sections = [s.strip() for s in text.split("\n---\n")] if text else []
    name = sections[0] if sections else "Diagram node"
    content_summary = f'[{shape.get("geo", "diagram")}] {name}'
    if len(sections) > 1:
        content_summary += f" ({len(sections) - 1} section(s))"

    structural_data: dict[str, Any] = {"sections": sections, "raw_text": text}

    return SemanticRecord(
        object_id=shape["id"],
        object_type=ObjectType.diagram,
        position=Position(x=float(shape.get("x", 0)), y=float(shape.get("y", 0))),
        size=_make_size(shape),
        content_summary=content_summary,
        tags=_extract_keywords(text),
        structural_data=structural_data,
        meta={"color": shape.get("color", ""), "geo": shape.get("geo", "")},
        content_hash=content_hash_for(shape),
        created_at=existing.created_at if existing else time.time(),
        updated_at=time.time(),
    )


def _preprocess_geo(shape: dict, existing: SemanticRecord | None) -> SemanticRecord:
    """Plain geo shape (rectangle, ellipse, etc.) without UML sections."""
    geo = shape.get("geo", "rectangle")
    text = shape.get("text", "").strip()
    content_summary = f"{geo} shape"
    if text:
        content_summary += f': "{text}"'
    return SemanticRecord(
        object_id=shape["id"],
        object_type=ObjectType.shape,
        position=Position(x=float(shape.get("x", 0)), y=float(shape.get("y", 0))),
        size=_make_size(shape),
        content_summary=content_summary,
        tags=_extract_keywords(text),
        meta={"geo": geo, "color": shape.get("color", "")},
        content_hash=content_hash_for(shape),
        created_at=existing.created_at if existing else time.time(),
        updated_at=time.time(),
    )


def _preprocess_generic(shape: dict, existing: SemanticRecord | None) -> SemanticRecord:
    """Catch-all for unrecognised shape types."""
    text = shape.get("text", "").strip()
    content_summary = text or f'{shape.get("type", "unknown")} object'
    return SemanticRecord(
        object_id=shape["id"],
        object_type=ObjectType.shape,
        position=Position(x=float(shape.get("x", 0)), y=float(shape.get("y", 0))),
        size=_make_size(shape),
        content_summary=content_summary,
        tags=_extract_keywords(text),
        content_hash=content_hash_for(shape),
        created_at=existing.created_at if existing else time.time(),
        updated_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def preprocess_shape(
    shape_data: dict,
    existing_record: SemanticRecord | None = None,
) -> SemanticRecord:
    """Route *shape_data* to the appropriate type-specific preprocessor.

    Async because image preprocessing may call the Vision API.
    All other preprocessors are synchronous internally.

    Parameters
    ----------
    shape_data:
        A single raw shape dict from the simplified canvas state.
    existing_record:
        The current SemanticRecord in the registry for this shape, if any.
        Used to preserve created_at timestamps across updates.

    Returns
    -------
    SemanticRecord ready to be written into the ContentRegistry via
    registry.set(shape_data["id"], record).
    """
    if "id" not in shape_data:
        raise ValueError("shape_data must have an 'id' field")

    obj_type = _detect_object_type(shape_data)

    if obj_type == ObjectType.sticky_note:
        return _preprocess_note(shape_data, existing_record)
    if obj_type == ObjectType.image:
        return await _preprocess_image(shape_data, existing_record)
    if obj_type == ObjectType.link:
        return _preprocess_link(shape_data, existing_record)
    if obj_type == ObjectType.video:
        return _preprocess_video(shape_data, existing_record)
    if obj_type == ObjectType.arrow:
        return _preprocess_arrow(shape_data, existing_record)
    if obj_type == ObjectType.diagram:
        return _preprocess_diagram(shape_data, existing_record)
    if obj_type == ObjectType.shape and shape_data.get("type") == "geo":
        return _preprocess_geo(shape_data, existing_record)
    return _preprocess_generic(shape_data, existing_record)


def should_reprocess(
    existing_record: SemanticRecord,
    new_shape_data: dict,
) -> bool:
    """Return True only if the shape's semantic content has changed.

    A pure move (position/size change only) returns False — the diff
    engine handles those without calling the preprocessor.
    This function is a convenience wrapper around content_hash_for();
    call it before queueing a shape for preprocessing to avoid redundant work.
    """
    return content_hash_for(new_shape_data) != existing_record.content_hash
