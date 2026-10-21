"""Limit model-requested file reads to the upload folder."""

from __future__ import annotations

import os

_ROOT = os.path.realpath("/tmp/bot_downloads")


def resolve_upload_path(file_path: str) -> str | None:
    """Return a real path inside /tmp/bot_downloads, or None if it escapes that folder."""
    if not file_path or not isinstance(file_path, str):
        return None
    os.makedirs(_ROOT, exist_ok=True)
    real = os.path.realpath(file_path)
    if real != _ROOT and not real.startswith(_ROOT + os.sep):
        return None
    return real
