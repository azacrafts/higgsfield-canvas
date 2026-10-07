"""
Audio processing pipeline: Whisper transcription → wake-word detection →
command accumulation → canvas agent invocation.

All per-room accumulation state lives in RoomManager to keep it co-located
with the rest of the room data.
"""

import asyncio
import logging
import re
import time

from room_manager import room_manager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HIGGS_VARIANTS: frozenset[str] = frozenset(
    {"higgs", "higs", "highs", "hicks", "fix", "hix", "higg", "his", "hex"}
)

COMMAND_KEYWORDS: frozenset[str] = frozenset(
    {
        # create
        "create", "draw", "make", "add", "build", "show", "generate",
        "diagram", "flowchart", "mindmap", "uml", "chart", "note", "sticky",
        # modify / delete
        "remove", "delete", "move", "update", "edit", "rename", "change",
        "connect", "link", "arrow", "label", "clear",
        # visual / media
        "moodboard", "mood", "vibe", "aesthetic", "inspiration", "inspiring",
        "image", "images", "picture", "pictures", "photo", "photos",
        "video", "animate", "animation", "fetch", "find", "search",
        "pinterest", "reference", "references", "visual", "visuals",
        "illustration", "render", "portrait", "landscape",
    }
)

_WAKE_WORD_RE = re.compile(r"\b(higgs|higs|highs|hicks)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _has_wake_word(text: str) -> bool:
    words = set(re.sub(r"[^a-z ]", "", text.lower()).split())
    return bool(words & HIGGS_VARIANTS) or bool(set(text.lower().split()) & HIGGS_VARIANTS)


def _has_command_keyword(text: str) -> bool:
    return bool(set(text.lower().split()) & COMMAND_KEYWORDS)


def _strip_wake_word(text: str) -> str:
    cleaned = _WAKE_WORD_RE.sub("", text).strip(" ,.")
    return cleaned or text


def _reset_flush_timer(room_id: str) -> None:
    existing = room_manager._pending_task.get(room_id)
    if existing and not existing.done():
        existing.cancel()
    room_manager._pending_task[room_id] = asyncio.create_task(_flush_command(room_id))


# ---------------------------------------------------------------------------
# Command flush
# ---------------------------------------------------------------------------


async def _flush_command(room_id: str) -> None:
    """Wait for the accumulation window then fire the canvas agent."""
    from config import get_settings  # noqa: PLC0415

    await asyncio.sleep(get_settings().command_flush_delay_s)
    command = room_manager._pending_command.pop(room_id, "")
    if command:
        await _invoke_canvas_agent(room_id, command)


async def _invoke_canvas_agent(room_id: str, command: str) -> None:
    clean = _strip_wake_word(command)
    logger.info("room=%s invoking agent cmd='%s'", room_id, clean)
    canvas_state = room_manager.get_canvas(room_id)
    from agent.graph import run_canvas_agent  # noqa: PLC0415

    try:
        await run_canvas_agent(clean, canvas_state, room_id)
    except Exception:
        logger.exception("room=%s agent error", room_id)


# ---------------------------------------------------------------------------
# Classifier path (async, runs in thread)
# ---------------------------------------------------------------------------


async def _classify_and_maybe_accumulate(room_id: str, text: str) -> None:
    from intent import is_canvas_command  # noqa: PLC0415

    try:
        is_command = await asyncio.to_thread(is_canvas_command, text)
    except Exception:
        logger.exception("classifier error for text='%s'", text[:60])
        return
    logger.info("classifier '%s' → %s", text[:60], "YES" if is_command else "NO")
    if is_command:
        room_manager._pending_command[room_id] = text
        _reset_flush_timer(room_id)


# ---------------------------------------------------------------------------
# Persistence helper
# ---------------------------------------------------------------------------


async def _save_transcript(room_id: str, username: str, text: str) -> None:
    try:
        from db import save_transcript  # noqa: PLC0415

        await save_transcript(room_id, username, text, time.time())
    except Exception:
        logger.exception("transcript save error room=%s", room_id)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def handle_audio_chunk(room_id: str, username: str, audio_b64: str) -> None:
    """Transcribe audio, broadcast transcript, and trigger agent if warranted."""
    from whisper_transcribe import transcribe  # noqa: PLC0415

    try:
        text = await asyncio.to_thread(transcribe, audio_b64)
    except Exception:
        logger.exception("whisper error user=%s", username)
        return
    if not text:
        return

    logger.info("transcript user=%s: %s", username, text)
    await room_manager.broadcast(
        room_id,
        {"type": "transcript", "username": username, "text": text},
    )
    room_manager.get_buffer(room_id).add(username, text)
    asyncio.create_task(_save_transcript(room_id, username, text))

    # --- Command accumulation ---

    # If already accumulating: append and reset flush timer.
    if room_id in room_manager._pending_command:
        room_manager._pending_command[room_id] += " " + text
        _reset_flush_timer(room_id)
        return

    # Wake-word fast path (no LLM call needed).
    if _has_wake_word(text):
        room_manager._pending_command[room_id] = text
        _reset_flush_timer(room_id)
        return

    # Keyword gate: only run classifier when there are plausible command words.
    if _has_command_keyword(text):
        asyncio.create_task(_classify_and_maybe_accumulate(room_id, text))
