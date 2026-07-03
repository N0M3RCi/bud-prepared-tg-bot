from __future__ import annotations

import asyncio
import logging
import os
from contextvars import ContextVar

logger = logging.getLogger(__name__)

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write or generate a file with the given content and filename. "
                       "Use this to create code files, HTML pages, scripts, text files, "
                       "config files, or any other text-based file the user requests. "
                       "The file will be sent to the user as a downloadable document.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "The name of the file to create (e.g., 'index.html', 'script.py', 'style.css', 'landing_page.html')",
                },
                "content": {
                    "type": "string",
                    "description": "The full content of the file to write",
                },
                "description": {
                    "type": "string",
                    "description": "Optional short description of what the file does (shown to the user)",
                },
            },
            "required": ["filename", "content"],
        },
    },
}

# Context variable — set by router before calling tools, read by file_writer
current_user_id: ContextVar[int] = ContextVar("file_writer_user_id", default=0)

# Per-user pending files: {user_id: [(file_path, display_name), ...]}
_pending_files: dict[int, list[tuple[str, str]]] = {}


def pop_pending_files(user_id: int) -> list[tuple[str, str]]:
    """Return and clear all pending file paths for a user.

    Returns list of (file_path, display_name) tuples.
    """
    return _pending_files.pop(user_id, [])


async def write_file(filename: str, content: str, description: str = "") -> str:
    """Write a file with the given content and register it for delivery.

    The file is saved to a temp directory so the router can send it
    as a Telegram document.
    """
    if not filename or not content:
        return "Error: Both filename and content are required."

    # Sanitize filename — prevent path traversal
    safe_filename = filename.replace("/", "_").replace("\\", "_").strip()
    if not safe_filename:
        safe_filename = "output.txt"

    # Save to temp directory — offload sync I/O to thread pool
    os.makedirs("/tmp/bot_files", exist_ok=True)
    temp_path = os.path.join("/tmp/bot_files", f"{os.urandom(4).hex()}_{safe_filename}")
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_text_file, temp_path, content)
    except Exception as e:
        logger.error("Failed to write file %s: %s", safe_filename, e)
        return f"Error: Failed to write file '{safe_filename}': {e}"

    # Store for the router — keyed by current user via context var
    uid = current_user_id.get()
    if uid:
        _pending_files.setdefault(uid, []).append((temp_path, safe_filename))
        logger.info("File saved for user %d: %s (%d bytes)", uid, safe_filename, len(content))

    desc = f" ({description})" if description else ""
    return f"File '{safe_filename}' created successfully{desc}. The file will be sent to you as a document."


def _write_text_file(path: str, content: str) -> None:
    """Synchronous file write — runs in thread pool executor."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)