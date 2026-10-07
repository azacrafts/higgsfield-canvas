"""
Chat agent API router.

Exposes POST /api/chat/stream — a streaming SSE endpoint that takes a message
and the current canvas snapshot, then streams back canvas actions + a message
reply.
"""

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, AsyncIterator, Iterator

import anthropic
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.prompts import ACTION_SCHEMA, CHAT_AGENT_SYSTEM
from agent.tools import format_canvas
from config import get_settings
from context.graph import run_context_agent
from pinterest import fetch_pinterest_images

logger = logging.getLogger(__name__)

router = APIRouter()

_client: anthropic.Anthropic | None = None
_moodboard_lock = asyncio.Lock()
_moodboard_signatures: dict[tuple[str, str], str] = {}
_MOODBOARD_KEYWORDS = (
    "moodboard",
    "mood board",
    "inspiration",
    "aesthetic",
    "references",
    "reference images",
    "visual style",
    "pinterest",
    "images of",
    "photos of",
    "pictures of",
    "vibe",
)
_MOODBOARD_IMAGE_W = 160
_MOODBOARD_IMAGE_H = 200
_MOODBOARD_IMAGE_GAP = 24
_MOODBOARD_LABEL_H = 36
_MOODBOARD_LABEL_GAP = 16
_MOODBOARD_COLUMNS = 3
_MOODBOARD_PADDING = 24
_MOODBOARD_ANCHOR_GAP = 56


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    return _client


def _is_moodboard_request(message: str) -> bool:
    lower = message.lower()
    return any(keyword in lower for keyword in _MOODBOARD_KEYWORDS)


def _moodboard_query(message: str) -> str:
    query = message.lower()
    for phrase in (
        "mood board",
        "moodboard",
        "inspiration images",
        "reference images",
        "visual style",
        "references",
        "reference",
        "pinterest",
        "images of",
        "photos of",
        "pictures of",
        "images",
        "photos",
        "pictures",
        "image",
        "photo",
        "picture",
        "visuals",
        "visual",
        "create",
        "make",
        "give me",
        "show me",
        "find",
        "fetch",
        "for",
    ):
        query = query.replace(phrase, " ")
    query = re.sub(r"\s+", " ", query).strip(" ,.-")
    return query or message


def _format_pinterest_context(pinterest_images: list[dict[str, Any]]) -> str:
    if not pinterest_images:
        return ""

    lines = ["Available Pinterest images (use ALL of them as create_image actions):"]
    for idx, image in enumerate(pinterest_images, start=1):
        lines.append(f'  [{idx}] url: "{image["url"]}"  title: "{image["title"]}"')
    lines.append(
        "Layout: place a create_text label above the row, then place images in a "
        "horizontal row - each 160px wide x 200px tall, 20px gap between them, "
        "starting around y=350. Use the exact URLs above."
    )
    return "\n".join(lines)


def _shape_bounds(shape: dict[str, Any]) -> tuple[float, float, float, float] | None:
    x = shape.get("x")
    y = shape.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None

    shape_type = str(shape.get("type") or "")
    width = shape.get("w")
    height = shape.get("h")

    if not isinstance(width, (int, float)) or width <= 0:
        if shape_type == "note":
            width = 200
        elif shape_type == "text":
            text = str(shape.get("text") or "")
            width = min(max(len(text) * 7, 160), 360)
        elif shape_type in {"image", "video"}:
            width = 300
        elif shape_type == "arrow":
            width = 220
        else:
            width = 180

    if not isinstance(height, (int, float)) or height <= 0:
        if shape_type == "note":
            height = 200
        elif shape_type == "text":
            height = 48
        elif shape_type in {"image", "video"}:
            height = 200
        elif shape_type == "arrow":
            height = 40
        else:
            height = 120

    return float(x), float(y), float(x + width), float(y + height)


def _rect_overlaps(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    padding: float = _MOODBOARD_PADDING,
) -> bool:
    return not (
        left[2] + padding <= right[0]
        or left[0] >= right[2] + padding
        or left[3] + padding <= right[1]
        or left[1] >= right[3] + padding
    )


