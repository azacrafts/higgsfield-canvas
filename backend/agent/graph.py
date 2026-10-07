"""
LangGraph ReAct canvas agent.

Graph:  START → agent → (tools?) → tools → agent → … → END

The graph is compiled once at import time. room_manager is imported from
room_manager.py — no circular dependency.
"""

import asyncio
import json
import logging
from typing import Any, Literal, TypedDict

from anthropic import AsyncAnthropic
from langgraph.graph import END, StateGraph

from agent.prompts import CANVAS_AGENT_SYSTEM
from agent.tools import TOOLS, COLOR_MAP, apply_optimistic, format_canvas
from config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Anthropic client (lazy — created on first use to respect env loading order)
# ---------------------------------------------------------------------------

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=get_settings().anthropic_api_key)
    return _client


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------


class AgentState(TypedDict):
    messages: list[dict[str, Any]]
    canvas: list[dict]
    room_id: str
    _stop: bool
    model: str  # optional model override; falls back to settings.canvas_agent_model


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def agent_node(state: AgentState) -> dict:
    settings = get_settings()
    system_text = CANVAS_AGENT_SYSTEM.format(canvas=format_canvas(state["canvas"]))
    model = state.get("model") or settings.canvas_agent_model
    response = await _get_client().messages.create(
        model=model,
        max_tokens=2048,
        tools=TOOLS,
        system=system_text,
        messages=state["messages"],
    )
    new_messages = state["messages"] + [{"role": "assistant", "content": response.content}]
    tool_uses = [b for b in response.content if b.type == "tool_use"]
    stop = not tool_uses or any(b.name == "finish" for b in tool_uses)
    logger.debug("agent tools=%s", [b.name for b in tool_uses] if tool_uses else "none → done")
    return {"messages": new_messages, "_stop": stop}


