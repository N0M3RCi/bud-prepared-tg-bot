"""Dashboard event reporter — pushes real-time events to the admin dashboard."""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Shared httpx client with connection pooling — avoids TCP/SSL handshake per event
_dashboard_client: httpx.AsyncClient | None = None


def _get_dashboard_client() -> httpx.AsyncClient:
    """Get or create the shared dashboard httpx client (connection pooling)."""
    global _dashboard_client
    if _dashboard_client is None:
        _dashboard_client = httpx.AsyncClient(
            timeout=5.0,
            limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=30),
        )
    return _dashboard_client


async def _send_event(payload: dict) -> None:
    """Low-level HTTP POST to the dashboard endpoint (fire-and-forget)."""
    url = settings.dashboard_url
    if not url:
        return  # Dashboard not configured — silent no-op
    try:
        client = _get_dashboard_client()
        resp = await client.post(
            f"{url.rstrip('/')}/api/events/report",
            json=payload,
        )
        if resp.status_code != 200:
            logger.warning("Dashboard report failed: HTTP %d", resp.status_code)
    except asyncio.TimeoutError:
        logger.debug("Dashboard report timed out (fire-and-forget)")
    except Exception as e:
        logger.debug("Dashboard report error: %s", e)


async def report_event(
    event: str,
    *,
    user_id: int | None = None,
    first_name: str = "",
    last_name: str = "",
    username: str = "",
    language_code: str = "",
    chat_id: int | None = None,
    chat_type: str = "",
    message_text: str = "",
    tokens: int = 0,
    model: str | None = None,
    msg_type: str = "",
    extra: dict | None = None,
) -> None:
    """Report an event to the dashboard server (fire-and-forget)."""
    url = settings.dashboard_url
    if not url:
        return  # Dashboard not configured — silent no-op

    payload: dict = {"event": event}

    if user_id is not None:
        payload["user"] = {
            "id": user_id,
            "first_name": first_name or "",
            "last_name": last_name or "",
            "username": username or "",
            "language_code": language_code or "en",
        }

    if chat_id is not None:
        payload["chat"] = {
            "id": chat_id,
            "type": chat_type or "private",
        }

    if message_text:
        payload["message"] = {"text": message_text}

    if tokens:
        payload["tokens"] = tokens

    if model:
        payload["model"] = model

    if msg_type:
        payload["type"] = msg_type

    if extra:
        payload["extra"] = extra

    await _send_event(payload)


async def report_message(
    *,
    user_id: int,
    first_name: str = "",
    last_name: str = "",
    username: str = "",
    language_code: str = "",
    chat_id: int | None = None,
    chat_type: str = "",
    text: str = "",
) -> None:
    """Convenience: report a user message event."""
    await report_event(
        "message",
        user_id=user_id,
        first_name=first_name,
        last_name=last_name,
        username=username,
        language_code=language_code,
        chat_id=chat_id,
        chat_type=chat_type,
        message_text=text,
    )


async def report_token_spend(
    *,
    user_id: int,
    tokens: int,
    model: str | None = None,
) -> None:
    """Convenience: report token usage after an AI response."""
    if not tokens:
        return
    await report_event(
        "token_spend",
        user_id=user_id,
        tokens=tokens,
        model=model,
    )


async def report_command(
    *,
    user_id: int,
    command: str,
    args: str = "",
    first_name: str = "",
    last_name: str = "",
    username: str = "",
    chat_id: int | None = None,
    chat_type: str = "",
) -> None:
    """Report a command usage to the dashboard."""
    text = f"/{command}"
    if args:
        text += f" {args}"
    await report_event(
        "command",
        user_id=user_id,
        first_name=first_name,
        last_name=last_name,
        username=username,
        chat_id=chat_id,
        chat_type=chat_type,
        message_text=text,
        msg_type="command",
        extra={"command": command, "args": args},
    )


async def report_system_event(
    event: str,
    details: str = "",
    extra: dict | None = None,
) -> None:
    """Report a system-level event (startup, shutdown, etc.)."""
    payload: dict = {"event": event, "type": "system"}
    if details:
        payload["message"] = {"text": details}
    if extra:
        payload["extra"] = extra
    await _send_event(payload)


async def report_error(
    *,
    user_id: int | None = None,
    error_type: str,
    details: str = "",
) -> None:
    """Report an error event to the dashboard."""
    await report_event(
        "error",
        user_id=user_id,
        message_text=details,
        msg_type="error",
        extra={"error_type": error_type},
    )


async def report_admin_action(
    *,
    admin_id: int,
    action: str,
    details: str = "",
    target_user_id: int | None = None,
    extra: dict | None = None,
) -> None:
    """Report an admin action event to the dashboard."""
    payload: dict = {
        "event": "admin_action",
        "admin_id": admin_id,
        "type": "admin",
        "action": action,
    }
    if details:
        payload["message"] = {"text": details}
    if target_user_id is not None:
        payload["target_user"] = target_user_id
    if extra:
        payload["extra"] = extra
    await _send_event(payload)


async def report_tool_usage(
    *,
    user_id: int,
    tool_name: str,
    status: str = "success",
    error: str = "",
) -> None:
    """Report a tool/skill execution event to the dashboard."""
    extra = {"tool_name": tool_name, "status": status}
    if error:
        extra["error"] = error
    await report_event(
        "tool_usage",
        user_id=user_id,
        msg_type="tool",
        extra=extra,
    )