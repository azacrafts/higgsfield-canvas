import os
import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://platform.higgsfield.ai"

# Persistent client — reuses TCP connections and avoids repeated TLS handshakes
_client = httpx.AsyncClient(timeout=30, http2=True)


def get_auth() -> str:
    key = os.environ.get("HF_API_KEY", "")
    secret = os.environ.get("HF_API_SECRET", "")
    if not key or not secret:
        raise RuntimeError("Missing HF_API_KEY or HF_API_SECRET in env")
    return f"Key {key}:{secret}"


async def _post(path: str, body: dict) -> dict:
    headers = {
        "Authorization": get_auth(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    res = await _client.post(f"{BASE_URL}/{path}", json=body, headers=headers)
    if res.status_code >= 400:
        raise RuntimeError(f"Higgsfield API error {res.status_code} at {path}: {res.text}")
    return res.json()


async def submit_image_generation(
    prompt: str,
    aspect_ratio: str = "16:9",
    resolution: str = "2K",
) -> dict:
    return await _post(
        "bytedance/seedream/v4/text-to-image",
        {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
        },
    )


async def submit_video_generation(
    image_url: str,
    prompt: str,
    duration: int = 3,
    webhook_url: str | None = None,
) -> dict:
    path = "higgsfield-ai/dop/standard"
    if webhook_url:
        path = f"{path}?hf_webhook={webhook_url}"
    return await _post(
        path,
        {
            "image_url": image_url,
            "prompt": prompt,
            "duration": duration,
        },
    )


async def submit_flux_generation(
    prompt: str,
    aspect_ratio: str = "16:9",
    resolution: str = "1k",
) -> dict:
    return await _post(
        "flux-2",
        {
            "prompt": prompt,
            "image_urls": [],
            "resolution": resolution.lower(),  # Flux uses lowercase: "1k", "2k"
            "aspect_ratio": aspect_ratio,
            "prompt_upsampling": True,
        },
    )


async def submit_dop_turbo_generation(
    image_url: str,
    prompt: str,
    duration: int = 3,
    webhook_url: str | None = None,
) -> dict:
    path = "higgsfield-ai/dop/turbo"
    if webhook_url:
        path = f"{path}?hf_webhook={webhook_url}"
    return await _post(
        path,
        {
            "image_url": image_url,
            "prompt": prompt,
            "duration": duration,
            "motions": [],
            "enhance_prompt": True,
        },
    )


async def submit_kling_generation(
    image_url: str,
    prompt: str,
    duration: int = 5,
    webhook_url: str | None = None,
) -> dict:
    path = "kling-video/v3.0/std/image-to-video"
    if webhook_url:
        path = f"{path}?hf_webhook={webhook_url}"
    return await _post(
        path,
        {
            "image_url": image_url,
            "prompt": prompt,
            "duration": duration,
            "sound": "off",
            "cfg_scale": 0.5,
            "elements": [],
            "multi_shots": False,
            "multi_prompt": [],
        },
    )


async def get_request_status(request_id: str) -> dict:
    headers = {
        "Authorization": get_auth(),
        "Accept": "application/json",
    }
    res = await _client.get(
        f"{BASE_URL}/requests/{request_id}/status",
        headers=headers,
        timeout=15,
    )
    return res.json()
