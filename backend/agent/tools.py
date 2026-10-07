"""Canvas tool definitions, helpers, and optimistic state updates."""

COLOR_MAP: dict[str, str] = {
    "purple": "violet",
    "pink": "light-violet",
    "gray": "grey",
    "light-gray": "grey",
    "lightgray": "grey",
    "dark-blue": "blue",
    "light-purple": "light-violet",
    "teal": "green",
    "cyan": "light-blue",
    "lime": "light-green",
    "salmon": "light-red",
    "brown": "orange",
}

TOOLS: list[dict] = [
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
                "geo": {
                    "type": "string",
                    "default": "rectangle",
                    "description": "rectangle|ellipse|triangle|diamond|star|hexagon",
                },
                "x": {"type": "number"},
                "y": {"type": "number"},
                "w": {"type": "number", "description": "Width px (default 220)"},
                "h": {"type": "number", "description": "Height px (default 120)"},
                "text": {"type": "string"},
                "color": {
                    "type": "string",
                    "description": "blue|green|red|orange|violet|grey|yellow|black|white",
                },
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
                "x": {"type": "number"},
                "y": {"type": "number"},
                "text": {"type": "string"},
                "color": {
                    "type": "string",
                    "description": "yellow|blue|green|orange|red|violet|grey",
                },
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
                "x": {"type": "number"},
                "y": {"type": "number"},
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
                "fromId": {"type": "string"},
                "toId": {"type": "string"},
                "x1": {"type": "number"},
                "y1": {"type": "number"},
                "x2": {"type": "number"},
                "y2": {"type": "number"},
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
        "name": "fetch_pinterest",
        "description": (
            "Search Pinterest for images matching a vibe, aesthetic, or topic. "
            "Returns a JSON list of {url, title} objects. "
            "Always follow with create_image actions to place ALL returned images on the canvas. "
            "Use when users ask for moodboard, inspiration, aesthetic references, or 'images/pictures of X'."
        ),
        "input_schema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Search query (e.g. 'dark academia aesthetic', 'minimalist coffee shop interior')"},
                "max_results": {"type": "integer", "description": "Number of images to fetch (default 5, max 8)"},
            },
        },
    },
    {
        "name": "create_image",
        "description": "Place an image from a URL on the canvas. Use for Pinterest images returned by fetch_pinterest.",
        "input_schema": {
            "type": "object",
            "required": ["shapeId", "url", "x", "y"],
            "properties": {
                "shapeId": {"type": "string"},
                "url": {"type": "string", "description": "Image URL"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "w": {"type": "number", "description": "Width px (default 300)"},
                "h": {"type": "number", "description": "Height px (default 200)"},
            },
        },
    },
    {
        "name": "generate_image",
        "description": (
            "Generate an AI image on the canvas using Higgsfield Seedream. "
            "Creates a placeholder immediately; the real image appears in ~30-60s. "
            "Use when users ask to generate, create, or visualize an image/photo/illustration."
        ),
        "input_schema": {
            "type": "object",
            "required": ["shapeId", "prompt", "x", "y"],
            "properties": {
                "shapeId": {"type": "string"},
                "prompt": {"type": "string", "description": "Detailed, vivid image description"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "w": {"type": "number", "description": "Width px (default 320)"},
                "h": {"type": "number", "description": "Height px (default 220)"},
                "aspect_ratio": {"type": "string", "description": "16:9 | 1:1 | 9:16 (default 16:9)"},
            },
        },
    },
    {
        "name": "generate_video",
        "description": (
            "Generate an AI video from an existing image on the canvas. "
            "Use when users want to animate an image. "
            "Get the image_url from read_canvas (url field of an image shape)."
        ),
        "input_schema": {
            "type": "object",
            "required": ["shapeId", "image_url", "prompt", "x", "y"],
            "properties": {
                "shapeId": {"type": "string"},
                "image_url": {"type": "string", "description": "URL of the source image from canvas (url field)"},
                "prompt": {"type": "string", "description": "Description of desired motion or camera movement"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "w": {"type": "number", "description": "Width px (default 320)"},
                "h": {"type": "number", "description": "Height px (default 220)"},
                "duration": {"type": "integer", "description": "Duration in seconds (3 or 5)"},
            },
        },
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


def format_canvas(canvas_state: list[dict]) -> str:
    if not canvas_state:
        return "The canvas is empty."
    lines = ["Current shapes on canvas:"]
    for s in canvas_state:
        line = (
            f"  - ID: {s['id']}  type: {s['type']}"
            f"  pos: ({s.get('x', 0):.0f}, {s.get('y', 0):.0f})"
        )
        if s.get("text"):
            line += f'  text: "{s["text"]}"'
        if s.get("color"):
            line += f"  color: {s['color']}"
        if s.get("url"):
            line += f'  url: "{s["url"]}"'
        lines.append(line)
    return "\n".join(lines)


def apply_optimistic(canvas: list[dict], action: dict) -> None:
    """Update an in-memory canvas list after emitting an agent action."""
    t = action.get("_type", "")
    if t in ("create_shape", "create_note", "create_text", "create_arrow"):
        canvas.append(
            {
                "id": action.get("shapeId", action.get("id", "")),
                "type": {
                    "create_note": "note",
                    "create_text": "text",
                    "create_arrow": "arrow",
                }.get(t, "geo"),
                "x": action.get("x", action.get("x1", 0)),
                "y": action.get("y", action.get("y1", 0)),
                "w": action.get("w", 200),
                "h": action.get("h", 100),
                "text": action.get("text", ""),
                "color": action.get("color", ""),
                "geo": action.get("geo", "rectangle"),
            }
        )
    elif t == "delete_shape":
        sid = action.get("id") or action.get("shapeId")
        canvas[:] = [s for s in canvas if s.get("id") != sid]
    elif t == "move_shape":
        for s in canvas:
            if s.get("id") == action.get("id"):
                s["x"] = action.get("x", s["x"])
                s["y"] = action.get("y", s["y"])