def _find_moodboard_origin(
    canvas_snapshot: dict[str, Any],
    board_w: int,
    board_h: int,
    anchor_shape_id: str | None = None,
) -> tuple[int, int]:
    shapes = canvas_snapshot.get("shapes", [])
    occupied = [
        bounds
        for shape in shapes
        if isinstance(shape, dict)
        for bounds in [_shape_bounds(shape)]
        if bounds is not None
    ]

    viewport = canvas_snapshot.get("viewport")
    if isinstance(viewport, dict):
        viewport_x = int(viewport.get("x", 50))
        viewport_y = int(viewport.get("y", 80))
        viewport_w = int(viewport.get("w", 1200))
        viewport_h = int(viewport.get("h", 800))
    else:
        viewport_x = 50
        viewport_y = 80
        viewport_w = 1200
        viewport_h = 800

    used_max_x = max((bounds[2] for bounds in occupied), default=viewport_x)
    used_max_y = max((bounds[3] for bounds in occupied), default=viewport_y)

    if anchor_shape_id:
        anchor_shape = next(
            (
                shape
                for shape in shapes
                if isinstance(shape, dict) and str(shape.get("id")) == anchor_shape_id
            ),
            None,
        )
        anchor_bounds = _shape_bounds(anchor_shape) if anchor_shape else None
        if anchor_bounds is not None:
            ax1, ay1, ax2, ay2 = anchor_bounds
            anchor_cx = (ax1 + ax2) / 2
            anchor_cy = (ay1 + ay2) / 2
            anchor_candidates = [
                (int(ax2 + _MOODBOARD_ANCHOR_GAP), int(ay1)),
                (int(ax2 + _MOODBOARD_ANCHOR_GAP), int(anchor_cy - board_h / 2)),
                (int(ax1), int(ay2 + _MOODBOARD_ANCHOR_GAP)),
                (int(anchor_cx - board_w / 2), int(ay2 + _MOODBOARD_ANCHOR_GAP)),
                (int(ax1 - board_w - _MOODBOARD_ANCHOR_GAP), int(ay1)),
                (int(ax1 - board_w - _MOODBOARD_ANCHOR_GAP), int(anchor_cy - board_h / 2)),
                (int(ax1), int(ay1 - board_h - _MOODBOARD_ANCHOR_GAP)),
                (int(anchor_cx - board_w / 2), int(ay1 - board_h - _MOODBOARD_ANCHOR_GAP)),
            ]

            for x, y in anchor_candidates:
                if x < 40 or y < 40:
                    continue
                rect = (x, y, x + board_w, y + board_h)
                if all(not _rect_overlaps(rect, bounds) for bounds in occupied):
                    return x, y

            for radius in range(80, 1281, 80):
                min_y = max(40, int(anchor_cy - radius))
                max_y = int(anchor_cy + radius)
                min_x = max(40, int(anchor_cx - radius))
                max_x = int(anchor_cx + radius)
                for y in range(min_y, max_y + 1, 80):
                    for x in range(min_x, max_x + 1, 80):
                        rect = (x, y, x + board_w, y + board_h)
                        if all(not _rect_overlaps(rect, bounds) for bounds in occupied):
                            return x, y

    candidate_rects = [
        (viewport_x + 40, viewport_y + 60),
        (viewport_x + viewport_w + 80, viewport_y + 60),
        (viewport_x + 40, viewport_y + viewport_h + 80),
        (int(used_max_x) + 80, viewport_y + 60),
        (40, int(used_max_y) + 80),
    ]

    for x, y in candidate_rects:
        rect = (x, y, x + board_w, y + board_h)
        if all(not _rect_overlaps(rect, bounds) for bounds in occupied):
            return x, y

    search_min_x = max(40, viewport_x - 200)
    search_min_y = max(40, viewport_y - 120)
    search_max_x = max(viewport_x + viewport_w + 1600, int(used_max_x) + 1600)
    search_max_y = max(viewport_y + viewport_h + 1200, int(used_max_y) + 1200)

    for y in range(search_min_y, search_max_y, 80):
        for x in range(search_min_x, search_max_x, 80):
            rect = (x, y, x + board_w, y + board_h)
            if all(not _rect_overlaps(rect, bounds) for bounds in occupied):
                return x, y

    return int(used_max_x) + 120, int(used_max_y) + 120


