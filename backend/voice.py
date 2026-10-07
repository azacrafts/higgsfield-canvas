import asyncio
import json
import time
import uuid
from typing import Any
from fastapi import WebSocket, WebSocketDisconnect


COLOR_MAP = {
    "purple": "violet", "pink": "light-violet", "gray": "grey",
    "light-gray": "grey", "lightgray": "grey", "dark-blue": "blue",
    "light-purple": "light-violet", "teal": "green", "cyan": "light-blue",
    "lime": "light-green", "salmon": "light-red", "brown": "orange",
}


def _apply_optimistic(canvas: list[dict], action: dict):
    """Update the in-memory canvas after emitting an agent action."""
    t = action.get("_type", "")
    if t in ("create_shape", "create_note", "create_text", "create_arrow"):
        canvas.append({
            "id": action.get("shapeId", action.get("id", "")),
            "type": {"create_note": "note", "create_text": "text",
                     "create_arrow": "arrow"}.get(t, "geo"),
            "x": action.get("x", action.get("x1", 0)),
            "y": action.get("y", action.get("y1", 0)),
            "w": action.get("w", 200), "h": action.get("h", 100),
            "text": action.get("text", ""), "color": action.get("color", ""),
            "geo": action.get("geo", "rectangle"),
        })
    elif t == "delete_shape":
        sid = action.get("id") or action.get("shapeId")
        canvas[:] = [s for s in canvas if s.get("id") != sid]
    elif t == "move_shape":
        for s in canvas:
            if s.get("id") == action.get("id"):
                s["x"] = action.get("x", s["x"])
                s["y"] = action.get("y", s["y"])


class ConversationBuffer:
    SILENCE_THRESHOLD_SEC = 3.0
    MIN_EXCHANGES = 1

    def __init__(self):
        self._entries: list[dict] = []  # {username, text, ts}
        self._triggered = False

    def add(self, username: str, text: str):
        self._entries.append({"username": username, "text": text, "ts": time.time()})
        self._triggered = False  # new speech resets trigger guard

    def should_trigger(self) -> bool:
        if self._triggered:
            return False
        if len(self._entries) < self.MIN_EXCHANGES:
            return False
        elapsed = time.time() - self._entries[-1]["ts"]
        return elapsed >= self.SILENCE_THRESHOLD_SEC

    def mark_triggered(self):
        self._triggered = True

    def format(self) -> str:
        return "\n".join(f"{e['username']}: {e['text']}" for e in self._entries)

    def clear(self):
        self._entries.clear()
        self._triggered = False


