"""Shared image upload utilities used by main.py and team_chat_agent.py."""

import base64
import io

import httpx
from PIL import Image


def preprocess_image(image_bytes: bytes) -> tuple[bytes, str, str]:
    """Resize to max 1920px, strip alpha, convert to JPEG."""
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("RGB")
    if img.width > 1920 or img.height > 1920:
        img.thumbnail((1920, 1920), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue(), "image/jpeg", "jpg"


async def upload_to_public_host(image_bytes: bytes, mime: str, ext: str) -> str:
    """Try catbox.moe then transfer.sh. Returns public URL or raises."""
    filename = f"image.{ext}"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            res = await client.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": (filename, image_bytes, mime)},
            )
            if res.status_code == 200 and res.text.strip().startswith("https://"):
                return res.text.strip()
        except Exception as e:
            print(f"[upload] catbox.moe error: {e}")

        try:
            res = await client.put(
                f"https://transfer.sh/{filename}",
                content=image_bytes,
                headers={"Content-Type": mime, "Max-Days": "1"},
            )
            if res.status_code == 200 and res.text.strip().startswith("https://"):
                return res.text.strip()
        except Exception as e:
            print(f"[upload] transfer.sh error: {e}")

    raise RuntimeError("All upload services failed")


async def upload_data_url(data_url: str) -> str:
    """Upload a base64 data URL and return a public HTTPS URL."""
    header, b64data = data_url.split(",", 1)
    mime = header.split(":")[1].split(";")[0]
    ext = mime.split("/")[-1].split("+")[0]
    image_bytes = base64.b64decode(b64data)
    image_bytes, mime, ext = preprocess_image(image_bytes)
    return await upload_to_public_host(image_bytes, mime, ext)
