from __future__ import annotations

import asyncio
import base64
import io
import logging
import os

from PIL import Image

from app.config import settings
from app.llm_client import LLMClient

logger = logging.getLogger(__name__)

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read and analyze the contents of an uploaded file. "
            "Supports: PDF documents, images (analyzed with AI vision), "
            "text files, code files, JSON, CSV, and common document formats."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The local file path of the uploaded file to read",
                },
                "file_type": {
                    "type": "string",
                    "description": "The type of file: pdf, image, text, code, json, csv, or auto (auto-detect)",
                    "default": "auto",
                },
                "analysis_prompt": {
                    "type": "string",
                    "description": "Specific question about what to look for in the file",
                    "default": "Summarize the contents of this file.",
                },
            },
            "required": ["file_path"],
        },
    },
}

_DOWNLOAD_DIR = "/tmp/bot_downloads"
_MAX_FILE_SIZE = settings.file_max_size_mb * 1024 * 1024

# Extensions that are never safe to parse as text
_BLOCKED_EXTENSIONS = set(settings.blocked_file_extensions)


def _is_extension_blocked(ext: str) -> bool:
    return ext.lower() in _BLOCKED_EXTENSIONS


async def read_file(file_path: str, file_type: str = "auto",
                     analysis_prompt: str = "Summarize the contents of this file.") -> str:
    """Read and analyze a file. Runs sync I/O in a thread executor."""
    ext = os.path.splitext(file_path)[1].lower()

    # Security: block dangerous file types BEFORE checking existence
    if _is_extension_blocked(ext):
        return f"File type '{ext}' is not supported for security reasons."

    if not os.path.exists(file_path):
        return f"File not found at path: {file_path}. It may not have finished downloading."

    # Check file size
    size = os.path.getsize(file_path)
    if size > _MAX_FILE_SIZE:
        return f"File is too large ({size / 1024 / 1024:.1f} MB). Maximum supported size is {settings.file_max_size_mb} MB."

    detected_type = file_type if file_type != "auto" else _detect_type(ext)

    try:
        if detected_type == "image":
            return await _analyze_image(file_path, analysis_prompt)
        elif detected_type in ("pdf",):
            return await _read_pdf(file_path)
        elif detected_type in ("text", "code", "json", "csv"):
            return await _read_text_file(file_path, detected_type)
        else:
            text = await _read_text_file(file_path, "text")
            if text.startswith("Error"):
                return await _analyze_image(file_path, analysis_prompt)
            return text
    except Exception as e:
        logger.error("File reading failed for %s: %s", file_path, e)
        return f"Failed to read file: {e}"


def _detect_type(ext: str) -> str:
    mapping = {
        ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
        ".webp": "image", ".bmp": "image", ".tiff": "image",
        ".pdf": "pdf",
        ".txt": "text", ".md": "text", ".rst": "text",
        ".py": "code", ".js": "code", ".ts": "code", ".jsx": "code",
        ".tsx": "code", ".java": "code", ".c": "code", ".cpp": "code",
        ".h": "code", ".go": "code", ".rs": "code", ".rb": "code",
        ".php": "code", ".swift": "code", ".kt": "code", ".sql": "code",
        ".json": "json", ".csv": "csv", ".yaml": "yaml", ".yml": "yaml",
        ".xml": "text", ".html": "code", ".css": "code",
        ".log": "text", ".cfg": "text", ".ini": "text", ".toml": "text",
    }
    return mapping.get(ext, "text")


async def _read_text_file(file_path: str, file_type: str) -> str:
    """Read a plain text-based file in a thread executor."""
    loop = asyncio.get_running_loop()

    def _read() -> str:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    content = await loop.run_in_executor(None, _read)

    if not content.strip():
        return "The file appears to be empty."

    max_chars = settings.file_reader_max_chars
    if len(content) > max_chars:
        content = content[:max_chars] + "\n\n... [file truncated]"

    lang_label = {"json": "JSON", "csv": "CSV", "code": "Code", "yaml": "YAML",
                  }.get(file_type, "Text")
    return f"```{file_type}\n{content}\n```\n\n(File type: {lang_label}, {len(content)} characters read)"


async def _read_pdf(file_path: str) -> str:
    """Extract text from a PDF file in a thread executor."""
    loop = asyncio.get_running_loop()

    def _read_pdf_sync() -> str:
        try:
            import PyPDF2  # type: ignore[import-untyped]
        except ImportError:
            return "PDF reading requires PyPDF2. Install it with: pip install PyPDF2"

        try:
            text_parts = []
            with open(file_path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for i, page in enumerate(reader.pages):
                    page_text = page.extract_text() or ""
                    text_parts.append(f"--- Page {i + 1} ---\n{page_text.strip()}")

            full_text = "\n\n".join(text_parts)
            if not full_text.strip():
                return "The PDF appears to contain no extractable text (scanned/image-based)."

            max_chars = settings.file_reader_max_chars
            if len(full_text) > max_chars:
                full_text = full_text[:max_chars] + "\n\n... [file truncated]"

            return f"PDF Contents:\n\n{full_text}"
        except Exception as e:
            return f"Error reading PDF: {e}"

    return await loop.run_in_executor(None, _read_pdf_sync)


async def _analyze_image(file_path: str, analysis_prompt: str) -> str:
    """Analyze an image using NEAR AI vision capabilities."""
    loop = asyncio.get_running_loop()

    # Read image metadata in executor
    def _get_image_info() -> tuple[int, int, str, str]:
        with open(file_path, "rb") as f:
            img_data = f.read()
        img = Image.open(io.BytesIO(img_data))
        b64 = base64.b64encode(img_data).decode("utf-8")
        return img.width, img.height, (img.format or "unknown").lower(), b64

    try:
        width, height, fmt, b64 = await loop.run_in_executor(None, _get_image_info)
    except Exception as e:
        return f"Failed to read image file: {e}"

    data_uri = f"data:image/{fmt};base64,{b64}"

    client = LLMClient.get_client()

    try:
        response = await client.chat.completions.create(
            model=settings.near_ai_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": analysis_prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            max_tokens=2048,
        )
        analysis = response.choices[0].message.content or "No analysis generated."
        return f"**Image Analysis** ({width}x{height}, {fmt})\n\n{analysis}"
    except ImportError:
        return f"Image file detected ({file_path}). Install required packages."
    except Exception as e:
        logger.error("Image analysis failed: %s", e)
        return f"Failed to analyze image: {e}"