class RoomManager:
    def __init__(self):
        # room_id → {username: WebSocket}
        self._rooms: dict[str, dict[str, WebSocket]] = {}
        # room_id → ConversationBuffer
        self._buffers: dict[str, ConversationBuffer] = {}
        # room_id → canvas_state (simplified CanvasShape list for AI context)
        self._canvas: dict[str, list[dict]] = {}
        # room_id → full tldraw snapshot (for perfect canvas restore)
        self._canvas_snapshot: dict[str, dict | None] = {}
        # room_id → pending wake word command being accumulated
        self._pending_command: dict[str, str] = {}
        # room_id → asyncio task waiting to flush the command
        self._pending_task: dict[str, asyncio.Task] = {}
        # room_id → asyncio task for deferred DB save
        self._save_tasks: dict[str, asyncio.Task] = {}
        # HITL: request_id → asyncio.Event
        self._confirmations: dict[str, asyncio.Event] = {}
        # HITL: request_id → approved bool
        self._confirm_results: dict[str, bool] = {}

    async def _ensure_room(self, room_id: str):
        if room_id not in self._rooms:
            self._rooms[room_id] = {}
            self._buffers[room_id] = ConversationBuffer()
            from db import load_room
            data = await load_room(room_id)
            self._canvas[room_id] = data["shapes"]
            self._canvas_snapshot[room_id] = data["snapshot"]

    def _schedule_save(self, room_id: str):
        existing = self._save_tasks.get(room_id)
        if existing and not existing.done():
            existing.cancel()
        self._save_tasks[room_id] = asyncio.create_task(self._deferred_save(room_id))

    async def _deferred_save(self, room_id: str):
        await asyncio.sleep(2.0)
        from db import save_canvas
        await save_canvas(
            room_id,
            self._canvas.get(room_id, []),
            self._canvas_snapshot.get(room_id),
        )

    def get_buffer(self, room_id: str) -> ConversationBuffer:
        return self._buffers[room_id]

    def get_canvas(self, room_id: str) -> list[dict]:
        return self._canvas.get(room_id, [])

    def set_canvas(self, room_id: str, canvas_state: list[dict]):
        self._canvas[room_id] = canvas_state
        self._schedule_save(room_id)

    def set_canvas_snapshot(self, room_id: str, snapshot: dict):
        self._canvas_snapshot[room_id] = snapshot
        self._schedule_save(room_id)

    async def join(self, room_id: str, username: str, ws: WebSocket):
        await self._ensure_room(room_id)
        self._rooms[room_id][username] = ws
        await self._broadcast_room_update(room_id)

    async def leave(self, room_id: str, username: str):
        room = self._rooms.get(room_id, {})
        room.pop(username, None)
        if not room:
            self._rooms.pop(room_id, None)
            self._buffers.pop(room_id, None)
            # Keep canvas + snapshot in memory for fast rejoin; DB has persisted copy
        else:
            await self._broadcast_room_update(room_id)

    async def relay(self, room_id: str, to: str, msg: dict):
        room = self._rooms.get(room_id, {})
        ws = room.get(to)
        if ws:
            await ws.send_text(json.dumps(msg))

    async def broadcast(self, room_id: str, msg: dict, exclude: str | None = None):
        room = self._rooms.get(room_id, {})
        dead = []
        for username, ws in room.items():
            if username == exclude:
                continue
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                dead.append(username)
        for u in dead:
            room.pop(u, None)

    async def _broadcast_room_update(self, room_id: str):
        room = self._rooms.get(room_id, {})
        users = [{"username": u} for u in room]
        await self.broadcast(room_id, {"type": "room_update", "users": users})

    def users_in_room(self, room_id: str) -> list[str]:
        return list(self._rooms.get(room_id, {}).keys())


room_manager = RoomManager()


async def _handle_audio(room_id: str, username: str, audio_b64: str):
    """Transcribe an audio chunk via Whisper and process the result."""
    from whisper_transcribe import transcribe
    try:
        text = await asyncio.to_thread(transcribe, audio_b64)
    except Exception as e:
        print(f"Whisper error for {username}: {e}")
        return
    if not text:
        return
    print(f"[transcript] {username}: {text}")
    # Broadcast to all in room so everyone sees the transcript
    await room_manager.broadcast(
        room_id,
        {"type": "transcript", "username": username, "text": text},
    )
    room_manager.get_buffer(room_id).add(username, text)

    # Persist transcript
    asyncio.create_task(_save_transcript(room_id, username, text))

    if room_id in room_manager._pending_command:
        # Already triggered — append continuation chunk and reset flush timer
        room_manager._pending_command[room_id] += " " + text
        existing = room_manager._pending_task.get(room_id)
        if existing and not existing.done():
            existing.cancel()
        room_manager._pending_task[room_id] = asyncio.create_task(_flush_command(room_id))
        return

    import re
    words = set(re.sub(r"[^a-z ]", "", text.lower()).split())

    # Fast path: wake word variants (free, instant)
    HIGGS_VARIANTS = {"higgs", "higs", "highs", "hicks", "fix", "hix", "higg", "his", "hex",
                      "хиггс", "хигс", "хикс", "хиг"}
    if any(w in HIGGS_VARIANTS for w in words) or any(w in HIGGS_VARIANTS for w in text.lower().split()):
        room_manager._pending_command[room_id] = text
        existing = room_manager._pending_task.get(room_id)
        if existing and not existing.done():
            existing.cancel()
        room_manager._pending_task[room_id] = asyncio.create_task(_flush_command(room_id))
        return

    # Fallback: only run classifier if transcript has strong command keywords
    COMMAND_KEYWORDS = {
        # English
        "create", "draw", "make", "add", "build", "show", "generate",
        "diagram", "flowchart", "mindmap", "uml", "chart", "note", "sticky",
        # Russian
        "создай", "создать", "нарисуй", "нарисовать", "добавь", "добавить",
        "сделай", "сделать", "покажи", "построй", "диаграмму", "диаграмма",
        "схему", "схема", "заметку", "заметка", "граф",
    }
    text_words = set(text.lower().split())
    if any(w in COMMAND_KEYWORDS for w in text_words):
        asyncio.create_task(_classify_and_maybe_trigger(room_id, text))


