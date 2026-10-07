"""
LangGraph-based canvas agent.

Graph structure:
    START → agent → (tool calls?) → tools → agent → ... → END

State:
    messages  – Anthropic-format message history for this invocation
    canvas    – local copy of canvas, updated after each tool call
    room_id   – used to broadcast actions to the right WebSocket room
    _stop     – set True when agent calls finish or produces no tool calls
"""

import os
from typing import Literal, TypedDict

from anthropic import AsyncAnthropic
from langgraph.graph import StateGraph, END

from agent import format_canvas

client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ---------------------------------------------------------------------------
# Tool schemas (Anthropic format)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "create_shape",
        "description": (
            "Create a geometric shape (rectangle, ellipse, triangle, diamond, star, hexagon) "
            "with optional text inside. Use for UML boxes, flowchart nodes, containers. "
            "All text (including UML sections separated by \\n---\\n) goes in the text field."
        ),
        "input_schema": {
            "type": "object",
            "required": ["shapeId", "x", "y"],
            "properties": {
                "shapeId": {"type": "string"},
                "geo": {"type": "string", "default": "rectangle",
                        "description": "rectangle|ellipse|triangle|diamond|star|hexagon"},
                "x": {"type": "number"}, "y": {"type": "number"},
                "w": {"type": "number", "description": "Width px (default 220)"},
                "h": {"type": "number", "description": "Height px (default 120)"},
                "text": {"type": "string"},
                "color": {"type": "string",
                          "description": "blue|green|red|orange|violet|grey|yellow|black|white"},
            },
        },
    },
    {
        "name": "create_note",
        "description": "Create a sticky note.",
        "input_schema": {
            "type": "object",
            "required": ["shapeId", "x", "y"],
            "properties": {
                "shapeId": {"type": "string"},
                "x": {"type": "number"}, "y": {"type": "number"},
                "text": {"type": "string"},
                "color": {"type": "string",
                          "description": "yellow|blue|green|orange|red|violet|grey"},
            },
        },
    },
    {
        "name": "create_text",
        "description": "Floating text label — use ONLY for titles or section headers above groups.",
        "input_schema": {
            "type": "object",
            "required": ["shapeId", "x", "y"],
            "properties": {
                "shapeId": {"type": "string"},
                "x": {"type": "number"}, "y": {"type": "number"},
                "text": {"type": "string"},
                "color": {"type": "string"},
            },
        },
    },
    {
        "name": "create_arrow",
        "description": "Arrow connecting two shapes via fromId/toId, or free via x1/y1/x2/y2.",
        "input_schema": {
            "type": "object",
            "required": ["shapeId"],
            "properties": {
                "shapeId": {"type": "string"},
                "fromId": {"type": "string"}, "toId": {"type": "string"},
                "x1": {"type": "number"}, "y1": {"type": "number"},
                "x2": {"type": "number"}, "y2": {"type": "number"},
                "text": {"type": "string"},
                "color": {"type": "string"},
            },
        },
    },
    {
        "name": "update_text",
        "description": (
            "Update the text of an EXISTING shape on the canvas. "
            "Use this to add methods/attributes to existing UML classes. "
            "The id must match a shape from read_canvas output."
        ),
        "input_schema": {
            "type": "object",
            "required": ["id", "text"],
            "properties": {
                "id": {"type": "string", "description": "Shape ID from canvas state"},
                "text": {"type": "string", "description": "Complete new text (replaces current)"},
            },
        },
    },
    {
        "name": "delete_shape",
        "description": "Delete a shape by its canvas ID.",
        "input_schema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "string"},
            },
        },
    },
    {
        "name": "read_canvas",
        "description": (
            "Read current canvas state including any shapes created this turn. "
            "Always call this first when the canvas is not empty."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "finish",
        "description": "Call when all requested actions are complete.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
        },
    },
]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM = """\
You are Higgs, an AI assistant embedded in a collaborative whiteboard canvas.
You were invoked by name in a voice conversation between collaborators.

{canvas}

RULES:
- Speakers called your name — ALWAYS act on the request immediately.
- NEVER ask clarifying questions. Make a reasonable interpretation and do it.
- The command may contain speech-to-text errors; use context to interpret them.
- Use at most 8 tool call rounds total. Call finish when done.

MODIFYING EXISTING SHAPES:
- If the command says "add X to Y" or "update Y" and shape Y already exists, use update_text — do NOT create a new shape.
- To add a method to a UML class: call read_canvas first to get the current text, then call update_text with the full appended text.
  Example: current="Order\\n---\\n- orderId: int\\n---\\n+ createOrder()"
  After "add editOrder": update_text with "Order\\n---\\n- orderId: int\\n---\\n+ createOrder()\\n+ editOrder()"

CREATING NEW SHAPES:
- Spread items: x 50–1100, y 50–700. Leave 80px gap between shapes.
- UML: w=220, h=280, row layout x = 80 + (i × 320), y = 150.
- Flowcharts: top-to-bottom, centered, 120px vertical gap.
- Mind maps: center at x=550 y=350, branches at ~200px radius.
- All UML class text (fields + methods) goes in the shape's text field using \\n---\\n as separator.
- NEVER use create_text on top of a shape — put everything in the shape's text field.\
"""

# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    messages: list        # Anthropic-format message list
    canvas: list[dict]    # local canvas view, updated per tool round
    room_id: str
    _stop: bool           # True when graph should terminate


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def agent_node(state: AgentState) -> dict:
    system_text = SYSTEM.format(canvas=format_canvas(state["canvas"]))
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        tools=TOOLS,
        system=system_text,
        messages=state["messages"],
    )
    new_messages = state["messages"] + [{"role": "assistant", "content": response.content}]
    tool_uses = [b for b in response.content if b.type == "tool_use"]
    stop = not tool_uses or any(b.name == "finish" for b in tool_uses)
    print(f"[lg] agent: {[b.name for b in tool_uses] if tool_uses else 'no tools → done'}")
    return {"messages": new_messages, "_stop": stop}


async def tool_node(state: AgentState) -> dict:
    from voice import room_manager, COLOR_MAP, _apply_optimistic

    last_content = state["messages"][-1]["content"]
    tool_uses = [b for b in last_content if b.type == "tool_use"]
    canvas = list(state["canvas"])
    results = []

    for tu in tool_uses:
        name, args = tu.name, tu.input

        if name == "finish":
            print(f"[lg] finish: {args.get('summary', '')}")
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "Done."})
            continue

        if name == "read_canvas":
            result = format_canvas(canvas)

        elif name == "update_text":
            action = {"_type": "update_text", **args}
            await room_manager.broadcast(state["room_id"], {"type": "agent_action", "action": action})
            sid = args.get("id", "")
            new_text = args.get("text", "")
            for lst in (canvas, room_manager._canvas.get(state["room_id"], [])):
                for s in lst:
                    if s.get("id") == sid:
                        s["text"] = new_text
            room_manager._schedule_save(state["room_id"])
            result = f"Updated text on '{sid}'."

        elif name == "delete_shape":
            action = {"_type": "delete_shape", **args}
            await room_manager.broadcast(state["room_id"], {"type": "agent_action", "action": action})
            sid = args.get("id") or args.get("shapeId")
            canvas[:] = [s for s in canvas if s.get("id") != sid]
            _apply_optimistic(room_manager._canvas.get(state["room_id"], []), action)
            room_manager._schedule_save(state["room_id"])
            result = f"Deleted '{sid}'."

        else:
            action = {"_type": name, **args}
            if "color" in action:
                action["color"] = COLOR_MAP.get(action["color"], action["color"])
            await room_manager.broadcast(state["room_id"], {"type": "agent_action", "action": action})
            _apply_optimistic(canvas, action)
            _apply_optimistic(room_manager._canvas.get(state["room_id"], []), action)
            room_manager._schedule_save(state["room_id"])
            result = f"Done. Canvas: {len(canvas)} shapes."

        results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

    new_messages = state["messages"] + [{"role": "user", "content": results}]
    return {"messages": new_messages, "canvas": canvas}


def route(state: AgentState) -> Literal["tools", "__end__"]:
    return END if state.get("_stop", False) else "tools"


# ---------------------------------------------------------------------------
# Build graph (compiled once at import time)
# ---------------------------------------------------------------------------

_builder = StateGraph(AgentState)
_builder.add_node("agent", agent_node)
_builder.add_node("tools", tool_node)
_builder.set_entry_point("agent")
_builder.add_conditional_edges("agent", route, {"tools": "tools", END: END})
_builder.add_edge("tools", "agent")

canvas_agent = _builder.compile()


# ---------------------------------------------------------------------------
# Public entrypoint (called from voice.py)
# ---------------------------------------------------------------------------

async def listener_agent_react(command: str, canvas_state: list[dict], room_id: str) -> None:
    print(f"[lg] room={room_id} cmd='{command[:80]}'")
    await canvas_agent.ainvoke({
        "messages": [{"role": "user", "content": command}],
        "canvas": list(canvas_state),
        "room_id": room_id,
        "_stop": False,
    })
    print(f"[lg] done")
