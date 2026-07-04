"""Export conversation history as a PDF document."""
from __future__ import annotations

import datetime
import logging
import os
import tempfile
from typing import Optional

from app.database import Database

logger = logging.getLogger(__name__)

_EXTRA_DISCLAIMER = (
    "\n\nExported by Bud AI Assistant"
)

async def export_conversation_pdf(user_id: int, conversation_id: Optional[int] = None) -> Optional[str]:
    """Generate a PDF of the conversation and return the file path."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib.colors import HexColor
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, HRFlowable,
        )
    except ImportError:
        logger.error("reportlab not installed, cannot export PDF")
        return None

    messages = await Database.get_conversation_messages(
        conversation_id,
        limit=None,
    )
    if not messages:
        return None

    # Build PDF
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"bud_conversation_{timestamp}.pdf"
    filepath = os.path.join(tempfile.gettempdir(), filename)

    doc = SimpleDocTemplate(filepath, pagesize=A4,
                            rightMargin=20*mm, leftMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()

    # Custom styles
    user_style = ParagraphStyle(
        "UserStyle", parent=styles["Normal"],
        textColor=HexColor("#2563EB"), spaceAfter=4, spaceBefore=12,
        fontName="Helvetica-Bold",
    )
    user_text_style = ParagraphStyle(
        "UserTextStyle", parent=styles["Normal"],
        leftIndent=10, spaceAfter=12,
        fontSize=10,
    )
    bot_style = ParagraphStyle(
        "BotStyle", parent=styles["Normal"],
        textColor=HexColor("#059669"), spaceAfter=4, spaceBefore=12,
        fontName="Helvetica-Bold",
    )
    bot_text_style = ParagraphStyle(
        "BotTextStyle", parent=styles["Normal"],
        leftIndent=10, spaceAfter=12,
        fontSize=10,
    )

    story = []

    # Title
    title_style = ParagraphStyle(
        "Title2", parent=styles["Title"],
        spaceAfter=6,
    )
    story.append(Paragraph("Bud AI Conversation Export", title_style))
    story.append(Paragraph(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles["Normal"]))
    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=1))
    story.append(Spacer(1, 6*mm))

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            story.append(Paragraph("You:", user_style))
            story.append(Paragraph(content or "(no text)", user_text_style))
        elif role == "assistant":
            story.append(Paragraph("Bud:", bot_style))
            story.append(Paragraph(content or "(no text)", bot_text_style))
        elif role == "system":
            story.append(Spacer(1, 4*mm))
            story.append(HRFlowable(width="60%", thickness=0.5, color=HexColor("#CCCCCC")))
            story.append(Paragraph(f"System: {content}", styles["Italic"]))
            story.append(Spacer(1, 4*mm))

    story.append(Spacer(1, 10*mm))
    story.append(HRFlowable(width="100%", thickness=1))
    story.append(Paragraph("Exported by Bud - AI Telegram Assistant", styles["Normal"]))

    doc.build(story)
    logger.info("PDF conversation export: %s", filepath)
    return filepath