def _build_moodboard_actions(
    query: str,
    pinterest_images: list[dict[str, Any]],
    canvas_snapshot: dict[str, Any],
    anchor_shape_id: str | None = None,
) -> list[dict[str, Any]]:
    images = pinterest_images[:5]
    if not images:
        return [
            {
                "_type": "message",
                "text": f'Pinterest returned no images for "{query}".',
            }
        ]

    rows = (len(images) + _MOODBOARD_COLUMNS - 1) // _MOODBOARD_COLUMNS
    board_w = (
        _MOODBOARD_COLUMNS * _MOODBOARD_IMAGE_W
        + (_MOODBOARD_COLUMNS - 1) * _MOODBOARD_IMAGE_GAP
    )
    board_h = (
        _MOODBOARD_LABEL_H
        + _MOODBOARD_LABEL_GAP
        + rows * _MOODBOARD_IMAGE_H
        + max(rows - 1, 0) * _MOODBOARD_IMAGE_GAP
    )
    origin_x, origin_y = _find_moodboard_origin(
        canvas_snapshot,
        board_w,
        board_h,
        anchor_shape_id=anchor_shape_id,
    )

    slug = hashlib.sha1(query.lower().encode("utf-8")).hexdigest()[:8]
    actions: list[dict[str, Any]] = [
        {
            "_type": "create_text",
            "shapeId": f"moodboard_label_{slug}",
            "text": f"Moodboard: {query.title()}",
            "x": origin_x,
            "y": origin_y,
            "color": "black",
        }
    ]

    start_y = origin_y + _MOODBOARD_LABEL_H + _MOODBOARD_LABEL_GAP
    for index, image in enumerate(images):
        row = index // _MOODBOARD_COLUMNS
        col = index % _MOODBOARD_COLUMNS
        actions.append(
            {
                "_type": "create_image",
                "shapeId": f"moodboard_img_{slug}_{index + 1}",
                "url": image["url"],
                "x": origin_x + col * (_MOODBOARD_IMAGE_W + _MOODBOARD_IMAGE_GAP),
                "y": start_y + row * (_MOODBOARD_IMAGE_H + _MOODBOARD_IMAGE_GAP),
                "w": _MOODBOARD_IMAGE_W,
                "h": _MOODBOARD_IMAGE_H,
            }
        )

    actions.append(
        {
            "_type": "message",
            "text": (
                f'Built a Pinterest moodboard for "{query}" near the triggering note.'
                if anchor_shape_id
                else f'Built a Pinterest moodboard for "{query}" in a free area of the canvas.'
            ),
        }
    )
    return actions


async def _verify_moodboard_trigger(
    room_id: str,
    shape_id: str,
    text: str,
) -> dict[str, Any]:
    cleaned = text.strip()
    if not cleaned or not _is_moodboard_request(cleaned):
        return {"should_trigger": False, "reason": "no_moodboard_intent"}

    query = _moodboard_query(cleaned)
    if len(query) < 3:
        return {"should_trigger": False, "reason": "query_too_short"}

    signature = hashlib.sha1(cleaned.lower().encode("utf-8")).hexdigest()
    key = (room_id, shape_id)

    async with _moodboard_lock:
        if _moodboard_signatures.get(key) == signature:
            return {"should_trigger": False, "reason": "duplicate"}
        _moodboard_signatures[key] = signature

    return {
        "should_trigger": True,
        "query": query,
        "trigger_message": f"Create a moodboard for {query}",
    }


# ---------------------------------------------------------------------------
# JSON streaming helper
# ---------------------------------------------------------------------------


def _close_and_parse_json(s: str) -> dict | None:
    """Parse a potentially incomplete JSON string by closing open brackets."""
    stack: list[str] = []
    i = 0
    while i < len(s):
        char = s[i]
        last = stack[-1] if stack else None
        if char == '"':
            if i > 0 and s[i - 1] == "\\":
                i += 1
                continue
            if last == '"':
                stack.pop()
            else:
                stack.append('"')
        if last == '"':
            i += 1
            continue
        if char in ("{", "["):
            stack.append(char)
        elif char == "}" and last == "{":
            stack.pop()
        elif char == "]" and last == "[":
            stack.pop()
        i += 1

    result = s
    for opening in reversed(stack):
        result += {"{": "}", "[": "]", '"': '"'}[opening]

    try:
        return json.loads(result)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Streaming generators
# ---------------------------------------------------------------------------


