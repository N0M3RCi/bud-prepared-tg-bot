from __future__ import annotations

import logging
import os
from typing import Any

from app.llm_client import LLMClient

logger = logging.getLogger(__name__)

TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "summarize_document",
        "description": (
            "Summarize the contents of a file or document. "
            "Reads the file from a given path and generates a concise summary. "
            "Use this when the user wants a TL;DR, key points, or overview "
            "of a PDF, text file, article, or code file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Full path to the file to summarize",
                },
                "max_length": {
                    "type": "integer",
                    "description": "Maximum summary length in words (default 150, max 500)",
                    "default": 150,
                },
                "focus": {
                    "type": "string",
                    "description": "Optional focus area: 'general', 'technical', 'key_points', 'conclusions'",
                    "default": "general",
                },
            },
            "required": ["file_path"],
        },
    },
}


async def summarize_document(file_path: str, max_length: int = 150, focus: str = "general") -> str:
    """Summarize a document file using the LLM."""
    if not os.path.isfile(file_path):
        return f"File not found: {file_path}"

    max_length = max(50, min(max_length, 500))
    focus_instructions = {
        "general": "Provide a general summary covering the main topics.",
        "technical": "Focus on technical details, specifications, and data.",
        "key_points": "List the key points in bullet form.",
        "conclusions": "Focus on conclusions, findings, and takeaways.",
    }.get(focus, "Provide a general summary.")

    # Read file content
    ext = os.path.splitext(file_path)[1].lower()
    content = ""

    if ext == ".pdf":
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(file_path)
            pages = []
            for page in reader.pages[:30]:
                text = page.extract_text()
                if text:
                    pages.append(text)
            content = "\n\n".join(pages)
        except Exception as e:
            return f"Could not read PDF: {e}"
    elif ext in (".docx",):
        try:
            from docx import Document
            doc = Document(file_path)
            content = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            return f"Could not read DOCX: {e}"
    elif ext in (".txt", ".md", ".py", ".js", ".ts", ".html", ".css", ".json", ".csv", ".yaml", ".yml", ".c", ".cpp", ".h", ".java", ".rs", ".go", ".rb", ".php", ".sql", ".sh"):
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            return f"Could not read file: {e}"
    elif ext in (".xlsx", ".xls"):
        try:
            from openpyxl import load_workbook
            wb = load_workbook(file_path, read_only=True, data_only=True)
            rows = []
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                rows.append(f"[Sheet: {sheet}]")
                count = 0
                for row in ws.iter_rows(values_only=True):
                    rows.append(" | ".join(str(c) if c is not None else "" for c in row))
                    count += 1
                    if count > 100:
                        rows.append("... (truncated)")
                        break
            content = "\n".join(rows)
        except Exception as e:
            return f"Could not read XLSX: {e}"
    else:
        # Try reading as text for unknown types
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            return f"Unsupported file type: {ext}"

    if not content.strip():
        return "The file appears to be empty or could not be read."

    # Truncate if too long (send max ~50K chars to LLM)
    if len(content) > 50000:
        content = content[:50000] + "\n\n... (content truncated due to length)"

    filename = os.path.basename(file_path)

    prompt = (
        f"Summarize the following document titled '{filename}'.\n"
        f"{focus_instructions}\n"
        f"Maximum summary length: {max_length} words.\n\n"
        f"Document content:\n{content}"
    )

    summary = await LLMClient.chat_completion(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max(200, max_length * 3),
        temperature=0.3,
    )

    if not summary:
        return "Could not generate a summary."

    return f"Summary of {filename}:\n\n{summary}"
