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


_NEWS_KEYWORDS = {"news", "latest", "update", "today", "current", "breaking",
                    "flood", "weather", "election", "sports", "stock", "market",
                    "covid", "pandemic", "earthquake", "hurricane", "storm",
                    "crash", "accident", "attack", "war", "conflict"}

_DEFAULT_DAYS = 30
_NEWS_DAYS = 7


async def web_search(query: str, max_results: int = 5) -> str:
    """Execute a web search via Tavily with recency-aware parameters.

    Detects news/current-event queries and automatically sets a narrower
    time window and news topic to return fresh results rather than old
    but keyword-relevant results.
    """
    client = _get_client()
    if client is None:
        logger.warning("Tavily API key not configured — web search unavailable")
        return (
            "Web search is not available because no TAVILY_API_KEY is configured. "
            "Please ask the user to set up the Tavily API key."
        )

    # Heuristic: detect news/current-event queries for recency filtering
    query_lower = query.lower()
    is_news = any(kw in query_lower for kw in _NEWS_KEYWORDS)

    try:
        loop = asyncio.get_running_loop()

        def _search() -> dict:
            kwargs: dict = {
                "query": query,
                "max_results": min(max_results, 10),
                "search_depth": "advanced",
            }
            if is_news:
                kwargs["topic"] = "news"
                kwargs["days"] = _NEWS_DAYS
            else:
                kwargs["days"] = _DEFAULT_DAYS
            return client.search(**kwargs)

        response = await loop.run_in_executor(None, _search)
        results = response.get("results", [])

        if not results:
            return f"No results found for '{query}'."

        formatted = []
        for i, r in enumerate(results[:max_results], 1):
            title = r.get("title", "No title")
            url = r.get("url", "")
            snippet = r.get("content", r.get("snippet", ""))
            publish_date = r.get("published_date", "")
            date_str = f" ({publish_date})" if publish_date else ""
            formatted.append(f"{i}. **{title}**{date_str}\n   {snippet}\n   {url}")

        return "\n\n".join(formatted)
    except MissingAPIKeyError:
        return "Web search is not configured. Add TAVILY_API_KEY to environment variables."
    except Exception as e:
        logger.error("Web search failed: %s", e)
        return f"Web search encountered an error: {e}"