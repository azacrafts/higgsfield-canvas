"""
context.api
===========

FastAPI router for the context-aware canvas agent.

Endpoint
--------
    POST /api/agent/stream

    Request body:
        {
            "message":         str,
            "canvas_snapshot": {
                "shapes":       list[dict],   # all tldraw shapes
                "viewport":     dict,         # {x, y, w, h}
                "selected_ids": list[str]     # currently selected IDs
            },
            "room_id":         str            # default: "default"
        }

    Response (text/event-stream):
        data: {"type": "action", "action": {...}}\n\n   (one per action)
        data: {"type": "done"}\n\n

SSE streaming is "batch" style: the full LangGraph graph runs first, then
the collected actions are streamed back so the frontend processes them in
order without waiting for individual graph nodes.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from context.graph import run_context_agent

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class AgentRequest(BaseModel):
    message: str
    canvas_snapshot: dict[str, Any] = Field(default_factory=dict)
    room_id: str = "main"


# ---------------------------------------------------------------------------
# SSE generator
# ---------------------------------------------------------------------------


async def _stream_actions(
    message: str,
    canvas_snapshot: dict[str, Any],
    room_id: str,
) -> AsyncIterator[str]:
    """Run the context-aware graph and yield each action as an SSE event."""
    try:
        actions = await run_context_agent(
            message=message,
            canvas_snapshot=canvas_snapshot,
            room_id=room_id,
        )
        for action in actions:
            payload = json.dumps({"type": "action", "action": action})
            yield f"data: {payload}\n\n"
    except Exception as exc:
        logger.exception("agent stream error for room=%s: %s", room_id, exc)
        error_payload = json.dumps({"type": "error", "message": str(exc)})
        yield f"data: {error_payload}\n\n"
    finally:
        yield 'data: {"type": "done"}\n\n'


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/api/agent/stream")
async def agent_stream(body: AgentRequest):
    """Invoke the context-aware agent and stream its actions as SSE."""
    logger.info(
        "agent/stream: room=%s message=%r shapes=%d",
        body.room_id,
        body.message[:60],
        len(body.canvas_snapshot.get("shapes", [])),
    )
    return StreamingResponse(
        _stream_actions(body.message, body.canvas_snapshot, body.room_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
