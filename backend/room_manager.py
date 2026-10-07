"""
Room state management.

RoomManager owns all per-room state: WebSocket connections, conversation
buffers, canvas state, and persistence scheduling.  It does NOT import from
voice_pipeline or agent — dependency only flows inward.
"""

import asyncio
import json
import logging
import time
from fastapi import WebSocket

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conversation buffer
# ---------------------------------------------------------------------------


class ConversationBuffer:
    SILENCE_THRESHOLD_SEC = 3.0
    MIN_EXCHANGES = 1

    def __init__(self) -> None:
        self._entries: list[dict] = []  # {username, text, ts}
        self._triggered = False

    def add(self, username: str, text: str) -> None:
        self._entries.append({"username": username, "text": text, "ts": time.time()})
        self._triggered = False  # new speech resets trigger guard

    def should_trigger(self) -> bool:
        if self._triggered or len(self._entries) < self.MIN_EXCHANGES:
            return False
        return (time.time() - self._entries[-1]["ts"]) >= self.SILENCE_THRESHOLD_SEC

    def mark_triggered(self) -> None:
        self._triggered = True

    def format(self) -> str:
        return "\n".join(f"{e['username']}: {e['text']}" for e in self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self._triggered = False


# ---------------------------------------------------------------------------
# Room manager
# ---------------------------------------------------------------------------


class RoomManager:
    def __init__(self) -> None:
        # room_id → {username: WebSocket}
        self._rooms: dict[str, dict[str, WebSocket]] = {}
        # room_id → ConversationBuffer
        self._buffers: dict[str, ConversationBuffer] = {}
        # Simplified CanvasShape list for AI context
        self._canvas: dict[str, list[dict]] = {}
        # Full tldraw snapshot for perfect canvas restore on join
        self._canvas_snapshot: dict[str, dict | None] = {}
        # Pending wake-word command text being accumulated per room
        self._pending_command: dict[str, str] = {}
        # Asyncio task waiting to flush the accumulated command
        self._pending_task: dict[str, asyncio.Task] = {}
        # Asyncio task for debounced DB save
        self._save_tasks: dict[str, asyncio.Task] = {}
        # HITL: request_id → asyncio.Event
        self._confirmations: dict[str, asyncio.Event] = {}
        # HITL: request_id → approved bool
        self._confirm_results: dict[str, bool] = {}
        # Last image shared in team chat: room_id → {"data_url": str, "username": str}
        self._last_chat_images: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Room lifecycle
    # ------------------------------------------------------------------

    async def _ensure_room(self, room_id: str) -> None:
        if room_id in self._rooms:
            return
        self._rooms[room_id] = {}
        self._buffers[room_id] = ConversationBuffer()
        from db import load_room  # noqa: PLC0415

        data = await load_room(room_id)
        self._canvas[room_id] = data["shapes"]
        self._canvas_snapshot[room_id] = data["snapshot"]
        logger.debug("room %s initialised (shapes=%d)", room_id, len(self._canvas[room_id]))

    async def join(self, room_id: str, username: str, ws: WebSocket) -> None:
        await self._ensure_room(room_id)
        self._rooms[room_id][username] = ws
        await self._broadcast_room_update(room_id)
        logger.info("room=%s user=%s joined (%d users)", room_id, username, len(self._rooms[room_id]))

    async def leave(self, room_id: str, username: str) -> None:
        room = self._rooms.get(room_id, {})
        room.pop(username, None)
        if not room:
            self._rooms.pop(room_id, None)
            self._buffers.pop(room_id, None)
            # Canvas kept in memory for fast re-join; DB has the persisted copy.
        else:
            await self._broadcast_room_update(room_id)
        logger.info("room=%s user=%s left", room_id, username)

    def users_in_room(self, room_id: str) -> list[str]:
        return list(self._rooms.get(room_id, {}).keys())

    # ------------------------------------------------------------------
    # Canvas state
    # ------------------------------------------------------------------

    def get_canvas(self, room_id: str) -> list[dict]:
        return self._canvas.get(room_id, [])

    def set_canvas(self, room_id: str, canvas_state: list[dict]) -> None:
        self._canvas[room_id] = canvas_state
        self.schedule_save(room_id)

    def set_canvas_snapshot(self, room_id: str, snapshot: dict) -> None:
        self._canvas_snapshot[room_id] = snapshot
        self.schedule_save(room_id)

    # ------------------------------------------------------------------
    # Persistence (debounced)
    # ------------------------------------------------------------------

    def schedule_save(self, room_id: str) -> None:
        existing = self._save_tasks.get(room_id)
        if existing and not existing.done():
            existing.cancel()
        self._save_tasks[room_id] = asyncio.create_task(self._deferred_save(room_id))

    async def _deferred_save(self, room_id: str) -> None:
        from config import get_settings  # noqa: PLC0415

        await asyncio.sleep(get_settings().db_save_debounce_s)
        from db import save_canvas  # noqa: PLC0415

        try:
            await save_canvas(
                room_id,
                self._canvas.get(room_id, []),
                self._canvas_snapshot.get(room_id),
            )
            logger.debug("room=%s canvas saved to db", room_id)
        except Exception:
            logger.exception("room=%s db save failed", room_id)

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    async def relay(self, room_id: str, to: str, msg: dict) -> None:
        ws = self._rooms.get(room_id, {}).get(to)
        if ws:
            await ws.send_text(json.dumps(msg))

    async def broadcast(
        self, room_id: str, msg: dict, exclude: str | None = None
    ) -> None:
        room = self._rooms.get(room_id, {})
        dead: list[str] = []
        for username, ws in room.items():
            if username == exclude:
                continue
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                logger.warning("room=%s broadcast to %s failed", room_id, username)
                dead.append(username)
        for u in dead:
            room.pop(u, None)

    async def _broadcast_room_update(self, room_id: str) -> None:
        users = [{"username": u} for u in self._rooms.get(room_id, {})]
        # AI agent is always present as a virtual member
        users.append({"username": "Higgs AI", "isAI": True})
        await self.broadcast(room_id, {"type": "room_update", "users": users})

    def set_last_chat_image(self, room_id: str, data_url: str, username: str) -> None:
        self._last_chat_images[room_id] = {"data_url": data_url, "username": username}

    def get_last_chat_image(self, room_id: str) -> dict | None:
        return self._last_chat_images.get(room_id)

    def clear_last_chat_image(self, room_id: str) -> None:
        self._last_chat_images.pop(room_id, None)

    async def broadcast_ai_cursor(self, room_id: str, x: float, y: float) -> None:
        """Broadcast AI agent cursor position to all room members."""
        await self.broadcast(room_id, {
            "type": "cursor_move",
            "username": "Higgs AI",
            "x": x,
            "y": y,
        })

    # ------------------------------------------------------------------
    # Conversation buffer
    # ------------------------------------------------------------------

    def get_buffer(self, room_id: str) -> ConversationBuffer:
        return self._buffers[room_id]


# Module-level singleton — imported by graph.py and ws_handler.py.
room_manager = RoomManager()
