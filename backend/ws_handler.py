"""
WebSocket connection handler.

Thin dispatcher: accepts the connection, joins the room, then loops over
incoming messages and delegates to room_manager and voice_pipeline.
"""

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from room_manager import room_manager
from voice_pipeline import handle_audio_chunk

logger = logging.getLogger(__name__)


async def handle_websocket(ws: WebSocket, room_id: str, username: str) -> None:
    await ws.accept()
    await room_manager.join(room_id, username, ws)

    # Tell the joining user who is already in the room so they can initiate offers.
    existing_peers = [u for u in room_manager.users_in_room(room_id) if u != username]
    await ws.send_text(json.dumps({"type": "existing_peers", "peers": existing_peers}))

    # Restore canvas for the new user.
    snapshot = room_manager._canvas_snapshot.get(room_id)
    if snapshot:
        try:
            await ws.send_text(json.dumps({"type": "canvas_restore_full", "snapshot": snapshot}))
        except Exception:
            logger.warning("room=%s failed to send canvas_restore_full to %s", room_id, username)
    elif room_manager.get_canvas(room_id):
        try:
            await ws.send_text(
                json.dumps({"type": "canvas_snapshot", "shapes": room_manager.get_canvas(room_id)})
            )
        except Exception:
            logger.warning("room=%s failed to send canvas_snapshot to %s", room_id, username)

    try:
        while True:
            raw = await ws.receive_text()
            msg: dict[str, Any] = json.loads(raw)
            await _dispatch(ws, room_id, username, msg)
    except WebSocketDisconnect:
        pass
    finally:
        await room_manager.leave(room_id, username)


async def _dispatch(
    ws: WebSocket, room_id: str, username: str, msg: dict[str, Any]
) -> None:
    t = msg.get("type")

    # --- WebRTC signalling ---
    if t in ("offer", "answer", "ice"):
        to = msg.get("to")
        if to:
            await room_manager.relay(room_id, to, {**msg, "from": username})

    # --- Audio ---
    elif t == "audio_chunk":
        audio_b64: str = msg.get("data", "")
        if audio_b64:
            asyncio.create_task(handle_audio_chunk(room_id, username, audio_b64))

    # --- Real-time audio relay (bypasses WebRTC) ---
    elif t == "audio_relay":
        await room_manager.broadcast(room_id, msg, exclude=username)

    # --- Cursor tracking ---
    elif t == "cursor_move":
        await room_manager.broadcast(room_id, {**msg, "username": username}, exclude=username)

    # --- Canvas state (simplified shapes for AI context) ---
    elif t == "canvas_state":
        room_manager.set_canvas(room_id, msg.get("state", []))

    # --- Full tldraw snapshot (for perfect restore on join + live sync) ---
    elif t == "canvas_snapshot_full":
        snapshot = msg.get("snapshot")
        if snapshot:
            room_manager.set_canvas_snapshot(room_id, snapshot)
            # Broadcast to everyone else so their canvas stays in sync
            await room_manager.broadcast(
                room_id,
                {"type": "canvas_restore_full", "snapshot": snapshot},
                exclude=username,
            )

    # --- HITL confirmation response ---
    elif t == "confirm_response":
        req_id: str = msg.get("request_id", "")
        if req_id and req_id in room_manager._confirmations:
            room_manager._confirm_results[req_id] = bool(msg.get("approved", False))
            room_manager._confirmations[req_id].set()

    # --- Team chat message ---
    elif t == "chat_message":
        content: str = msg.get("content", "")
        msg_type: str = msg.get("msgType", "text")
        ts: int = msg.get("ts") or int(time.time() * 1000)
        payload = {
            "type": "chat_message",
            "username": username,
            "content": content,
            "msgType": msg_type,
            "ts": ts,
        }
        # Broadcast to ALL users including sender (sender uses server echo as source of truth)
        await room_manager.broadcast(room_id, payload)
        if msg_type == "image" and content.startswith("data:"):
            # Store the shared image so the AI can use it for video generation etc.
            room_manager.set_last_chat_image(room_id, content, username)
        elif msg_type == "text" and content.strip():
            from team_chat_agent import analyze_team_chat  # noqa: PLC0415
            asyncio.create_task(analyze_team_chat(room_id, username, content))