async def _save_transcript(room_id: str, username: str, text: str):
    try:
        from db import save_transcript
        await save_transcript(room_id, username, text, time.time())
    except Exception as e:
        print(f"[db] transcript save error: {e}")


async def _classify_and_maybe_trigger(room_id: str, text: str):
    """Run LLM classifier; if it's a canvas command, start accumulation window."""
    from intent import is_canvas_command
    try:
        is_command = await asyncio.to_thread(is_canvas_command, text)
    except Exception as e:
        print(f"[classifier] error: {e}")
        return
    print(f"[classifier] '{text[:60]}' → {'YES' if is_command else 'NO'}")
    if is_command:
        room_manager._pending_command[room_id] = text
        existing = room_manager._pending_task.get(room_id)
        if existing and not existing.done():
            existing.cancel()
        room_manager._pending_task[room_id] = asyncio.create_task(_flush_command(room_id))


async def _flush_command(room_id: str):
    """Wait one extra chunk interval, then fire the listener with accumulated command."""
    await asyncio.sleep(3.5)
    command = room_manager._pending_command.pop(room_id, "")
    if command:
        await _run_listener_now(room_id, command)


async def _run_listener_now(room_id: str, command: str):
    """Strip wake word and pass directly to the LangGraph canvas agent."""
    import re
    canvas_state = room_manager.get_canvas(room_id)
    # Simple wake-word strip — Sonnet handles the rest
    clean = re.sub(
        r'\b(higgs|higs|highs|hicks|хиггс|хигс|хикс|хиг)\b', '',
        command, flags=re.IGNORECASE
    ).strip(' ,.')
    if not clean:
        clean = command
    print(f"[listener] cmd: '{clean}'")

    from listener import listener_agent_react
    try:
        await listener_agent_react(clean, canvas_state, room_id)
    except Exception as e:
        print(f"[listener] error: {e}")


async def handle_websocket(ws: WebSocket, room_id: str, username: str):
    await ws.accept()
    await room_manager.join(room_id, username, ws)

    # Notify the new user of existing peers so they can initiate offers
    existing = [u for u in room_manager.users_in_room(room_id) if u != username]
    await ws.send_text(json.dumps({"type": "existing_peers", "peers": existing}))

    # Send persisted canvas to new user
    snapshot = room_manager._canvas_snapshot.get(room_id)
    if snapshot:
        # Full tldraw restore — preferred path
        try:
            await ws.send_text(json.dumps({"type": "canvas_restore_full", "snapshot": snapshot}))
        except Exception:
            pass
    elif room_manager._canvas.get(room_id):
        # Fallback: relay agent-created shapes as individual actions
        try:
            await ws.send_text(json.dumps({
                "type": "canvas_snapshot",
                "shapes": room_manager._canvas[room_id],
            }))
        except Exception:
            pass

    try:
        while True:
            raw = await ws.receive_text()
            msg: dict[str, Any] = json.loads(raw)
            t = msg.get("type")

            if t in ("offer", "answer", "ice"):
                to = msg.get("to")
                if to:
                    await room_manager.relay(room_id, to, {**msg, "from": username})

            elif t == "audio_chunk":
                audio_b64 = msg.get("data", "")
                if audio_b64:
                    asyncio.create_task(_handle_audio(room_id, username, audio_b64))

            elif t == "canvas_state":
                # Simplified shape list from frontend — update AI context
                room_manager.set_canvas(room_id, msg.get("state", []))

            elif t == "canvas_snapshot_full":
                # Full tldraw snapshot — store for new-user restore
                snapshot = msg.get("snapshot")
                if snapshot:
                    room_manager.set_canvas_snapshot(room_id, snapshot)

            elif t == "confirm_response":
                req_id = msg.get("request_id")
                if req_id and req_id in room_manager._confirmations:
                    room_manager._confirm_results[req_id] = bool(msg.get("approved", False))
                    room_manager._confirmations[req_id].set()

    except WebSocketDisconnect:
        pass
    finally:
        await room_manager.leave(room_id, username)
