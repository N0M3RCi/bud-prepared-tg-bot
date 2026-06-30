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
                max_results=min(max_results, 10),
                search_depth="advanced",
                days=2 if is_latest else _DAYS,
            )

        response = await loop.run_in_executor(None, _search)
        results = response.get("results", [])

        if not results:
            return f"No results found for '{query}'."

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