"""
context.graph
=============

LangGraph StateGraph for the context-aware canvas agent.

Graph topology
--------------

    START
      │
      ▼
    prepare          ← diff + preprocess + event logging (always runs)
      │
      ├─ needs_summary_update=True  ──→  summary_update
      │                                        │
      └─ needs_summary_update=False ──────────┤
                                              ▼
                                          context      ← assemble ContextPacket
                                              │
                                              ▼
                                           agent       ← Claude call + action parsing
                                              │
                                              ▼
                                             log        ← write to EventLog + SessionMemory
                                              │
                                              ▼
                                             END

Key design decisions
--------------------
• prepare_node merges diff + preprocessing (simpler than two separate nodes).
• summary_node is conditional — triggers only after ≥ 5 new events (configurable).
• The Claude call in agent_node uses async streaming internally so tokens are
  collected as they arrive; the final action list is returned to state.
• Action validation runs inside agent_node before returning state.
• log_node has no side-effects on the graph state — it writes to the shared
  context_store and returns an empty dict.

Public entry point
------------------
    run_context_agent(message, canvas_snapshot, room_id) -> list[dict]
        Invokes the compiled graph and returns the validated agent_actions list.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

from anthropic import AsyncAnthropic
from langgraph.graph import END, StateGraph

from config import get_settings
from context.assembly import build_context_packet
from context.diff import apply_diff_to_registry, diff_canvas
from context.graph_state import ContextAwareAgentState
from context.models import CanvasEvent, EventType
from context.preprocessors import preprocess_shape
from context.prompt_builder import build_messages
from context.session_updater import update_session_summary
from context.storage import context_store

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy Anthropic client
# ---------------------------------------------------------------------------

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=get_settings().anthropic_api_key)
    return _client


def _context_debug_enabled() -> bool:
    return get_settings().context_debug


def _debug_log(message: str, *args: object) -> None:
    if _context_debug_enabled():
        logger.info(message, *args)


# ---------------------------------------------------------------------------
# JSON streaming helper (mirrors _close_and_parse_json in chat_agent.py)
# ---------------------------------------------------------------------------


def _parse_json(s: str) -> dict | None:
    """Parse a potentially incomplete JSON string by closing open brackets."""
    stack: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        last = stack[-1] if stack else None
        if ch == '"':
            if i > 0 and s[i - 1] == "\\":
                i += 1
                continue
            stack.pop() if last == '"' else stack.append('"')
        if last == '"':
            i += 1
            continue
        if ch in ("{", "["):
            stack.append(ch)
        elif ch == "}" and last == "{":
            stack.pop()
        elif ch == "]" and last == "[":
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
# Action validation
# ---------------------------------------------------------------------------


def _validate_actions(
    actions: list[dict[str, Any]],
    existing_ids: set[str],
) -> list[dict[str, Any]]:
    """Drop or repair actions that reference non-existent shape IDs.

    Rules
    -----
    • move_shape / update_text / delete_shape: if the target id isn't on
      the canvas AND wasn't created earlier in this action batch, drop it.
    • create_arrow: if fromId or toId doesn't resolve, remove that binding
      rather than dropping the whole arrow.
    • All other actions pass through unchanged.
    """
    created_ids: set[str] = set()
    valid: list[dict[str, Any]] = []

    for action in actions:
        t = action.get("_type", "")

        # Track newly created shape IDs so later actions can reference them.
        if t in ("create_shape", "create_note", "create_text",
                 "create_image", "create_arrow"):
            sid = action.get("shapeId", "")
            if sid:
                created_ids.add(sid)

        all_ids = existing_ids | created_ids

        if t in ("move_shape", "update_text", "delete_shape"):
            sid = action.get("id") or action.get("shapeId", "")
            if sid and sid not in all_ids:
                logger.warning(
                    "dropping %s: shape %r not on canvas", t, sid
                )
                continue

        elif t == "create_arrow":
            action = dict(action)  # shallow copy before mutating
            from_id = action.get("fromId", "")
            to_id = action.get("toId", "")
            if from_id and from_id not in all_ids:
                logger.warning("create_arrow: removing invalid fromId %r", from_id)
                action.pop("fromId", None)
            if to_id and to_id not in all_ids:
                logger.warning("create_arrow: removing invalid toId %r", to_id)
                action.pop("toId", None)

        valid.append(action)

    return valid


# ---------------------------------------------------------------------------
# Node 1: prepare (diff + preprocess + event logging)
# ---------------------------------------------------------------------------


async def prepare_node(state: ContextAwareAgentState) -> dict:
    """Diff the incoming canvas snapshot against the registry, preprocess
    any new or updated shapes, and log canvas-change events.

    Also determines whether a session summary refresh is warranted.
    """
    room_id = state["room_id"]
    room = context_store.get_room(room_id)
    registry_before = len(room.registry)

    snapshot_shapes: list[dict] = state.get("canvas_snapshot", {}).get("shapes", [])

    # ---- Diff ----------------------------------------------------------------
    diff = diff_canvas(snapshot_shapes, room.registry)

    # Apply position updates and deletions immediately (no preprocessing needed).
    apply_diff_to_registry(diff, room.registry)

    if any([diff.new_shapes, diff.updated_shapes, diff.moved_shapes, diff.deleted_ids]):
        _debug_log(
            "context.prepare room=%s registry_before=%d diff new=%d updated=%d moved=%d deleted=%d",
            room_id,
            registry_before,
            len(diff.new_shapes),
            len(diff.updated_shapes),
            len(diff.moved_shapes),
            len(diff.deleted_ids),
        )
    else:
        _debug_log(
            "context.prepare room=%s cache_hit registry=%d no_canvas_diffs",
            room_id,
            registry_before,
        )

    # Log deletion events.
    for deleted_id in diff.deleted_ids:
        room.event_log.append(CanvasEvent(
            event_type=EventType.deleted,
            object_id=deleted_id,
            summary=f"Removed shape {deleted_id} from canvas",
        ))

    for moved in diff.moved_shapes:
        moved_id = moved.get("id", "")
        room.event_log.append(CanvasEvent(
            event_type=EventType.moved,
            object_id=moved_id,
            summary=(
                f"Moved shape {moved_id} to "
                f"({float(moved.get('x', 0)):.0f}, {float(moved.get('y', 0)):.0f})"
            ),
        ))

    # ---- Preprocess new and updated shapes -----------------------------------
    preprocessed: list = []
    shapes_to_process = diff.new_shapes + diff.updated_shapes

    for shape in shapes_to_process:
        existing = room.registry.get(shape.get("id", ""))
        try:
            record = await preprocess_shape(shape, existing)
            room.registry.set(record.object_id, record)
            preprocessed.append(record)

            evt = EventType.created if shape in diff.new_shapes else EventType.updated
            room.event_log.append(CanvasEvent(
                event_type=evt,
                object_id=record.object_id,
                summary=(
                    f"{'Added' if evt == EventType.created else 'Updated'} "
                    f"{record.object_type.value}: "
                    f"{record.content_summary[:60]}"
                ),
            ))
        except Exception as exc:
            logger.exception(
                "preprocess failed for shape %s: %s", shape.get("id"), exc
            )

    # Increment the event counter for the summary trigger.
    n_new_events = (
        len(diff.new_shapes)
        + len(diff.updated_shapes)
        + len(diff.moved_shapes)
        + len(diff.deleted_ids)
    )
    if n_new_events:
        room.session_memory.increment_event_count(n_new_events)

    needs_summary = room.session_memory.should_update_summary()

    _debug_log(
        "context.prepare room=%s registry_after=%d preprocess=%d summary_update=%s event_log=%d actions=%d",
        room_id,
        len(room.registry),
        len(shapes_to_process),
        needs_summary,
        len(room.event_log),
        len(room.session_memory),
    )

    return {
        "canvas_diff": diff,
        "preprocessed_records": preprocessed,
        "needs_preprocessing": bool(shapes_to_process),
        "needs_summary_update": needs_summary,
    }


# ---------------------------------------------------------------------------
# Node 2: summary_update (conditional — runs only when triggered)
# ---------------------------------------------------------------------------


async def summary_node(state: ContextAwareAgentState) -> dict:
    """Refresh the SessionSummary using Claude Haiku (cheap + fast).

    Triggered only when session_memory.should_update_summary() is True.
    Resets the event counter afterwards.
    """
    room_id = state["room_id"]
    room = context_store.get_room(room_id)

    _debug_log(
        "context.summary room=%s refresh_start recent_events=%d recent_actions=%d",
        room_id,
        len(room.event_log.get_recent(20)),
        len(room.session_memory.get_recent_actions(5)),
    )

    updated = await update_session_summary(
        current_summary=room.session_memory.get_summary(),
        recent_events=room.event_log.get_recent(20),
        recent_actions=room.session_memory.get_recent_actions(5),
    )
    room.session_memory.update_summary(updated)
    room.session_memory.mark_summary_updated()

    _debug_log(
        "context.summary room=%s refresh_done goal=%r topics=%d questions=%d decisions=%d",
        room_id,
        updated.board_goal,
        len(updated.active_topics),
        len(updated.open_questions),
        len(updated.recent_decisions),
    )

    return {"needs_summary_update": False}


# ---------------------------------------------------------------------------
# Node 3: context (assemble ContextPacket)
# ---------------------------------------------------------------------------


def context_node(state: ContextAwareAgentState) -> dict:
    """Build the ContextPacket from the registry and session memory."""
    room_id = state["room_id"]
    room = context_store.get_room(room_id)

    packet = build_context_packet(
        user_message=state["user_message"],
        canvas_snapshot=state.get("canvas_snapshot", {}),
        registry=room.registry,
        event_log=room.event_log,
        session_memory=room.session_memory,
    )
    _debug_log(
        "context.packet room=%s selected=%d nearby=%d retrieved=%d recent_events=%d total_objects=%d",
        room_id,
        len(packet.selected_objects),
        len(packet.nearby_objects),
        len(packet.retrieved_objects),
        len(packet.recent_events),
        packet.canvas_stats.get("total", 0),
    )
    return {"context_packet": packet}


# ---------------------------------------------------------------------------
# Node 4: agent (Claude call + streaming + validation)
# ---------------------------------------------------------------------------


async def agent_node(state: ContextAwareAgentState) -> dict:
    """Call Claude with the assembled prompt and return validated actions.

    Uses async streaming internally so tokens are processed as they arrive;
    the full action list is returned to state once streaming completes.
    """
    settings = get_settings()
    packet = state.get("context_packet")
    if packet is None:
        logger.error("agent_node: context_packet is None — aborting")
        return {"agent_actions": [], "agent_message": ""}

    system_prompt, user_content = build_messages(packet)

    buffer = ""
    cursor = 0
    actions: list[dict[str, Any]] = []

    async with _get_client().messages.stream(
        model=settings.chat_agent_model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    ) as stream:
        async for text in stream.text_stream:
            buffer += text
            start = buffer.find("{")
            if start == -1:
                continue
            parsed = _parse_json(buffer[start:])
            if not parsed:
                continue
            raw_actions = parsed.get("actions")
            if not isinstance(raw_actions, list):
                continue
            # Emit completed actions (all except the last, which may be partial).
            while len(raw_actions) > cursor + 1:
                actions.append(raw_actions[cursor])
                cursor += 1

    # Final parse after stream closes — pick up the last action.
    start = buffer.find("{")
    if start != -1:
        parsed = _parse_json(buffer[start:])
        if parsed:
            raw_actions = parsed.get("actions", [])
            while len(raw_actions) > cursor:
                actions.append(raw_actions[cursor])
                cursor += 1

    # Validate actions against the current canvas.
    existing_ids = {
        s["id"] for s in state.get("canvas_snapshot", {}).get("shapes", [])
        if "id" in s
    }
    actions = _validate_actions(actions, existing_ids)

    # Extract the agent's chat message.
    msg_action = next(
        (a for a in actions if a.get("_type") == "message"), None
    )
    agent_message = msg_action.get("text", "") if msg_action else ""

    logger.info(
        "room=%s agent produced %d actions (message=%r)",
        state["room_id"],
        len(actions),
        agent_message[:60],
    )
    _debug_log(
        "context.agent room=%s actions=%d message=%r",
        state["room_id"],
        len(actions),
        agent_message[:60],
    )
    return {"agent_actions": actions, "agent_message": agent_message}


# ---------------------------------------------------------------------------
# Node 5: log (write to EventLog + SessionMemory — no state mutation)
# ---------------------------------------------------------------------------


def log_node(state: ContextAwareAgentState) -> dict:
    """Persist agent actions and message to the room's context store."""
    room_id = state["room_id"]
    room = context_store.get_room(room_id)

    actions = state.get("agent_actions", [])
    message = state.get("agent_message", "")

    non_msg = [a for a in actions if a.get("_type") != "message"]
    if non_msg:
        type_summary = ", ".join(
            a.get("_type", "?") for a in non_msg[:5]
        )
        if len(non_msg) > 5:
            type_summary += f" + {len(non_msg) - 5} more"
        room.event_log.append(CanvasEvent(
            event_type=EventType.agent_action,
            summary=f"Agent: {type_summary}",
        ))

    # Log the user instruction so future summaries have it.
    user_msg = state.get("user_message", "")
    if user_msg:
        room.event_log.append(CanvasEvent(
            event_type=EventType.instruction,
            summary=f'User: "{user_msg[:80]}"',
        ))

    # Add a compressed turn summary to action history.
    if message:
        short_user = (user_msg[:50] + "…") if len(user_msg) > 50 else user_msg
        room.session_memory.add_action_summary(
            f'"{short_user}" → {message[:100]}'
        )

    _debug_log(
        "context.log room=%s action_history=%d event_log=%d last_message=%r",
        room_id,
        len(room.session_memory),
        len(room.event_log),
        message[:60],
    )

    return {}


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------


