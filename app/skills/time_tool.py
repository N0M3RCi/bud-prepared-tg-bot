from __future__ import annotations

import datetime
import logging
from zoneinfo import ZoneInfo, available_timezones
from typing import Any

logger = logging.getLogger(__name__)

TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": (
            "Get the current date and time for a given timezone. "
            "Use an IANA timezone name like 'Africa/Accra', 'UTC', 'America/New_York'. "
            "Common timezone mappings: Ghana = Africa/Accra (UTC+0, no DST), "
            "Nigeria = Africa/Lagos, UK = Europe/London, US East = America/New_York, "
            "US West = America/Los_Angeles, India = Asia/Kolkata, "
            "Japan = Asia/Tokyo, Australia East = Australia/Sydney. "
            "For any country you can usually use 'Area/CapitalCity' format."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone name (e.g. 'Africa/Accra', 'UTC', 'America/New_York')",
                },
            },
            "required": ["timezone"],
        },
    },
}


async def get_current_time(timezone: str) -> str:
    """Get the current date and time for the given IANA timezone."""
    try:
        tz = ZoneInfo(timezone)
    except (KeyError, TypeError):
        # Try common variations
        normalized = timezone.strip()
        # Check if it's a known timezone (case-insensitive)
        tz_names = {t.lower(): t for t in available_timezones()}
        matched = tz_names.get(normalized.lower())
        if matched:
            tz = ZoneInfo(matched)
        else:
            available = sorted(available_timezones())
            # Try partial match
            keyword = normalized.replace("/", " ").lower()
            partials = [t for t in available if keyword in t.lower()]
            if partials:
                tz = ZoneInfo(partials[0])
            else:
                return (
                    f"Unknown timezone '{timezone}'. "
                    f"Use an IANA timezone name like 'Africa/Accra', 'UTC', 'America/New_York'. "
                    f"Example mappings: Ghana = Africa/Accra, Nigeria = Africa/Lagos, UK = Europe/London."
                )

    now = datetime.datetime.now(tz)
    formatted = now.strftime("%A, %B %d, %Y at %I:%M:%S %p %Z (UTC%z)")
    return f"The current time in {tz} is {formatted}."