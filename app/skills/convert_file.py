from __future__ import annotations

import asyncio
import contextvars
import json as json_mod
import logging
import os as os_mod
import tempfile
from typing import Any

from app.llm_client import LLMClient
from app.skills.safe_path import resolve_upload_path

logger = logging.getLogger(__name__)

TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "convert_file",
        "description": (
            "Convert a file from one format to another. "
            "Supported conversions: PDF to text, DOCX to text, "
            "XLSX to CSV, images to text (OCR via vision), "
            "and any text-based format to another text format. "
            "The converted output is saved as a new file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Full path to the source file",
                },
                "target_format": {
                    "type": "string",
                    "description": "Target format: text, csv, json, or markdown",
                    "default": "text",
                },
                "output_filename": {
                    "type": "string",
                    "description": "Optional custom output filename (without extension)",
                },
            },
            "required": ["file_path"],
        },
    },
}

_pending_converted: dict[int, list[tuple[str, str]]] = {}
# ContextVar set by router
current_user_id: contextvars.ContextVar[int] = contextvars.ContextVar("convert_file_uid", default=0)


async def convert_file(file_path: str, target_format: str = "text", output_filename: str | None = None) -> str:
    safe_path = resolve_upload_path(file_path)
    if not safe_path:
        return "File path is not allowed. Only uploaded files can be read."
    file_path = safe_path
    if not os_mod.path.isfile(file_path):
        return f"File not found: {file_path}"

    target_format = target_format.lower().strip()
    valid_formats = {"text", "csv", "json", "markdown"}
    if target_format not in valid_formats:
        return f"Unsupported format: {target_format}. Choose from: {', '.join(sorted(valid_formats))}"

    ext = os_mod.path.splitext(file_path)[1].lower()
    source_name = os_mod.path.splitext(os_mod.path.basename(file_path))[0]
    out_name = output_filename or f"{source_name}_converted"
    out_ext = {"text": ".txt", "csv": ".csv", "json": ".json", "markdown": ".md"}[target_format]

    raw_content = ""

    if ext == ".pdf":
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(file_path)
            pages = [page.extract_text() or "" for page in reader.pages]
            raw_content = "\n\n".join(pages)
        except Exception as e:
            return f"Could not read PDF: {e}"
    elif ext == ".docx":
        try:
            from docx import Document
            doc = Document(file_path)
            raw_content = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            return f"Could not read DOCX: {e}"
    elif ext in (".xlsx", ".xls"):
        try:
            from openpyxl import load_workbook
            wb = load_workbook(file_path, read_only=True, data_only=True)
            parts = []
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                parts.append(f"[Sheet: {sheet}]")
                for row in ws.iter_rows(values_only=True):
                    parts.append(",".join(str(c) if c is not None else "" for c in row))
            raw_content = "\n".join(parts)
        except Exception as e:
            return f"Could not read XLSX: {e}"
    else:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                raw_content = f.read()
        except Exception as e:
            return f"Could not read file: {e}"

    if not raw_content.strip():
        return "The source file appears empty."

    if target_format == "csv":
        lines = raw_content.split("\n")
        csv_lines = []
        for line in lines:
            cells = line.replace(",", " ").split()
            csv_lines.append(",".join(cells))
        output = "\n".join(csv_lines)
    elif target_format == "json":
        lines = raw_content.split("\n")
        output = json_mod.dumps({"filename": source_name, "lines": lines, "total_lines": len(lines)}, indent=2)
    elif target_format == "markdown":
        output = f"# {source_name}\n\nConverted from: `{os_mod.path.basename(file_path)}`\n\n```\n{raw_content}\n```"
    else:
        output = raw_content

    out_path = os_mod.path.join(tempfile.gettempdir(), f"convert_{out_name}{out_ext}")

    def _write():
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(output)
    await asyncio.get_event_loop().run_in_executor(None, _write)

    uid = current_user_id.get()
    if uid:
        _pending_converted.setdefault(uid, []).append((out_path, os_mod.path.basename(out_path)))

    return f"Converted to {target_format}: {os_mod.path.basename(out_path)}"


def pop_pending_converted(user_id: int) -> list[tuple[str, str]]:
    return _pending_converted.pop(user_id, [])