def _route_after_prepare(
    state: ContextAwareAgentState,
) -> Literal["summary_update", "context"]:
    return "summary_update" if state.get("needs_summary_update") else "context"


# ---------------------------------------------------------------------------
# Build + compile graph
# ---------------------------------------------------------------------------

_builder = StateGraph(ContextAwareAgentState)
_builder.add_node("prepare", prepare_node)
_builder.add_node("summary_update", summary_node)
_builder.add_node("context", context_node)
_builder.add_node("agent", agent_node)
_builder.add_node("log", log_node)

_builder.set_entry_point("prepare")

_builder.add_conditional_edges(
    "prepare",
    _route_after_prepare,
    {"summary_update": "summary_update", "context": "context"},
)
_builder.add_edge("summary_update", "context")
_builder.add_edge("context", "agent")
_builder.add_edge("agent", "log")
_builder.add_edge("log", END)

context_aware_graph = _builder.compile()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_context_agent(
    message: str,
    canvas_snapshot: dict[str, Any],
    room_id: str = "main",
) -> list[dict[str, Any]]:
    """Invoke the context-aware graph and return the validated action list.

    Parameters
    ----------
    message:
        The user's natural-language message for this turn.
    canvas_snapshot:
        Dict with optional keys "shapes", "viewport", "selected_ids".
    room_id:
        Identifies the room's ContentRegistry / EventLog / SessionMemory.
        # UPGRADE: use Redis or DB-backed sessions keyed by room_id so
        #          context survives restarts and can scale horizontally.

    Returns
    -------
    list of validated action dicts (same format as chat_agent.py actions).
    """
    shapes = canvas_snapshot.get("shapes", [])
    _debug_log(
        "context.run room=%s incoming_shapes=%d selected=%d viewport=%s",
        room_id,
        len(shapes),
        len(canvas_snapshot.get("selected_ids") or []),
        "yes" if canvas_snapshot.get("viewport") else "no",
    )

    initial_state: ContextAwareAgentState = {
        # Required base fields
        "messages": [],
        "canvas": list(shapes),
        "room_id": room_id,
        "_stop": False,
        # Context-awareness fields
        "canvas_snapshot": canvas_snapshot,
        "user_message": message,
        "needs_preprocessing": False,
        "needs_summary_update": False,
    }

    final_state = await context_aware_graph.ainvoke(initial_state)
    return final_state.get("agent_actions", [])
