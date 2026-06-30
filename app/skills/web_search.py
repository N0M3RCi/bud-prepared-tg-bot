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


async def web_search(query: str, max_results: int = 5) -> str:
    """Execute a web search via Tavily with recency-aware parameters.

    Uses general search with a 14-day window to return fresh, relevant
    results without Tavily's news topic categorization (which adds
    irrelevant category labels to results).
    """
    client = _get_client()
    if client is None:
        logger.warning("Tavily API key not configured — web search unavailable")
        return (
            "Web search is not available because no TAVILY_API_KEY is configured. "
            "Please ask the user to set up the Tavily API key."
        )

    try:
        loop = asyncio.get_running_loop()

        def _search() -> dict:
            return client.search(
                query=query,
                max_results=min(max_results, 10),
                search_depth="advanced",
                days=_DAYS,
            )

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
            formatted.append(f"{i}. {title}{date_str}\n   {snippet}\n   {url}")

        return "\n\n".join(formatted)
    except MissingAPIKeyError:
        return "Web search is not configured. Add TAVILY_API_KEY to environment variables."
    except Exception as e:
        logger.error("Web search failed: %s", e)
        return f"Web search encountered an error: {e}"