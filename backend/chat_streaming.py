import os
import json
import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MOODBOARD_KEYWORDS = [
    "moodboard", "mood board", "inspiration", "aesthetic",
    "references", "reference images", "visual style", "pinterest",
    "images of", "photos of", "pictures of", "vibe",
]

ACTION_SCHEMA = """{
  "actions": [
    // Sticky note — assign your own shapeId so arrows can reference it:
    {"_type": "create_note", "shapeId": "my_note_1", "text": "string", "x": number, "y": number, "color": "yellow|blue|green|orange|red|violet|grey"},

    // Geometric shape (rectangle, ellipse, triangle, diamond, star, hexagon):
    {"_type": "create_shape", "shapeId": "my_box_1", "geo": "rectangle|ellipse|triangle|diamond|star|hexagon", "text": "string", "x": number, "y": number, "w": number, "h": number, "color": "blue|green|red|orange|violet|grey|yellow|black|white"},

    // Standalone text label (no box):
    {"_type": "create_text", "shapeId": "my_label_1", "text": "string", "x": number, "y": number, "color": "black|blue|red|grey|violet|orange|yellow|green"},

    // Arrow connecting two shapes — fromId/toId reference shapeId values YOU assigned above:
    {"_type": "create_arrow", "shapeId": "my_arrow_1", "fromId": "shape_a", "toId": "shape_b", "x1": number, "y1": number, "x2": number, "y2": number, "color": "black|blue|red|grey", "text": "optional label"},

    // Pinterest image — use ONLY the exact URLs provided in the system context:
    {"_type": "create_image", "shapeId": "my_img_1", "url": "https://...", "x": number, "y": number, "w": number, "h": number},

    // Generate an AI image on the canvas (takes 30-60s, creates a placeholder while generating):
    {"_type": "generate_image", "shapeId": "img_1", "prompt": "detailed vivid description of desired image", "x": number, "y": number},

    // Generate an AI video from an existing image on the canvas (takes 1-3min):
    {"_type": "generate_video", "shapeId": "vid_1", "prompt": "description of desired motion/camera movement", "sourceImageShapeId": "existing_image_shape_id", "x": number, "y": number},

    // Move existing shape (id comes from canvas state):
    {"_type": "move_shape", "id": "shape_id_from_canvas", "x": number, "y": number},

    // Update text of existing shape:
    {"_type": "update_text", "id": "shape_id_from_canvas", "text": "string"},

    // Delete a shape:
    {"_type": "delete_shape", "id": "shape_id_from_canvas"},

    // Reply to user (ALWAYS include as the last action):
    {"_type": "message", "text": "string"}
  ]
}"""


def close_and_parse_json(s: str):
    """Given a potentially incomplete JSON string, close it and parse it."""
    stack = []
    i = 0
    while i < len(s):
        char = s[i]
        last = stack[-1] if stack else None
        if char == '"':
            if i > 0 and s[i - 1] == '\\':
                i += 1
                continue
            if last == '"':
                stack.pop()
            else:
                stack.append('"')
        if last == '"':
            i += 1
            continue
        if char in ('{', '['):
            stack.append(char)
        elif char == '}' and last == '{':
            stack.pop()
        elif char == ']' and last == '[':
            stack.pop()
        i += 1

    result = s
    for opening in reversed(stack):
        if opening == '{':
            result += '}'
        elif opening == '[':
            result += ']'
        elif opening == '"':
            result += '"'

    try:
        return json.loads(result)
    except Exception:
        return None


def format_canvas(canvas_state: list[dict]) -> str:
    if not canvas_state:
        return "The canvas is empty."
    lines = ["Current shapes on canvas:"]
    for s in canvas_state:
        line = f"  - ID: {s['id']}  type: {s['type']}  pos: ({s.get('x', 0):.0f}, {s.get('y', 0):.0f})"
        if s.get("text"):
            line += f'  text: "{s["text"]}"'
        if s.get("color"):
            line += f"  color: {s['color']}"
        lines.append(line)
    return "\n".join(lines)


def _detect_moodboard(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in MOODBOARD_KEYWORDS)


def stream_agent(message: str, canvas_state: list[dict], pinterest_images: list | None = None):
    """Sync generator that streams SSE-formatted events."""
    canvas_description = format_canvas(canvas_state)

    if pinterest_images is None:
        pinterest_images = []

    image_section = ""
    if pinterest_images:
        lines = ["\nAvailable Pinterest images (use ALL of them as create_image actions):"]
        for i, img in enumerate(pinterest_images):
            lines.append(f'  [{i + 1}] url: "{img["url"]}"  title: "{img["title"]}"')
        lines.append(
            "\nLayout: place a create_text label above the row, then place images in a "
            "horizontal row — each 160px wide × 200px tall, 20px gap between them, "
            "starting around y=350. Use the exact URLs above."
        )
        image_section = "\n".join(lines)

    system = f"""You are an AI brainstorming partner living directly on a collaborative canvas.

{canvas_description}{image_section}

CRITICAL RULES:
- ALWAYS act immediately. Never ask clarifying questions — make reasonable choices and do it.
- Spread items across the canvas: x between 50–1100, y between 50–700. Leave ~20–40px between items.
- For diagrams (UML, flowcharts, mind maps, etc.), use create_shape + create_arrow with meaningful shapeId names like "user_class", "auth_service", etc.
- Arrow fromId/toId reference the shapeId you assigned — NOT the full tldraw ID. Example: create shape with shapeId "box_a", then arrow with fromId "box_a".
- You can generate AI images with generate_image. Provide a vivid, detailed prompt describing the desired image. Image generation takes ~30-60s; a placeholder will appear on the canvas while generating.
- You can generate AI videos with generate_video from an existing image on the canvas. Provide a motion/camera description as prompt and reference the source image's shapeId via sourceImageShapeId.
- When the user asks to create/draw/generate a picture/photo/image/illustration, use generate_image (NOT create_shape). Use create_shape only for geometric diagrams.
- When Pinterest images are provided in the context, use create_image to place ALL of them on the canvas.
- Always include a "message" action as the LAST action with a one-sentence confirmation of what you did.
- Output ONLY a valid JSON object. No markdown, no code blocks, no explanation outside the JSON.

Output format (strictly follow this schema):
{ACTION_SCHEMA}"""

    buffer = ""
    cursor = 0

    with client.messages.stream(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        system=system,
        messages=[
            {"role": "user", "content": message},
        ],
    ) as stream:
        for text in stream.text_stream:
            buffer += text

            # Find the JSON object in the buffer (skip any leading text/markdown)
            start = buffer.find("{")
            if start == -1:
                continue
            json_slice = buffer[start:]

            parsed = close_and_parse_json(json_slice)
            if not parsed:
                continue

            actions = parsed.get("actions")
            if not isinstance(actions, list):
                continue

            # An action at index `cursor` is complete when the next action exists
            while len(actions) > cursor + 1:
                action = actions[cursor]
                yield f"data: {json.dumps({'type': 'action', 'action': action})}\n\n"
                cursor += 1

    # Stream ended — emit any remaining actions
    start = buffer.find("{")
    parsed = close_and_parse_json(buffer[start:]) if start != -1 else None
    if parsed:
        actions = parsed.get("actions", [])
        while len(actions) > cursor:
            action = actions[cursor]
            yield f"data: {json.dumps({'type': 'action', 'action': action})}\n\n"
            cursor += 1

    yield f"data: {json.dumps({'type': 'done'})}\n\n"