def _stream_legacy_agent(
    message: str,
    canvas_state: list[dict],
    pinterest_images: list[dict[str, Any]] | None = None,
) -> Iterator[str]:
    """Fallback path using the original prompt-only chat agent."""
    settings = get_settings()
    context_blocks = [format_canvas(canvas_state)]
    image_context = _format_pinterest_context(pinterest_images or [])
    if image_context:
        context_blocks.append(image_context)
    system = CHAT_AGENT_SYSTEM.format(
        canvas="\n\n".join(context_blocks),
        schema=ACTION_SCHEMA,
    )
    buffer = ""
    cursor = 0

    with _get_client().messages.stream(
        model=settings.chat_agent_model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": message}],
    ) as stream:
        for text in stream.text_stream:
            buffer += text
            start = buffer.find("{")
            if start == -1:
                continue
            parsed = _close_and_parse_json(buffer[start:])
            if not parsed:
                continue
            actions = parsed.get("actions")
            if not isinstance(actions, list):
                continue
            while len(actions) > cursor + 1:
                yield f"data: {json.dumps({'type': 'action', 'action': actions[cursor]})}\n\n"
                cursor += 1

    start = buffer.find("{")
    if start != -1:
        parsed = _close_and_parse_json(buffer[start:])
        if parsed:
            actions = parsed.get("actions", [])
            while len(actions) > cursor:
                yield f"data: {json.dumps({'type': 'action', 'action': actions[cursor]})}\n\n"
                cursor += 1

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


async def _stream_agent(
    message: str,
    canvas_snapshot: dict[str, Any],
    room_id: str,
    anchor_shape_id: str | None = None,
) -> AsyncIterator[str]:
    if _is_moodboard_request(message):
        query = _moodboard_query(message)
        pinterest_images = await asyncio.to_thread(
            fetch_pinterest_images,
            query,
            5,
        )
        if pinterest_images:
            logger.info(
                "chat moodboard room=%s fetched %d pinterest images",
                room_id,
                len(pinterest_images),
            )
            for action in _build_moodboard_actions(
                query,
                pinterest_images,
                canvas_snapshot,
                anchor_shape_id=anchor_shape_id,
            ):
                yield f"data: {json.dumps({'type': 'action', 'action': action})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        logger.warning("chat moodboard room=%s returned no pinterest images", room_id)
        failure_action = {
            "_type": "message",
            "text": f'Could not fetch Pinterest images for "{query}" right now.',
        }
        yield f"data: {json.dumps({'type': 'action', 'action': failure_action})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    try:
        actions = await run_context_agent(
            message=message,
            canvas_snapshot=canvas_snapshot,
            room_id=room_id,
        )
    except Exception:
        logger.exception(
            "context-aware chat failed for room=%s; falling back to legacy prompt",
            room_id,
        )
        for event in _stream_legacy_agent(message, canvas_snapshot.get("shapes", [])):
            yield event
        return

    for action in actions:
        yield f"data: {json.dumps({'type': 'action', 'action': action})}\n\n"

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str
    canvas_state: list[dict[str, Any]] = Field(default_factory=list)
    canvas_snapshot: dict[str, Any] = Field(default_factory=dict)
    room_id: str = "main"
    anchor_shape_id: str | None = None


class MoodboardVerifyRequest(BaseModel):
    room_id: str = "main"
    shape_id: str
    text: str


def _coerce_canvas_snapshot(body: ChatRequest) -> dict[str, Any]:
    snapshot = dict(body.canvas_snapshot)
    shapes = snapshot.get("shapes")

    if not isinstance(shapes, list) or (not shapes and body.canvas_state):
        snapshot["shapes"] = list(body.canvas_state)

    selected_ids = snapshot.get("selected_ids")
    if not isinstance(selected_ids, list):
        snapshot["selected_ids"] = []

    viewport = snapshot.get("viewport")
    if viewport is not None and not isinstance(viewport, dict):
        snapshot.pop("viewport", None)

    return snapshot


@router.post("/api/chat/stream")
async def chat_stream(body: ChatRequest):
    canvas_snapshot = _coerce_canvas_snapshot(body)
    return StreamingResponse(
        _stream_agent(
            body.message,
            canvas_snapshot,
            body.room_id,
            anchor_shape_id=body.anchor_shape_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/api/moodboard/verify")
async def moodboard_verify(body: MoodboardVerifyRequest):
    return await _verify_moodboard_trigger(body.room_id, body.shape_id, body.text)