async def tool_node(state: AgentState) -> dict:
    # Import here is fine — room_manager is a dedicated module, no circular dep.
    from room_manager import room_manager  # noqa: PLC0415

    last_content = state["messages"][-1]["content"]
    tool_uses = [b for b in last_content if b.type == "tool_use"]
    canvas = list(state["canvas"])
    results: list[dict] = []

    for tu in tool_uses:
        name, args = tu.name, tu.input

        if name == "finish":
            logger.info("finish: %s", args.get("summary", ""))
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "Done."})
            continue

        if name == "read_canvas":
            result = format_canvas(canvas)

        elif name == "fetch_pinterest":
            from pinterest import fetch_pinterest_images  # noqa: PLC0415

            query = args.get("query", "")
            max_results = int(args.get("max_results", 5))
            images = await asyncio.to_thread(fetch_pinterest_images, query, max_results)
            result = json.dumps(images) if images else "[]"
            logger.info("pinterest query=%r returned %d images", query, len(images))

        elif name == "create_image":
            action = {"_type": "create_image", **args}
            await room_manager.broadcast_ai_cursor(state["room_id"], float(args.get("x", 200)), float(args.get("y", 200)))
            await room_manager.broadcast(state["room_id"], {"type": "agent_action", "action": action})
            # Track as generic shape in optimistic state
            apply_optimistic(
                canvas,
                {
                    "_type": "create_shape",
                    "shapeId": args.get("shapeId", ""),
                    "x": args.get("x", 0),
                    "y": args.get("y", 0),
                    "w": args.get("w", 300),
                    "h": args.get("h", 200),
                    "text": "",
                },
            )
            apply_optimistic(room_manager.get_canvas(state["room_id"]), {
                "_type": "create_shape",
                "shapeId": args.get("shapeId", ""),
                "x": args.get("x", 0),
                "y": args.get("y", 0),
                "w": args.get("w", 300),
                "h": args.get("h", 200),
                "text": "",
            })
            room_manager.schedule_save(state["room_id"])
            result = f"Placed image on canvas as '{args.get('shapeId', '')}'."

        elif name == "generate_image":
            from higgsfield import submit_image_generation  # noqa: PLC0415

            shape_id = args.get("shapeId", f"gen_{int(asyncio.get_event_loop().time())}")
            x = float(args.get("x", 200))
            y = float(args.get("y", 200))
            w = float(args.get("w", 320))
            h = float(args.get("h", 220))
            prompt = args.get("prompt", "")
            aspect_ratio = args.get("aspect_ratio", "16:9")

            # Broadcast placeholder
            placeholder = {
                "_type": "create_shape",
                "shapeId": shape_id,
                "geo": "rectangle",
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "text": "Generating image…",
                "color": "grey",
            }
            await room_manager.broadcast_ai_cursor(state["room_id"], x, y)
            await room_manager.broadcast(state["room_id"], {"type": "agent_action", "action": placeholder})
            apply_optimistic(canvas, placeholder)
            apply_optimistic(room_manager.get_canvas(state["room_id"]), placeholder)

            try:
                gen_result = await submit_image_generation(prompt, aspect_ratio=aspect_ratio)
                logger.info("generate_image response keys: %s", list(gen_result.keys()))

                if gen_result.get("images"):
                    # Seedream returned synchronously — broadcast generate_complete immediately
                    url = gen_result["images"][0].get("url")
                    if url:
                        await room_manager.broadcast(
                            state["room_id"],
                            {
                                "type": "agent_action",
                                "action": {
                                    "_type": "generate_complete",
                                    "shapeId": shape_id,
                                    "url": url,
                                    "x": x, "y": y, "w": w, "h": h,
                                    "media_type": "image",
                                    "prompt": prompt,
                                },
                            },
                        )
                        result = f"Image generated and placed at '{shape_id}'."
                    else:
                        result = "Image generation returned no URL."
                elif gen_result.get("request_id"):
                    # Async — poll until complete
                    request_id = gen_result["request_id"]
                    asyncio.create_task(
                        _poll_generation(state["room_id"], shape_id, x, y, w, h, request_id, "image")
                    )
                    result = f"Image generation started (request={request_id}). Placeholder '{shape_id}' on canvas."
                else:
                    result = f"Image generation unexpected response: {gen_result}"
            except Exception as e:
                logger.exception("generate_image failed")
                result = f"Image generation failed: {e}"

        elif name == "generate_video":
            from higgsfield import submit_video_generation  # noqa: PLC0415

            shape_id = args.get("shapeId", f"vid_{int(asyncio.get_event_loop().time())}")
            x = float(args.get("x", 200))
            y = float(args.get("y", 200))
            w = float(args.get("w", 320))
            h = float(args.get("h", 220))
            image_url = _unwrap_proxy_url(args.get("image_url", ""))
            prompt = args.get("prompt", "")
            duration = int(args.get("duration", 3))

            # Broadcast placeholder
            placeholder = {
                "_type": "create_shape",
                "shapeId": shape_id,
                "geo": "rectangle",
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "text": "Generating video…",
                "color": "violet",
            }
            await room_manager.broadcast_ai_cursor(state["room_id"], x, y)
            await room_manager.broadcast(state["room_id"], {"type": "agent_action", "action": placeholder})
            apply_optimistic(canvas, placeholder)
            apply_optimistic(room_manager.get_canvas(state["room_id"]), placeholder)

            try:
                gen_result = await submit_video_generation(image_url, prompt, duration)
                request_id = gen_result["request_id"]
                asyncio.create_task(
                    _poll_generation(state["room_id"], shape_id, x, y, w, h, request_id, "video")
                )
                result = f"Video generation started (request={request_id}). Placeholder '{shape_id}' on canvas."
            except Exception as e:
                result = f"Video generation failed: {e}"

        elif name == "update_text":
            action = {"_type": "update_text", **args}
            await room_manager.broadcast(state["room_id"], {"type": "agent_action", "action": action})
            sid = args.get("id", "")
            new_text = args.get("text", "")
            for lst in (canvas, room_manager.get_canvas(state["room_id"])):
                for s in lst:
                    if s.get("id") == sid:
                        s["text"] = new_text
            room_manager.schedule_save(state["room_id"])
            result = f"Updated text on '{sid}'."

        elif name == "delete_shape":
            action = {"_type": "delete_shape", **args}
            await room_manager.broadcast(state["room_id"], {"type": "agent_action", "action": action})
            sid = args.get("id") or args.get("shapeId")
            canvas[:] = [s for s in canvas if s.get("id") != sid]
            apply_optimistic(room_manager.get_canvas(state["room_id"]), action)
            room_manager.schedule_save(state["room_id"])
            result = f"Deleted '{sid}'."

        else:
            action = {"_type": name, **args}
            if "color" in action:
                action["color"] = COLOR_MAP.get(action["color"], action["color"])
            cursor_x = float(args.get("x", args.get("x1", 200)))
            cursor_y = float(args.get("y", args.get("y1", 200)))
            await room_manager.broadcast_ai_cursor(state["room_id"], cursor_x, cursor_y)
            await room_manager.broadcast(state["room_id"], {"type": "agent_action", "action": action})
            apply_optimistic(canvas, action)
            apply_optimistic(room_manager.get_canvas(state["room_id"]), action)
            room_manager.schedule_save(state["room_id"])
            result = f"Done. Canvas now has {len(canvas)} shapes."

        results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

    new_messages = state["messages"] + [{"role": "user", "content": results}]
    return {"messages": new_messages, "canvas": canvas}


