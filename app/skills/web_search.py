from __future__ import annotations

import asyncio
import logging
from typing import Optional

from tavily import TavilyClient, MissingAPIKeyError

from app.config import settings

logger = logging.getLogger(__name__)

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for current information. Use this when you need real-time or up-to-date data.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query string",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (1-10)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}

_client: Optional[TavilyClient] = None


def _get_client() -> Optional[TavilyClient]:
    global _client
    if _client is None and settings.tavily_api_key:
        _client = TavilyClient(api_key=settings.tavily_api_key)
    return _client


_DAYS = 14
_LATEST_KEYWORDS = {"latest", "recent", "breaking", "just in", "up to date", "newest",
                     "today", "yesterday", "this week", "this morning", "this evening"}


async def web_search(query: str, max_results: int = 5) -> str:
    """Execute a web search via Tavily with recency-aware parameters.

    Detects "latest" / "today" / "breaking" keywords and automatically
    appends today's date and narrows to a 2-day window for freshness.
    Returns just title + URL — clean results with automatic link previews.
    """
    import datetime

    client = _get_client()
    if client is None:
        logger.warning("Tavily API key not configured — web search unavailable")
        return (
            "Web search is not available because no TAVILY_API_KEY is configured. "
            "Please ask the user to set up the Tavily API key."
        )

    # Detect "latest" queries — append today's date and tighten window
    query_lower = query.lower()
    is_latest = any(kw in query_lower for kw in _LATEST_KEYWORDS)
    if is_latest:
        today = datetime.date.today().strftime("%B %d, %Y")
        if today.split()[0].lower() not in query_lower:
            query = f"{query} {today}"

    try:
        loop = asyncio.get_running_loop()

        def _search() -> dict:
            return client.search(
                query=query,
                max_results=min(max_results + 3, 10),
                search_depth="advanced",
                days=2 if is_latest else _DAYS,
            )

        response = await loop.run_in_executor(None, _search)
        raw_results = response.get("results", [])

        if not raw_results:
            return f"No results found for '{query}'."

        if is_latest:
            # Parse dates and bucket results by recency
            today_d = datetime.date.today()
            yesterday_d = today_d - datetime.timedelta(days=1)

            def _parse_date(entry: dict) -> datetime.date | None:
                raw = entry.get("published_date", "")
                if not raw:
                    return None
                raw = raw.split("T")[0].split(" ")[0]
                try:
                    parts = raw.split("-")
                    if len(parts) == 3:
                        return datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
                except (ValueError, IndexError):
                    pass
                return None

            today_results: list[dict] = []
            yesterday_results: list[dict] = []
            older_results: list[dict] = []

            for r in raw_results:
                d = _parse_date(r)
                if d == today_d:
                    today_results.append(r)
                elif d == yesterday_d:
                    yesterday_results.append(r)
                else:
                    older_results.append(r)

            # Distribute: fill ~75% from today first, then yesterday, then older
            results: list[dict] = []
            today_take = min(len(today_results),
                             max_results - len(results))
            if today_take > 0:
                # Take up to 3/4 from today, ensuring at least 1 if available
                target_today = max(1, max_results * 3 // 4)
                today_take = min(today_take, min(target_today, today_take))
            results.extend(today_results[:today_take])
            yesterday_take = min(len(yesterday_results), max_results - len(results))
            results.extend(yesterday_results[:yesterday_take])
            if len(results) < max_results:
                results.extend(older_results[:max_results - len(results)])
        else:
            results = raw_results[:max_results]

        # Clean output: just title + URL per result. Telegram auto-generates
        # link previews so the user sees the page card without long snippets.
        formatted = []
        for i, r in enumerate(results[:max_results], 1):
            title = r.get("title", "No title")
            url = r.get("url", "")
            publish_date = r.get("published_date", "")
            date_str = f" ({publish_date})" if publish_date else ""
            formatted.append(f"{i}. {title}{date_str}\n   {url}")

        return "\n\n".join(formatted)
    except MissingAPIKeyError:
        return "Web search is not configured. Add TAVILY_API_KEY to environment variables."
    except Exception as e:
        logger.error("Web search failed: %s", e)
        return f"Web search encountered an error: {e}"