from __future__ import annotations

import asyncio
import base64
import logging
import os
from contextvars import ContextVar

import httpx

from app.llm_client import LLMClient

logger = logging.getLogger(__name__)

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": "Generate an image from a text description using AI.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "A detailed description of the image to generate",
                },
            },
            "required": ["prompt"],
        },
    },
}

# Context variable — set by router before calling tools, read by image_gen
current_user_id: ContextVar[int] = ContextVar("current_user_id", default=0)

# Per-user pending images: {user_id: [file_path, ...]}
_pending_images: dict[int, list[str]] = {}


def pop_pending_images(user_id: int) -> list[str]:
    """Return and clear all pending image paths for a user."""
    return _pending_images.pop(user_id, [])


async def generate_image(prompt: str) -> str:
    """Generate an image via NEAR AI Cloud, download it, and return info.

    The image is saved to a temp file so the router can send it as a Telegram photo.
    """
    url = await LLMClient.generate_image(prompt)
    if not url:
        return "Sorry, image generation failed. Please try a different prompt."

    # Download the image asynchronously
    img_bytes = None
    try:
        if url.startswith("data:image"):
            # Base64 data URI — decode inline instead of HTTP download
            _, b64_data = url.split(",", 1)
            img_bytes = base64.b64decode(b64_data)
        else:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url)
                response.raise_for_status()
                img_bytes = response.content
    except Exception as e:
        logger.error("Image download failed: %s", e)
        return "Image generated, but download failed. The image URL was logged."

    if img_bytes is None:
        return "Sorry, image generation failed."

    # Save to temp file — offload sync I/O to thread pool
    os.makedirs("/tmp/bot_images", exist_ok=True)
    temp_path = os.path.join("/tmp/bot_images", f"gen_{os.urandom(4).hex()}.png")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _write_image, temp_path, img_bytes)

    # Store for the router — keyed by current user via context var
    uid = current_user_id.get()
    if uid:
        _pending_images.setdefault(uid, []).append(temp_path)
        logger.info("Image saved for user %d: %s", uid, temp_path)

    # Keep return text short — the image is sent as a Telegram photo separately
    return "Image generated successfully."


def _write_image(path: str, data: bytes) -> None:
    """Synchronous file write — runs in thread pool executor."""
    with open(path, "wb") as f:
        f.write(data)