def _unwrap_proxy_url(url: str) -> str:
    """Extract the original URL if it was wrapped by a backend proxy endpoint."""
    for prefix in ("/api/proxy-image?url=", "/api/proxy-media?url="):
        idx = url.find(prefix)
        if idx != -1:
            from urllib.parse import unquote
            return unquote(url[idx + len(prefix):])
    return url


async def _poll_generation(
    room_id: str,
    shape_id: str,
    x: float,
    y: float,
    w: float,
    h: float,
    request_id: str,
    media_type: str,
) -> None:
    """Poll Higgsfield until the generation completes, then broadcast the result."""
    from higgsfield import get_request_status  # noqa: PLC0415
    from room_manager import room_manager  # noqa: PLC0415

    for _ in range(180):  # 180 × 2s ≈ 6 min
        await asyncio.sleep(2)
        try:
            status = await get_request_status(request_id)
            s = status.get("status", "")
            if s == "completed":
                url = None
                if status.get("images"):
                    url = status["images"][0].get("url")
                elif status.get("video"):
                    url = status["video"].get("url")
                if url:
                    await room_manager.broadcast_ai_cursor(room_id, x, y)
                    await room_manager.broadcast(
                        room_id,
                        {
                            "type": "agent_action",
                            "action": {
                                "_type": "generate_complete",
                                "shapeId": shape_id,
                                "url": url,
                                "x": x,
                                "y": y,
                                "w": w,
                                "h": h,
                                "media_type": media_type,
                            },
                        },
                    )
                return
            if s in ("failed", "nsfw", "cancelled"):
                await room_manager.broadcast(
                    room_id,
                    {
                        "type": "agent_action",
                        "action": {
                            "_type": "update_text",
                            "id": f"shape:{shape_id}",
                            "text": f"Generation {s}.",
                        },
                    },
                )
                return
        except Exception:
            logger.exception("_poll_generation error room=%s shape=%s", room_id, shape_id)


def _route(state: AgentState) -> Literal["tools", "__end__"]:
    return END if state.get("_stop", False) else "tools"


# ---------------------------------------------------------------------------
# Build graph (compiled once at import time)
# ---------------------------------------------------------------------------

_builder = StateGraph(AgentState)
_builder.add_node("agent", agent_node)
_builder.add_node("tools", tool_node)
_builder.set_entry_point("agent")
_builder.add_conditional_edges("agent", _route, {"tools": "tools", END: END})
_builder.add_edge("tools", "agent")

canvas_agent = _builder.compile()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_canvas_agent(
    command: str,
    canvas_state: list[dict],
    room_id: str,
    model: str | None = None,
) -> None:
    logger.info("room=%s model=%s cmd='%s'", room_id, model or "default", command[:80])
    await canvas_agent.ainvoke(
        {
            "messages": [{"role": "user", "content": command}],
            "canvas": list(canvas_state),
            "room_id": room_id,
            "_stop": False,
            "model": model or "",
        }
    )
    logger.info("room=%s agent done", room_id)
