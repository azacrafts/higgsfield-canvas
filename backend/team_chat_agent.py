"""
Proactive AI participation in team chat.

Key behaviours:
  1. Detects creative intent in team chat and acts on canvas.
  2. If a user shared an image in chat and then asks to animate/generate video,
     uploads that image and kicks off the video pipeline directly — no LLM needed.
  3. Uses a single-shot Claude call (CHAT_AGENT_SYSTEM + JSON prefill) to keep
     token usage low and avoid rate limits.

Entry point: analyze_team_chat(room_id, username, text)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

logger = logging.getLogger(__name__)

AI_USERNAME = "Higgs AI"
_AGENT_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------

_TASK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(moodboard|mood\s+board|inspiration\s+board|pinterest\s+board)\b", re.I), "moodboard"),
    (re.compile(r"\b(diagram|flowchart|flow\s+chart|mindmap|mind\s+map|uml)\b", re.I), "diagram"),
    (re.compile(r"\b(mock[\s-]?up|wireframe|prototype|wireframes|mockups)\b", re.I), "mockup"),
    (re.compile(r"\b(generate|create|draw|design|make|produce)\b.{0,40}\b(image|illustration|picture|visual|photo|render)\b", re.I), "image"),
    (re.compile(r"\b(plan|outline|roadmap|structure|layout|schedule|timeline)\b", re.I), "plan"),
    (re.compile(r"\b(brainstorm|ideate|brainstorming)\b", re.I), "brainstorm"),
]

_DESIRE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(let'?s|lets)\b", re.I),
    re.compile(r"\bwould\s+be\s+(good|great|nice|cool|helpful|awesome)\b", re.I),
    re.compile(r"\b(should|we\s+should|we\s+could|could\s+we|can\s+we|can\s+you)\b", re.I),
    re.compile(r"\b(want\s+to|wanna|need\s+to|needs?\s+a|i\s+think\s+we)\b", re.I),
    re.compile(r"\b(how\s+about|what\s+if|maybe\s+we|perhaps)\b", re.I),
    re.compile(r"\b(make|create|generate|draw|build|design)\b", re.I),
]

_ADDRESS_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(higgs|@ai|@agent|hey\s+ai|ok\s+ai|yo\s+ai)\b", re.I),
]

_VIDEO_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(generate|create|make|animate)\b.{0,30}\b(video|animation|clip)\b", re.I),
    re.compile(r"\b(animate|animation)\b.{0,30}\b(this|the|it|image|photo|picture)\b", re.I),
    re.compile(r"\bmake\s+(it|this|the\s+image)\s+(move|animate|come\s+alive)\b", re.I),
    re.compile(r"\b(video|animate)\b", re.I),
]

AI_RESPONSES: dict[str, str] = {
    "moodboard": "On it — pulling images for the moodboard now!",
    "diagram": "Starting the diagram on the canvas now!",
    "mockup": "Got it — sketching a mockup on the canvas!",
    "image": "Generating the image and placing it on the canvas now!",
    "plan": "Mapping out the plan on the canvas now!",
    "brainstorm": "Let's go! Placing ideas on the canvas now!",
    "addressed": "On it! Working on the canvas now…",
    "video_from_image": "Uploading your image and generating a video from it now!",
}


def _detect_intent(text: str) -> tuple[str | None, bool]:
    is_addressed = any(p.search(text) for p in _ADDRESS_PATTERNS)
    has_desire = any(p.search(text) for p in _DESIRE_PATTERNS)

    for pattern, task_type in _TASK_PATTERNS:
        if pattern.search(text):
            if has_desire or is_addressed:
                return task_type, is_addressed
            return None, is_addressed

    return None, is_addressed


def _is_video_request(text: str) -> bool:
    return any(p.search(text) for p in _VIDEO_PATTERNS)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def analyze_team_chat(room_id: str, username: str, text: str) -> None:
    if username == AI_USERNAME:
        return

    from room_manager import room_manager  # noqa: PLC0415

    # ── Special case: video from a recently shared chat image ──────────────
    if _is_video_request(text):
        last_image = room_manager.get_last_chat_image(room_id)
        if last_image:
            await asyncio.sleep(0.8)
            await _send_ai_chat(room_id, AI_RESPONSES["video_from_image"], room_manager)
            # Consume the stored image so it isn't reused accidentally
            room_manager.clear_last_chat_image(room_id)
            asyncio.create_task(
                _generate_video_from_chat_image(
                    room_id,
                    text,
                    last_image["data_url"],
                    room_manager,
                )
            )
            return

    # ── General creative intent ─────────────────────────────────────────────
    task_type, is_addressed = _detect_intent(text)
    if not task_type and not is_addressed:
        return

    await asyncio.sleep(1.2)

    reply = AI_RESPONSES.get(
        "addressed" if is_addressed and not task_type else task_type or "addressed",
        "On it! Working on the canvas now…",
    )
    await _send_ai_chat(room_id, reply, room_manager)

    canvas_state = room_manager.get_canvas(room_id)
    asyncio.create_task(_run_single_shot_agent(room_id, text, canvas_state, room_manager))


# ---------------------------------------------------------------------------
# Video-from-chat-image pipeline (no LLM needed)
# ---------------------------------------------------------------------------


async def _generate_video_from_chat_image(
    room_id: str,
    user_text: str,
    data_url: str,
    room_manager,
) -> None:
    from image_utils import upload_data_url  # noqa: PLC0415
    from higgsfield import submit_kling_generation  # noqa: PLC0415
    from agent.graph import _poll_generation  # noqa: PLC0415

    # 1. Upload to get public URL
    try:
        public_url = await upload_data_url(data_url)
        logger.info("room=%s chat image uploaded: %s", room_id, public_url)
    except Exception:
        logger.exception("room=%s failed to upload chat image", room_id)
        await _send_ai_chat(room_id, "Failed to upload the image. Please try again.", room_manager)
        return

    ts = int(time.time() * 1000)
    img_shape_id = f"chat-img-{ts}"
    vid_shape_id = f"chat-vid-{ts}"
    img_x, img_y = 100, 200
    vid_x, vid_y = 480, 200

    # 2. Place image on canvas
    await room_manager.broadcast_ai_cursor(room_id, img_x, img_y)
    await room_manager.broadcast(room_id, {
        "type": "agent_action",
        "action": {
            "_type": "create_image",
            "shapeId": img_shape_id,
            "url": public_url,
            "x": img_x, "y": img_y,
            "w": 320, "h": 220,
        },
    })
    await asyncio.sleep(0.5)

    # 3. Place video placeholder
    await room_manager.broadcast_ai_cursor(room_id, vid_x, vid_y)
    await room_manager.broadcast(room_id, {
        "type": "agent_action",
        "action": {
            "_type": "create_shape",
            "shapeId": vid_shape_id,
            "geo": "rectangle",
            "x": vid_x, "y": vid_y,
            "w": 320, "h": 220,
            "text": "Generating video…",
            "color": "violet",
        },
    })

    # 4. Extract motion prompt from user text (strip video-trigger words)
    motion_prompt = re.sub(
        r"\b(generate|create|make|animate|animation|video|clip|from|this|the|image|photo|picture|it|a)\b",
        " ", user_text, flags=re.I,
    ).strip()
    if len(motion_prompt) < 5:
        motion_prompt = "smooth cinematic camera motion"

    # 5. Submit video generation
    try:
        result = await submit_kling_generation(public_url, motion_prompt, duration=5)
        request_id = result["request_id"]
        logger.info("room=%s video generation started request_id=%s", room_id, request_id)
        asyncio.create_task(
            _poll_generation(room_id, vid_shape_id, vid_x, vid_y, 320, 220, request_id, "video")
        )
    except Exception:
        logger.exception("room=%s video generation failed", room_id)
        await room_manager.broadcast(room_id, {
            "type": "agent_action",
            "action": {
                "_type": "update_text",
                "id": f"shape:{vid_shape_id}",
                "text": "Video generation failed.",
            },
        })


# ---------------------------------------------------------------------------
# Single-shot canvas agent (one API call, JSON output, WebSocket broadcast)
# ---------------------------------------------------------------------------


async def _run_single_shot_agent(
    room_id: str,
    text: str,
    canvas_state: list,
    room_manager,
) -> None:
    """
    One Claude call with JSON assistant-prefill (guarantees JSON output).
    No LangGraph, no tool schemas — ~10x fewer tokens than LangGraph approach.
    """
    from anthropic import AsyncAnthropic, RateLimitError  # noqa: PLC0415
    from agent.prompts import CHAT_AGENT_SYSTEM, ACTION_SCHEMA  # noqa: PLC0415
    from agent.tools import format_canvas  # noqa: PLC0415
    from chat_agent import (  # noqa: PLC0415
        _is_moodboard_request,
        _moodboard_query,
        _format_pinterest_context,
        _close_and_parse_json,
    )
    from config import get_settings  # noqa: PLC0415

    settings = get_settings()

    # Pre-fetch Pinterest images for moodboard requests
    pinterest_images: list = []
    if _is_moodboard_request(text):
        query = _moodboard_query(text)
        if len(query) >= 3:
            from pinterest import fetch_pinterest_images  # noqa: PLC0415
            try:
                pinterest_images = await asyncio.to_thread(fetch_pinterest_images, query, 6)
                logger.info("room=%s pinterest fetched %d images for '%s'", room_id, len(pinterest_images), query)
            except Exception:
                logger.exception("room=%s pinterest fetch failed", room_id)

    context_blocks = [format_canvas(canvas_state)]
    image_context = _format_pinterest_context(pinterest_images)
    if image_context:
        context_blocks.append(image_context)

    system = CHAT_AGENT_SYSTEM.format(
        canvas="\n\n".join(context_blocks),
        schema=ACTION_SCHEMA,
    )

    # Assistant prefill forces JSON output — Haiku reliably follows it
    PREFILL = '{"actions": ['
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        response = await client.messages.create(
            model=_AGENT_MODEL,
            max_tokens=2048,
            system=system,
            messages=[
                {"role": "user", "content": text},
                {"role": "assistant", "content": PREFILL},
            ],
        )
    except RateLimitError:
        logger.warning("room=%s rate-limited on team-chat agent", room_id)
        await _send_ai_chat(room_id, "I'm rate-limited right now — try again in a moment!", room_manager)
        return
    except Exception:
        logger.exception("room=%s team-chat agent API call failed", room_id)
        await _send_ai_chat(room_id, "Something went wrong on my end. Please try again.", room_manager)
        return

    raw = PREFILL + (response.content[0].text if response.content else "")
    parsed = _close_and_parse_json(raw)
    if not parsed:
        logger.warning("room=%s agent JSON parse failed: %r", room_id, raw[:200])
        return

    actions: list[dict] = parsed.get("actions", [])
    logger.info("room=%s single-shot agent produced %d actions", room_id, len(actions))

    for action in actions:
        action_type = action.get("_type", "")
        if action_type == "message":
            msg_text = (action.get("text") or "").strip()
            if msg_text:
                await _send_ai_chat(room_id, msg_text, room_manager)
        else:
            x = float(action.get("x") or action.get("x1") or 200)
            y = float(action.get("y") or action.get("y1") or 200)
            await room_manager.broadcast_ai_cursor(room_id, x, y)
            await room_manager.broadcast(room_id, {"type": "agent_action", "action": action})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _send_ai_chat(room_id: str, content: str, room_manager) -> None:
    await room_manager.broadcast(room_id, {
        "type": "chat_message",
        "username": AI_USERNAME,
        "content": content,
        "msgType": "text",
        "ts": int(time.time() * 1000),
        "isAI": True,
    })
    logger.info("room=%s AI chat: %s", room_id, content[:80])
